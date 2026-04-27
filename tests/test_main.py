import logging
from typing import TYPE_CHECKING
from typing import Any
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
import sentry_sdk
from fastapi import FastAPI
from fastapi.testclient import TestClient
from faststream.exceptions import IgnoredException
from faststream.message import StreamMessage
from faststream.rabbit import TestRabbitBroker
from pydantic import AmqpDsn
from pydantic import HttpUrl
from pydantic import TypeAdapter
from pydantic import ValidationError
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.transport import Transport

from croupier.main import Message
from croupier.main import SentryDsn
from croupier.main import SentryEnvironment
from croupier.main import SentryMiddleware
from croupier.main import Settings
from croupier.main import handle_message
from croupier.main import health
from croupier.main import main
from croupier.main import router
from croupier.main import settings

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sentry_sdk.envelope import Envelope


@pytest.fixture
def sample_message() -> Message:
    return Message(
        content=b"\x1bt\x00Hello World!\x1bd\x06\x1dV\x00",
        network_host="192.168.1.100",
        network_timeout=10,
    )


class TestHandleMessageSubscriber:
    """Tests for handle_message as a RabbitMQ subscriber."""

    async def test_subscriber_receives_message(self, sample_message: Message) -> None:
        with patch("croupier.main.Network") as mock_network_cls:
            mock_network_cls.return_value = MagicMock()

            async with TestRabbitBroker(router.broker) as br:
                await br.publish(
                    sample_message,
                    queue=settings.queue_name,
                )
                handle_message.mock.assert_called_once()

    async def test_subscriber_opens_printer_connection(
        self, sample_message: Message
    ) -> None:
        with patch("croupier.main.Network") as mock_network_cls:
            mock_printer = MagicMock()
            mock_network_cls.return_value = mock_printer

            async with TestRabbitBroker(router.broker) as br:
                await br.publish(
                    sample_message,
                    queue=settings.queue_name,
                )

            mock_network_cls.assert_called_once_with(
                host="192.168.1.100",
                timeout=10,
            )
            mock_printer.open.assert_called_once()

    async def test_subscriber_sends_raw_content_to_printer(
        self, sample_message: Message
    ) -> None:
        with patch("croupier.main.Network") as mock_network_cls:
            mock_printer = MagicMock()
            mock_network_cls.return_value = mock_printer

            async with TestRabbitBroker(router.broker) as br:
                await br.publish(
                    sample_message,
                    queue=settings.queue_name,
                )

            mock_printer._raw.assert_called_once_with(
                b"\x1bt\x00Hello World!\x1bd\x06\x1dV\x00"
            )

    async def test_handler_sets_printer_id_tag_and_fingerprint(
        self, sample_message: Message
    ) -> None:
        fake_iso_scope = MagicMock()
        with (
            patch("croupier.main.Network") as mock_network_cls,
            patch("croupier.main.sentry_sdk.set_tag") as mock_set_tag,
            patch("croupier.main.sentry_sdk.set_context"),
            patch(
                "croupier.main.sentry_sdk.get_isolation_scope",
                return_value=fake_iso_scope,
            ),
        ):
            mock_network_cls.return_value = MagicMock()
            async with TestRabbitBroker(router.broker) as br:
                await br.publish(sample_message, queue=settings.queue_name)

        mock_set_tag.assert_any_call(
            "printer.id", f"{settings.queue_name}:192.168.1.100"
        )
        assert fake_iso_scope.fingerprint == ["{{ default }}", settings.queue_name]

    async def test_handler_sets_printer_context(self, sample_message: Message) -> None:
        with (
            patch("croupier.main.Network") as mock_network_cls,
            patch("croupier.main.sentry_sdk.set_context") as mock_set_context,
        ):
            mock_network_cls.return_value = MagicMock()
            async with TestRabbitBroker(router.broker) as br:
                await br.publish(sample_message, queue=settings.queue_name)

        mock_set_context.assert_any_call(
            "printer",
            {
                "host": "192.168.1.100",
                "timeout": 10,
                "payload_size": len(sample_message.content),
            },
        )

    async def test_handler_records_printer_breadcrumbs(
        self, sample_message: Message
    ) -> None:
        # Breadcrumbs make printer-failure events diagnosable: when _raw raises,
        # the captured event must show "open" (with host/port/timeout) followed
        # by "raw" (with byte count) so the chain "connected, then send failed"
        # is reconstructable from the Sentry UI alone.
        with (
            patch("croupier.main.Network") as mock_network_cls,
            patch("croupier.main.sentry_sdk.add_breadcrumb") as mock_breadcrumb,
        ):
            mock_network_cls.return_value = MagicMock()
            async with TestRabbitBroker(router.broker) as br:
                await br.publish(sample_message, queue=settings.queue_name)

        categories_messages = [
            (call.kwargs["category"], call.kwargs["message"])
            for call in mock_breadcrumb.call_args_list
        ]
        assert ("printer", "open") in categories_messages
        assert ("printer", "raw") in categories_messages
        open_call = next(
            c for c in mock_breadcrumb.call_args_list if c.kwargs["message"] == "open"
        )
        assert open_call.kwargs["data"] == {
            "host": "192.168.1.100",
            "port": 9100,
            "timeout": 10,
        }
        raw_call = next(
            c for c in mock_breadcrumb.call_args_list if c.kwargs["message"] == "raw"
        )
        assert raw_call.kwargs["data"] == {"bytes": len(sample_message.content)}

    async def test_subscriber_closes_printer_on_exception(
        self, sample_message: Message
    ) -> None:
        # DLQ-routing contract is covered by TestSentryMiddleware; TestRabbitBroker
        # does not model NACK/DLX semantics, so this test only pins the finally-block.
        with patch("croupier.main.Network") as mock_network_cls:
            mock_printer = MagicMock()
            mock_printer._raw.side_effect = RuntimeError("printer offline")
            mock_network_cls.return_value = mock_printer

            async with TestRabbitBroker(router.broker) as br:
                with pytest.raises(RuntimeError, match="printer offline"):
                    await br.publish(sample_message, queue=settings.queue_name)

            mock_printer.close.assert_called_once()

    async def test_subscriber_closes_printer_on_success(
        self, sample_message: Message
    ) -> None:
        with patch("croupier.main.Network") as mock_network_cls:
            mock_printer = MagicMock()
            mock_network_cls.return_value = mock_printer

            async with TestRabbitBroker(router.broker) as br:
                await br.publish(sample_message, queue=settings.queue_name)

            mock_printer.close.assert_called_once()

    async def test_subscriber_swallows_close_error_after_raw_failure(
        self, sample_message: Message
    ) -> None:
        # If _raw() raises and close() then also raises, the original _raw()
        # exception must propagate. close() failure is captured + logged, not
        # re-raised — otherwise DLQ/Sentry would key on the wrong error.
        with patch("croupier.main.Network") as mock_network_cls:
            mock_printer = MagicMock()
            mock_printer._raw.side_effect = RuntimeError("printer offline")
            mock_printer.close.side_effect = OSError("socket already closed")
            mock_network_cls.return_value = mock_printer

            async with TestRabbitBroker(router.broker) as br:
                with pytest.raises(RuntimeError, match="printer offline"):
                    await br.publish(sample_message, queue=settings.queue_name)

            mock_printer.close.assert_called_once()

    async def test_subscriber_skips_close_when_open_fails(
        self, sample_message: Message
    ) -> None:
        with patch("croupier.main.Network") as mock_network_cls:
            mock_printer = MagicMock()
            mock_printer.open.side_effect = ConnectionError("refused")
            mock_network_cls.return_value = mock_printer

            async with TestRabbitBroker(router.broker) as br:
                with pytest.raises(ConnectionError, match="refused"):
                    await br.publish(sample_message, queue=settings.queue_name)

            mock_printer._raw.assert_not_called()
            mock_printer.close.assert_not_called()


