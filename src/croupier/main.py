import logging
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import ClassVar
from typing import Literal
from typing import override

import sentry_sdk
import uvicorn
from escpos.printer import Network
from faststream import BaseMiddleware
from faststream.asgi import AsgiFastStream
from faststream.exceptions import IgnoredException
from faststream.rabbit import RabbitBroker
from faststream.rabbit import RabbitQueue
from lite_bootstrap import FastStreamBootstrapper
from lite_bootstrap import FastStreamConfig
from pydantic import AmqpDsn
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import HttpUrl
from pydantic_settings import BaseSettings
from pydantic_settings import JsonConfigSettingsSource
from pydantic_settings import PydanticBaseSettingsSource
from pydantic_settings import SettingsConfigDict
from sentry_sdk.integrations.asyncio import AsyncioIntegration

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable

    from faststream.message import StreamMessage


logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        extra="ignore",
        frozen=True,
        json_file=Path.home() / ".croupier.json",
        json_file_encoding="utf-8",
    )
    queue_url: AmqpDsn
    queue_name: str
    sentry_dsn: HttpUrl | None = None
    sentry_environment: Literal["development", "staging", "production"] = "development"

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
        # Skip wrapping when sentry_sdk has no active client — only the
        # ``sentry_dsn=None`` path flips ``is_initialized()`` to False
        # (lite-bootstrap never calls ``sentry_sdk.init`` so the global stays a
        # ``NonRecordingClient``). A misconfigured-but-syntactically-valid DSN
        # leaves ``is_initialized()`` True with a real ``_Client``; events still
        # try to ship and surface as transport-layer warnings, which is the
        # loud behavior we want.
        if not sentry_sdk.is_initialized():
            return await call_next(msg)
        with sentry_sdk.isolation_scope() as scope:
            # Per-message correlation_id as a searchable tag so a log line
            # carrying ``correlation_id=...`` can be pivoted to the matching
            # Sentry event in one filter. High cardinality is fine here —
            # the index is used for lookup, never for groupby aggregation.
            scope.set_tag("correlation_id", msg.correlation_id)
            try:
                return await call_next(msg)
            except IgnoredException:
                raise
            except Exception:
                # Sentry SDK's auto-enabled LoggingIntegration promotes
                # ERROR-and-up records to standalone events; logger.exception
                # is enough — no explicit capture_exception() needed. Exception
                # class (e.g. payload-decode ValidationError vs in-handler
                # error) is filterable in the Sentry UI via the built-in
                # ``error.type`` index, so no custom tag is needed.
                logger.exception("message handler failed")
                # Re-raise so FastStream's NACK -> DLX/DLQ path runs.
                raise


settings = Settings()  # type: ignore[call-arg]
broker = RabbitBroker(
    settings.queue_url.unicode_string(),
    middlewares=(SentryMiddleware,) if settings.sentry_dsn else (),
)


@broker.subscriber(queue=RabbitQueue(name=settings.queue_name, declare=False))
async def handle_message(body: Message) -> None:  # noqa: RUF029
    printer = Network(
        host=body.network_host,
        timeout=body.network_timeout,
    )
    try:
        # ``open()`` lives inside the try so a half-open socket from a failed
        # connect still gets a ``close()`` attempt in the finally block
        # (close() on a never-opened printer raises AttributeError, which the
        # narrow except below swallows).
        printer.open()
        # python-escpos exposes only ``_raw`` for sending pre-built ESC/POS
        # bytes. The leading underscore is a library convention, not a
        # private-API hazard for this caller.
        printer._raw(body.content)  # noqa: SLF001  # pylint: disable=W0212
    finally:
        try:
            printer.close()
        # PEP 758 unparenthesized except — requires Python 3.14+ (see
        # ``requires-python`` in pyproject.toml). Equivalent to
        # ``except (OSError, AttributeError):``; do not "fix" by adding
        # parens unless the floor is moved below 3.14.
        except OSError, AttributeError:
            # Preserve the original _raw() / open() exception. close() failing
            # on a half-broken socket would otherwise replace the real fault
            # in tracebacks and DLQ/Sentry fingerprints. Narrow to OSError
            # (socket cleanup) and AttributeError (printer state when open
            # never completed); broader exception classes hide programming
            # bugs. logger.exception is auto-promoted to a separate Sentry
            # event by the SDK's default LoggingIntegration.
            logger.exception("printer close failed")


def create_app() -> AsgiFastStream:
    # ASGI app factory: builds the ``FastStreamConfig`` and runs it through
    # ``FastStreamBootstrapper`` so lite-bootstrap's instruments (Sentry,
    # structlog) wire themselves up. Compatible with
    # ``uvicorn --factory``: ``uvicorn croupier.main:create_app --factory``.
    config = FastStreamConfig(
        application=AsgiFastStream(broker),
        service_name="croupier",
        service_environment=settings.sentry_environment,
        # Required for the LoggingInstrument to take effect (structlog ->
        # JSON stdout). Toggle if a future requirement asks for plain
        # logging in development.
        service_debug=False,
        sentry_dsn=(
            settings.sentry_dsn.unicode_string() if settings.sentry_dsn else None
        ),
        # AsyncioIntegration is NOT in sentry-sdk's default integrations set,
        # so it has to be added explicitly. It catches unhandled exceptions
        # in background asyncio tasks — defense in depth in case something
        # escapes SentryMiddleware. The auto-enabled LoggingIntegration
        # handles ERROR-and-up log records, which is how SentryMiddleware
        # and the close-failure finally block emit their events.
        sentry_integrations=[AsyncioIntegration()],
        # Process-wide Sentry tags. lite-bootstrap calls sentry_sdk.set_tags()
        # after init.
        sentry_tags={"queue_name": settings.queue_name},
    )
    return FastStreamBootstrapper(config).bootstrap()


def main() -> None:
    uvicorn.run(create_app())
