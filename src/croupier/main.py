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
from faststream.rabbit.opentelemetry import RabbitTelemetryMiddleware
from faststream.rabbit.prometheus import RabbitPrometheusMiddleware
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
    exchange_name: str
    queue_name: str
    dlx_name: str
    dlq_name: str
    sentry_dsn: HttpUrl | None = None
    sentry_environment: Literal["development", "staging", "production"] | None = None

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
        # Skip wrapping when Sentry is not initialized (e.g. lite-bootstrap
        # decided sentry_dsn was missing, or init failed). Keeps the consume
        # path identical to the no-DSN configuration.
        if not sentry_sdk.get_client().is_active():
            return await call_next(msg)
        with sentry_sdk.isolation_scope():
            try:
                return await call_next(msg)
            except IgnoredException:
                raise
            except Exception as exc:
                # Distinguishes payload-decode failures (Pydantic ValidationError
                # raised before handle_message runs) from in-handler errors so
                # operators can tell whether a printer was even contacted. Tag
                # is set on the isolation scope, so the auto-promoted Sentry
                # event from the logger.exception below picks it up.
                sentry_sdk.set_tag("error.class", type(exc).__name__)
                # Sentry SDK's auto-enabled LoggingIntegration promotes
                # ERROR-and-up records to standalone events; logger.exception
                # is enough — no explicit capture_exception() needed.
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
            "port": printer.port,
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
        except OSError, AttributeError:
            # Preserve the original _raw() exception. close() failing on a
            # half-broken socket would otherwise replace the real print failure
            # in tracebacks and DLQ/Sentry fingerprints. Narrow to OSError
            # (socket cleanup) and AttributeError (printer state when open
            # never completed); broader exception classes hide programming bugs.
            # logger.exception is auto-promoted to a separate Sentry event by
            # the SDK's default LoggingIntegration.
            logger.exception("printer close failed")


def main() -> None:
    uvicorn.run(
        FastStreamBootstrapper(
            FastStreamConfig(
                application=AsgiFastStream(broker),
                service_name="croupier",
                service_version="0.1.0",
                service_environment=settings.sentry_environment,
                # Required for the LoggingInstrument to take effect (structlog
                # -> JSON stdout). Toggle this if a future requirement asks for
                # plain logging in development.
                service_debug=False,
                sentry_dsn=(
                    settings.sentry_dsn.unicode_string()
                    if settings.sentry_dsn
                    else None
                ),
                # Process-wide Sentry tags. lite-bootstrap calls
                # sentry_sdk.set_tags() after init.
                sentry_tags={"queue_name": settings.queue_name},
                # AsyncioIntegration is NOT in sentry-sdk's default integrations
                # set, so it has to be added explicitly. It catches unhandled
                # exceptions in background asyncio tasks — defense in depth in
                # case something escapes SentryMiddleware. The auto-enabled
                # LoggingIntegration handles ERROR-and-up log records, which is
                # how SentryMiddleware and the close-failure finally block emit
                # their events.
                sentry_integrations=[AsyncioIntegration()],
                # OpenTelemetry middleware class is always passed; lite-bootstrap
                # only initializes the tracer pipeline when an
                # ``opentelemetry_endpoint`` is configured (default: unset),
                # which is left to lite-bootstrap defaults — wire an OTLP
                # collector by adding the field to FastStreamConfig here when
                # one becomes available.
                opentelemetry_service_name="croupier",
                opentelemetry_middleware_cls=RabbitTelemetryMiddleware,
                # Prometheus middleware exports per-message counters/histograms
                # under the ``faststream`` namespace; mounted at ``/metrics``
                # by lite-bootstrap default.
                prometheus_middleware_cls=RabbitPrometheusMiddleware,
            )
        ).bootstrap()
    )