class TestSentryMiddleware:
    """Tests for the AMQP SentryMiddleware."""

    @pytest.fixture
    def stream_message(self) -> StreamMessage[bytes]:
        return MagicMock(spec=StreamMessage)

    async def test_captures_unhandled_exception_and_reraises(
        self, stream_message: StreamMessage[bytes]
    ) -> None:
        async def call_next(_msg: StreamMessage[bytes]) -> None:  # noqa: RUF029
            msg = "printer offline"
            raise RuntimeError(msg)

        middleware = SentryMiddleware(None, context=MagicMock())
        with (
            patch("croupier.main.sentry_sdk.capture_exception") as mock_capture,
            pytest.raises(RuntimeError, match="printer offline"),
        ):
            await middleware.consume_scope(call_next, stream_message)
        mock_capture.assert_called_once()

    async def test_skips_capture_for_ignored_exception(
        self, stream_message: StreamMessage[bytes]
    ) -> None:
        async def call_next(_msg: StreamMessage[bytes]) -> None:  # noqa: RUF029
            raise IgnoredException

        middleware = SentryMiddleware(None, context=MagicMock())
        with (
            patch("croupier.main.sentry_sdk.capture_exception") as mock_capture,
            pytest.raises(IgnoredException),
        ):
            await middleware.consume_scope(call_next, stream_message)
        mock_capture.assert_not_called()

    async def test_passes_through_when_no_exception(
        self, stream_message: StreamMessage[bytes]
    ) -> None:
        async def call_next(_msg: StreamMessage[bytes]) -> str:  # noqa: RUF029
            return "ok"

        middleware = SentryMiddleware(None, context=MagicMock())
        with patch("croupier.main.sentry_sdk.capture_exception") as mock_capture:
            result = await middleware.consume_scope(call_next, stream_message)
        assert result == "ok"
        mock_capture.assert_not_called()

    async def test_isolation_scope_does_not_leak_between_messages(
        self, stream_message: StreamMessage[bytes]
    ) -> None:
        outer_scope = sentry_sdk.get_isolation_scope()
        inner_scopes: list[sentry_sdk.Scope] = []

        async def cn_sets_tag(_msg: StreamMessage[bytes]) -> None:  # noqa: RUF029
            sentry_sdk.set_tag("scope_id", "first")
            inner_scopes.append(sentry_sdk.get_isolation_scope())

        async def cn_reads_tag(_msg: StreamMessage[bytes]) -> None:  # noqa: RUF029
            inner_scopes.append(sentry_sdk.get_isolation_scope())

        middleware = SentryMiddleware(None, context=MagicMock())
        await middleware.consume_scope(cn_sets_tag, stream_message)
        await middleware.consume_scope(cn_reads_tag, stream_message)

        assert inner_scopes[0] is not outer_scope
        assert inner_scopes[1] is not outer_scope
        assert inner_scopes[0] is not inner_scopes[1]
        assert sentry_sdk.get_isolation_scope() is outer_scope
        assert _scope_tags(inner_scopes[0]).get("scope_id") == "first"
        assert "scope_id" not in _scope_tags(inner_scopes[1])
        assert "scope_id" not in _scope_tags(outer_scope)

    async def test_tags_exception_class_before_capture(
        self, stream_message: StreamMessage[bytes]
    ) -> None:
        async def call_next(_msg: StreamMessage[bytes]) -> None:  # noqa: RUF029
            raise ValueError

        middleware = SentryMiddleware(None, context=MagicMock())
        with (
            patch("croupier.main.sentry_sdk.set_tag") as mock_set_tag,
            patch("croupier.main.sentry_sdk.capture_exception"),
            pytest.raises(ValueError),  # noqa: PT011
        ):
            await middleware.consume_scope(call_next, stream_message)
        mock_set_tag.assert_any_call("error.class", "ValueError")


