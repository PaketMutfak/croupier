import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import ClassVar
from typing import Literal
from typing import override

import sentry_sdk
import uvicorn
from escpos.printer import Network
from fastapi import FastAPI
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from faststream import BaseMiddleware
from faststream.exceptions import IgnoredException
from faststream.rabbit import RabbitQueue
from faststream.rabbit.fastapi import RabbitRouter
from pydantic import AmqpDsn
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import HttpUrl
from pydantic_settings import BaseSettings
from pydantic_settings import JsonConfigSettingsSource
from pydantic_settings import PydanticBaseSettingsSource
from pydantic_settings import SettingsConfigDict
from starlette.responses import Response
from starlette.status import HTTP_204_NO_CONTENT
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable

    from faststream.message import StreamMessage


class Settings(BaseSettings):
    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        extra="ignore",
        frozen=True,
        json_file=Path.home() / ".croupier.json",
        json_file_encoding="utf-8",
    )
    queue_url: AmqpDsn
    exchange_name: str
    queue_name: str
    dlx_name: str
    dlq_name: str
    sentry_dsn: HttpUrl | None = None
    sentry_environment: Literal["development", "staging", "production"] = "production"
    sentry_release: str | None = None

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


class SentryMiddleware(BaseMiddleware[Any, bytes]):
    @override
    async def consume_scope(
        self,
        call_next: Callable[[StreamMessage[bytes]], Awaitable[Any]],
        msg: StreamMessage[bytes],
    ) -> Any:
        with sentry_sdk.isolation_scope():
            try:
                return await call_next(msg)
            except IgnoredException:
                raise
            except Exception:
                sentry_sdk.capture_exception()
                raise


settings = Settings()  # type: ignore[call-arg]
router = RabbitRouter(
    settings.queue_url.unicode_string(),
    middlewares=(SentryMiddleware,),
)


@router.subscriber(queue=RabbitQueue(name=settings.queue_name, declare=False))
@router.post("/handle-message")
async def handle_message(body: Message) -> None:  # noqa: RUF029
    sentry_sdk.set_tag("printer.id", f"{settings.queue_name}:{body.network_host}")
    # pyrefly: ignore[missing-attribute]
    sentry_sdk.get_isolation_scope().fingerprint = [
        "{{ default }}",
        settings.queue_name,
    ]
    printer = Network(
        host=body.network_host,
        timeout=body.network_timeout,
    )
    printer.open()
    try:
        printer._raw(body.content)  # noqa: SLF001  # pylint: disable=W0212
    finally:
        printer.close()


@router.get("/")
async def health(request: Request) -> Response:
    if await request.state.broker.ping(6):
        return Response(status_code=HTTP_204_NO_CONTENT)
    return Response(status_code=HTTP_500_INTERNAL_SERVER_ERROR)


def main() -> None:
    file_handler = RotatingFileHandler(
        filename=Path.home() / ".croupier.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"),
    )
    for name in ("faststream", "faststream.access.rabbit", "sentry_sdk.errors"):
        logging.getLogger(name).addHandler(file_handler)

    if settings.sentry_dsn is not None:
        sentry_sdk.init(
            dsn=str(settings.sentry_dsn),
            environment=settings.sentry_environment,
            release=settings.sentry_release,
            send_default_pii=False,
            include_local_variables=False,
            attach_stacktrace=True,
        )
        sentry_sdk.set_tag("queue_name", settings.queue_name)

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
