import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import ClassVar
from typing import cast

import sentry_sdk
import uvicorn
from escpos.printer import Network
from fastapi import FastAPI
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from faststream.exceptions import IgnoredException
from faststream.middlewares import ExceptionMiddleware
from faststream.rabbit import RabbitQueue
from faststream.rabbit.fastapi import RabbitRouter
from pydantic import AmqpDsn
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic_settings import BaseSettings
from pydantic_settings import JsonConfigSettingsSource
from pydantic_settings import PydanticBaseSettingsSource
from pydantic_settings import SettingsConfigDict
from starlette.responses import Response
from starlette.status import HTTP_204_NO_CONTENT
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR

if TYPE_CHECKING:
    from sentry_sdk._types import Event
    from sentry_sdk._types import Hint

_PII_SENTINEL = "[Filtered]"
_PII_KEYS: frozenset[str] = frozenset({"content", "body", "data"})


class Settings(BaseSettings):
    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        extra="ignore",
        json_file=Path.home() / ".croupier.json",
        json_file_encoding="utf-8",
    )
    queue_url: AmqpDsn
    exchange_name: str
    queue_name: str
    dlx_name: str
    dlq_name: str
    sentry_dsn: str | None = None
    sentry_environment: str = "production"
    sentry_release: str | None = None
    sentry_traces_sample_rate: float = 0.0
    sentry_sample_rate: float = 1.0
    sentry_max_breadcrumbs: int = 30
    branch_id: str | None = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        env_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (JsonConfigSettingsSource(settings_cls),)


class Message(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")
    content: bytes
    network_host: str
    network_timeout: int


exception_middleware = ExceptionMiddleware()


@exception_middleware.add_handler(Exception)
def _capture_to_sentry(exc: Exception) -> None:
    if isinstance(exc, IgnoredException):
        return
    sentry_sdk.capture_exception(exc)


settings = Settings()  # type: ignore[call-arg]
router = RabbitRouter(
    settings.queue_url.unicode_string(),
    middlewares=(exception_middleware,),
)


@router.subscriber(queue=RabbitQueue(name=settings.queue_name, declare=False))
@router.post("/handle-message")
async def handle_message(body: Message) -> None:  # noqa: RUF029
    branch = settings.branch_id or "unknown"
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("printer.host", body.network_host)
        scope.set_tag("printer.id", f"{branch}:{body.network_host}")
        scope.fingerprint = ["{{ default }}", branch]
        printer = Network(
            host=body.network_host,
            timeout=body.network_timeout,
        )
        printer.open()
        printer._raw(body.content)  # noqa: SLF001  # pylint: disable=W0212
        printer.close()


@router.get("/")
async def health(request: Request) -> Response:
    if await request.state.broker.ping(6):
        return Response(status_code=HTTP_204_NO_CONTENT)
    return Response(status_code=HTTP_500_INTERNAL_SERVER_ERROR)


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _PII_SENTINEL if key in _PII_KEYS else _scrub(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return _PII_SENTINEL
    return value


def _scrub_event(event: Event, _hint: Hint) -> Event | None:
    return cast("Event", _scrub(event))


def main() -> None:
    if settings.sentry_dsn:
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.sentry_environment,
            release=settings.sentry_release,
            sample_rate=settings.sentry_sample_rate,
            traces_sample_rate=settings.sentry_traces_sample_rate,
            max_breadcrumbs=settings.sentry_max_breadcrumbs,
            send_default_pii=False,
            include_local_variables=False,
            attach_stacktrace=True,
            before_send=_scrub_event,
        )
        if settings.branch_id:
            sentry_sdk.set_tag("branch_id", settings.branch_id)

    file_handler = RotatingFileHandler(
        filename=Path.home() / ".croupier.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"),
    )
    for name in ("faststream", "faststream.access.rabbit"):
        logging.getLogger(name).addHandler(file_handler)

    app = FastAPI(debug=True)
    app.include_router(router)
    app.add_middleware(
        CORSMiddleware,  # ty:ignore[invalid-argument-type]
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )
    uvicorn.run(app)