def _scope_tags(scope: sentry_sdk.Scope) -> dict[str, object]:
    # Project the scope's tags via the public apply_to_event API instead of
    # reading scope._tags, which is internal to sentry-sdk and brittle to
    # release-to-release renames.
    event: Any = {}
    scope.apply_to_event(event, hint={})
    tags = event.get("tags")
    return tags if isinstance(tags, dict) else {}


def _build_settings(
    sentry_dsn: HttpUrl | None = None,
    sentry_environment: SentryEnvironment = "production",
) -> Settings:
    return Settings.model_construct(
        queue_url=AmqpDsn("amqp://guest:guest@127.0.0.1"),
        exchange_name="x",
        queue_name="test.queue",
        dlx_name="x.dlx",
        dlq_name="x.dlq",
        sentry_dsn=sentry_dsn,
        sentry_environment=sentry_environment,
    )


class TestSentryInit:
    """Tests for opt-in/opt-out Sentry initialization in main()."""

    @pytest.fixture(autouse=True)
    def _isolate_logger_handlers(self) -> Iterator[None]:
        # main() attaches handlers to the croupier/faststream/sentry_sdk.errors
        # loggers globally; without this snapshot, mock handlers from one test
        # leak into the next and crash logger.exception() with TypeError on
        # `record.levelno >= hdlr.level`.
        names = (
            "croupier",
            "faststream",
            "faststream.access.rabbit",
            "sentry_sdk.errors",
        )
        saved = {n: logging.getLogger(n).handlers[:] for n in names}
        yield
        for name, handlers in saved.items():
            logging.getLogger(name).handlers = handlers[:]

    def test_init_skipped_when_dsn_is_none(self) -> None:
        test_settings = _build_settings()
        with (
            patch("croupier.main.settings", test_settings),
            patch("croupier.main.sentry_sdk.init") as mock_init,
            patch("croupier.main.sentry_sdk.set_tag") as mock_set_tag,
            patch("croupier.main.uvicorn.run"),
            patch("croupier.main.RotatingFileHandler"),
            patch("croupier.main.logging.getLogger") as mock_get_logger,
        ):
            main()
            mock_init.assert_not_called()
            mock_set_tag.assert_not_called()
            for call in mock_get_logger.call_args_list:
                assert call.args[0] != "sentry_sdk.errors"

    def test_init_called_with_pii_safe_kwargs_when_dsn_set(self) -> None:
        test_settings = _build_settings(
            sentry_dsn=HttpUrl("https://k@o.ingest.sentry.io/1"),
            sentry_environment="production",
        )
        with (
            patch("croupier.main.settings", test_settings),
            patch("croupier.main.sentry_sdk.init") as mock_init,
            patch("croupier.main.sentry_sdk.set_tag") as mock_set_tag,
            patch("croupier.main.uvicorn.run"),
            patch("croupier.main.RotatingFileHandler"),
        ):
            main()
            mock_init.assert_called_once()
            kwargs = mock_init.call_args.kwargs
            assert kwargs["dsn"] == "https://k@o.ingest.sentry.io/1"
            assert kwargs["send_default_pii"] is False
            assert kwargs["include_local_variables"] is False
            assert kwargs["attach_stacktrace"] is True
            assert kwargs["environment"] == "production"
            mock_set_tag.assert_called_with("queue_name", "test.queue")

    def test_init_registers_expected_integrations(self) -> None:
        test_settings = _build_settings(
            sentry_dsn=HttpUrl("https://k@o.ingest.sentry.io/1"),
        )
        with (
            patch("croupier.main.settings", test_settings),
            patch("croupier.main.sentry_sdk.init") as mock_init,
            patch("croupier.main.sentry_sdk.set_tag"),
            patch("croupier.main.uvicorn.run"),
            patch("croupier.main.RotatingFileHandler"),
        ):
            main()
            integrations = mock_init.call_args.kwargs["integrations"]
            integration_types = {type(i).__name__ for i in integrations}
            assert "FastApiIntegration" in integration_types
            assert "StarletteIntegration" in integration_types
            assert "AsyncioIntegration" in integration_types
            assert "LoggingIntegration" in integration_types

    def test_logging_integration_silences_event_level(self) -> None:
        # Prevent double-firing: explicit capture_exception() in handle_message
        # plus LoggingIntegration default ERROR-as-event would emit two events
        # for one underlying error. event_level=CRITICAL silences auto-events;
        # INFO-level breadcrumbs are still emitted.
        test_settings = _build_settings(
            sentry_dsn=HttpUrl("https://k@o.ingest.sentry.io/1"),
        )
        with (
            patch("croupier.main.settings", test_settings),
            patch("croupier.main.sentry_sdk.init") as mock_init,
            patch("croupier.main.sentry_sdk.set_tag"),
            patch("croupier.main.uvicorn.run"),
            patch("croupier.main.RotatingFileHandler"),
        ):
            main()
            integrations = mock_init.call_args.kwargs["integrations"]
            logging_integration = next(
                i for i in integrations if type(i).__name__ == "LoggingIntegration"
            )
            assert logging_integration._handler.level == logging.CRITICAL
            assert logging_integration._breadcrumb_handler.level == logging.INFO

    def test_handler_attached_before_sentry_init(self) -> None:
        test_settings = _build_settings(
            sentry_dsn=HttpUrl("https://k@o.ingest.sentry.io/1"),
        )
        order: list[str] = []

        def _record_init(**_: object) -> None:
            order.append("init")

        def _record_handler(**_: object) -> MagicMock:
            order.append("handler")
            return MagicMock()

        with (
            patch("croupier.main.settings", test_settings),
            patch("croupier.main.sentry_sdk.init", side_effect=_record_init),
            patch("croupier.main.sentry_sdk.set_tag"),
            patch("croupier.main.uvicorn.run"),
            patch(
                "croupier.main.RotatingFileHandler",
                side_effect=_record_handler,
            ),
        ):
            main()

        assert order.index("handler") < order.index("init")

    def test_init_failure_does_not_crash_service(self) -> None:
        test_settings = _build_settings(
            sentry_dsn=HttpUrl("https://k@o.ingest.sentry.io/1"),
        )
        with (
            patch("croupier.main.settings", test_settings),
            patch(
                "croupier.main.sentry_sdk.init",
                side_effect=RuntimeError("DSN parse failed"),
            ),
            patch("croupier.main.sentry_sdk.set_tag") as mock_set_tag,
            patch("croupier.main.uvicorn.run") as mock_run,
            patch(
                "croupier.main.RotatingFileHandler",
                return_value=MagicMock(level=logging.NOTSET),
            ),
        ):
            main()
            mock_run.assert_called_once()
            mock_set_tag.assert_not_called()

    def test_handler_attached_before_sentry_init_even_when_init_raises(
        self,
    ) -> None:
        # Init failure must not bypass the file-handler attach: operators rely
        # on ~/.croupier.log to surface the very init failure that disabled
        # Sentry.
        test_settings = _build_settings(
            sentry_dsn=HttpUrl("https://k@o.ingest.sentry.io/1"),
        )
        order: list[str] = []

        def _record_init(**_: object) -> None:
            order.append("init")
            raise RuntimeError

        def _record_handler(**_: object) -> MagicMock:
            order.append("handler")
            return MagicMock(level=logging.NOTSET)

        with (
            patch("croupier.main.settings", test_settings),
            patch("croupier.main.sentry_sdk.init", side_effect=_record_init),
            patch("croupier.main.sentry_sdk.set_tag"),
            patch("croupier.main.uvicorn.run"),
            patch(
                "croupier.main.RotatingFileHandler",
                side_effect=_record_handler,
            ),
        ):
            main()

        assert order.index("handler") < order.index("init")


