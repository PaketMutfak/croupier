import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Annotated
from typing import Any
from typing import ClassVar
from typing import Literal
from typing import Self
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
from pydantic import AfterValidator
from pydantic import AmqpDsn
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import HttpUrl
from pydantic import model_validator
from pydantic_settings import BaseSettings
from pydantic_settings import JsonConfigSettingsSource
from pydantic_settings import PydanticBaseSettingsSource
from pydantic_settings import SettingsConfigDict
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from starlette.responses import Response
from starlette.status import HTTP_204_NO_CONTENT
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable

    from faststream.message import StreamMessage


logger = logging.getLogger(__name__)

type SentryEnvironment = Literal["development", "staging", "production"]


def _validate_sentry_dsn(url: HttpUrl) -> HttpUrl:
    # A real Sentry DSN is `<scheme>://<publickey>@<host>/<projectid>`.
    # `HttpUrl` alone happily accepts plain `https://example.com`, which only
    # surfaces as a transport error at first capture. Reject obviously broken
    # DSNs at config load instead.
    if not url.username:
        msg = "sentry_dsn must include a public key as URL userinfo"
        raise ValueError(msg)
    project_id = (url.path or "").lstrip("/").split("/", 1)[0]
    if not project_id:
        msg = "sentry_dsn must include a project id in the URL path"
        raise ValueError(msg)
    return url


type SentryDsn = Annotated[HttpUrl, AfterValidator(_validate_sentry_dsn)]


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
    sentry_dsn: SentryDsn | None = None
    sentry_environment: SentryEnvironment | None = None

    @model_validator(mode="after")
    def _require_environment_when_dsn_set(self) -> Self:
        # Otherwise an operator who sets `sentry_dsn` without touching
        # `sentry_environment` ships events tagged with whatever default we pick,
        # masking the real deploy stage.
        if self.sentry_dsn is not None and self.sentry_environment is None:
            msg = "sentry_environment must be set when sentry_dsn is set"
            raise ValueError(msg)
        return self

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
            except Exception as exc:
                # Distinguishes payload-decode failures (Pydantic ValidationError
                # raised before handle_message runs) from in-handler errors. Without
                # this tag both land under the same fingerprint and operators cannot
                # tell whether a printer was even contacted.
                sentry_sdk.set_tag("error.class", type(exc).__name__)
                sentry_sdk.capture_exception()
                raise


settings = Settings()  # type: ignore[call-arg]
router = RabbitRouter(
    settings.queue_url.unicode_string(),
    middlewares=(SentryMiddleware,) if settings.sentry_dsn is not None else (),
)


@router.subscriber(queue=RabbitQueue(name=settings.queue_name, declare=False))
@router.post("/handle-message")
async def handle_message(body: Message) -> None:  # noqa: RUF029
    sentry_sdk.set_tag("printer.id", f"{settings.queue_name}:{body.network_host}")
    sentry_sdk.set_context(
        "printer",
        {
            "host": body.network_host,
            "timeout": body.network_timeout,
            "payload_size": len(body.content),
        },
    )
    # pyrefly: ignore[missing-attribute]
    sentry_sdk.get_isolation_scope().fingerprint = [
        "{{ default }}",
        settings.queue_name,
    ]
    printer = Network(
        host=body.network_host,
        timeout=body.network_timeout,
    )
    sentry_sdk.add_breadcrumb(
        category="printer",
        level="info",
        message="open",
        data={
            "host": body.network_host,
            "port": 9100,
            "timeout": body.network_timeout,
        },
    )
    printer.open()
    sentry_sdk.add_breadcrumb(
        category="printer",
        level="info",
        message="raw",
        data={"bytes": len(body.content)},
    )
    try:
        printer._raw(body.content)  # noqa: SLF001  # pylint: disable=W0212
    finally:
        try:
            printer.close()
        except Exception:
            # Preserve original _raw() exception. close() failing on a half-broken
            # socket would otherwise replace the real print failure in tracebacks
            # and DLQ/Sentry fingerprints.
            sentry_sdk.capture_exception()
            logger.exception("printer close failed")


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
    # "croupier" is the package logger; child loggers (e.g. `croupier.main`)
    # inherit the handler so logger.exception() lands in ~/.croupier.log.
    for name in ("croupier", "faststream", "faststream.access.rabbit"):
        logging.getLogger(name).addHandler(file_handler)

    if settings.sentry_dsn is not None:
        # Attach handler to sentry_sdk.errors before init() so DSN parse errors,
        # integration setup failures, and transport warnings emitted during init
        # land in ~/.croupier.log instead of the default root logger.
        logging.getLogger("sentry_sdk.errors").addHandler(file_handler)
        try:
            sentry_sdk.init(
                dsn=settings.sentry_dsn.unicode_string(),
                environment=settings.sentry_environment,
                send_default_pii=False,
                include_local_variables=False,
                attach_stacktrace=True,
                integrations=[
                    FastApiIntegration(),
                    StarletteIntegration(),
                    AsyncioIntegration(),
                    # event_level=CRITICAL silences auto-events from logger.error/
                    # logger.exception so we do not duplicate the explicit
                    # sentry_sdk.capture_exception() calls in handle_message.
                    # INFO breadcrumbs from python-escpos and our own logger keep
                    # the trail leading up to a captured event.
                    LoggingIntegration(
                        level=logging.INFO,
                        event_level=logging.CRITICAL,
                    ),
                ],
            )
            sentry_sdk.set_tag("queue_name", settings.queue_name)
        except Exception:
            # README documents Sentry as Optional; a misconfigured DSN must not
            # take down receipt printing. Service continues without Sentry.
            logger.exception("sentry_sdk.init failed; continuing without Sentry")

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