class TestRouterMiddlewareWiring:
    """Tests for conditional SentryMiddleware registration on the router."""

    @pytest.mark.parametrize(
        ("dsn", "expected"),
        [
            (None, ()),
            (HttpUrl("https://k@o.ingest.sentry.io/1"), (SentryMiddleware,)),
        ],
    )
    def test_middleware_tuple_reflects_sentry_dsn(
        self,
        dsn: HttpUrl | None,
        expected: tuple[type[SentryMiddleware], ...],
    ) -> None:
        s = _build_settings(sentry_dsn=dsn)
        actual = (SentryMiddleware,) if s.sentry_dsn is not None else ()
        assert actual == expected


class TestSentryDsnValidator:
    """Tests for the AfterValidator that rejects malformed Sentry DSNs."""

    @pytest.fixture
    def adapter(self) -> TypeAdapter[SentryDsn]:
        return TypeAdapter(SentryDsn)

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/1",
            "https://example.com/path/1",
        ],
    )
    def test_rejects_dsn_without_userinfo(
        self, adapter: TypeAdapter[SentryDsn], url: str
    ) -> None:
        with pytest.raises(ValidationError, match="public key"):
            adapter.validate_python(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://k@example.com",
            "https://k@example.com/",
        ],
    )
    def test_rejects_dsn_without_project_id(
        self, adapter: TypeAdapter[SentryDsn], url: str
    ) -> None:
        with pytest.raises(ValidationError, match="project id"):
            adapter.validate_python(url)

    def test_accepts_realistic_dsn(self, adapter: TypeAdapter[SentryDsn]) -> None:
        url = adapter.validate_python("https://abc123@o42.ingest.sentry.io/4500")
        assert url.username == "abc123"
        assert url.path == "/4500"


class TestHandleMessageHTTP:
    """Tests for handle_message exposed as a FastAPI route."""

    def _make_client(self) -> TestClient:
        app = FastAPI()
        app.include_router(router)
        return TestClient(app, raise_server_exceptions=False)

    def test_http_route_returns_500_on_handler_exception(
        self, sample_message: Message
    ) -> None:
        with patch("croupier.main.Network") as mock_network_cls:
            mock_printer = MagicMock()
            mock_printer._raw.side_effect = RuntimeError("printer offline")
            mock_network_cls.return_value = mock_printer

            client = self._make_client()
            response = client.post(
                "/handle-message",
                content=sample_message.model_dump_json(),
                headers={"Content-Type": "application/json"},
            )

        assert response.status_code == 500
        mock_printer.close.assert_called_once()

    def test_http_route_emits_sentry_event_via_fastapi_integration(
        self, sample_message: Message
    ) -> None:
        # Pins the README claim: unhandled errors on the HTTP path are captured
        # via FastApiIntegration. A 500 response alone would pass without Sentry
        # wired at all — this asserts an event actually reaches the transport.
        captured: list[dict[str, object]] = []

        class _RecordingTransport(Transport):
            def capture_envelope(self, envelope: Envelope) -> None:
                for item in envelope.items:
                    payload = item.payload.json
                    if item.headers.get("type") == "event" and payload is not None:
                        captured.append(dict(payload))

        sentry_sdk.init(
            dsn="https://public@example.com/1",
            transport=_RecordingTransport(),
            integrations=[FastApiIntegration(), StarletteIntegration()],
            default_integrations=False,
        )
        try:
            with patch("croupier.main.Network") as mock_network_cls:
                mock_printer = MagicMock()
                mock_printer._raw.side_effect = RuntimeError("printer offline")
                mock_network_cls.return_value = mock_printer

                client = self._make_client()
                response = client.post(
                    "/handle-message",
                    content=sample_message.model_dump_json(),
                    headers={"Content-Type": "application/json"},
                )
            assert response.status_code == 500
            assert captured, "Sentry transport received no event"
        finally:
            sentry_sdk.init(dsn=None)


class TestHealthEndpoint:
    """Tests for GET / health check."""

    async def test_returns_204_when_broker_is_healthy(self) -> None:
        mock_request = MagicMock()
        mock_request.state.broker.ping = AsyncMock(return_value=True)

        response = await health(mock_request)
        assert response.status_code == 204

    async def test_returns_500_when_broker_is_unhealthy(self) -> None:
        mock_request = MagicMock()
        mock_request.state.broker.ping = AsyncMock(return_value=False)

        response = await health(mock_request)
        assert response.status_code == 500
