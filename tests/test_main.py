import logging
from typing import TYPE_CHECKING
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
import sentry_sdk
from faststream.exceptions import IgnoredException
from faststream.message import StreamMessage
from faststream.rabbit import RabbitBroker
from faststream.rabbit import RabbitQueue
from faststream.rabbit import TestRabbitBroker
from pydantic import ValidationError as PydanticValidationError
from sentry_sdk.client import NonRecordingClient
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.transport import Transport

from croupier.main import Message
from croupier.main import SentryMiddleware
from croupier.main import broker
from croupier.main import handle_message
from croupier.main import settings

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sentry_sdk.envelope import Envelope


class _RecordingTransport(Transport):
    """Sentry transport that captures events into a list for in-process assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        for item in envelope.items:
            payload = item.payload.json
            if item.headers.get("type") == "event" and payload is not None:
                self.events.append(dict(payload))


@pytest.fixture
def recording_transport() -> Iterator[_RecordingTransport]:
    """Initialize a real Sentry SDK with a recording transport for the test only.

    Tear down by replacing the global client with a ``NonRecordingClient`` —
    ``sentry_sdk.init(dsn=None)`` does NOT deactivate the client in
    sentry-sdk 2.x, so subsequent tests would still see ``is_active() is True``
    and the production short-circuit path could not be exercised.

    Also resets the global isolation scope so fingerprint/tag mutations
    written by one test do not leak into the next.
    """
    transport = _RecordingTransport()
    sentry_sdk.init(
        dsn="https://public@example.com/1",
        transport=transport,
        default_integrations=False,
        # Auto-promotion of logger.exception is the production capture path
        # since SentryMiddleware no longer calls capture_exception() directly.
        # Mirror that here so event-payload assertions reflect prod behavior.
        integrations=[
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR)
        ],
    )
    try:
        yield transport
    finally:
        sentry_sdk.get_global_scope().set_client(NonRecordingClient())
        sentry_sdk.get_isolation_scope().clear()
        sentry_sdk.get_global_scope().clear()


@pytest.fixture
def active_sentry() -> Iterator[None]:
    """Activate Sentry with a no-op transport for tests that exercise the
    capture path but do not need to inspect outgoing events.

    Pairs with ``_reset_sentry_global_state`` (autouse) — that fixture
    deactivates by default, so any middleware test exercising the
    ``is_active()`` short-circuit's *non-skipped* branch must opt back in.
    """
    sentry_sdk.init(
        dsn="https://public@example.com/1",
        transport=_RecordingTransport(),
        default_integrations=False,
    )
    try:
        yield
    finally:
        sentry_sdk.get_global_scope().set_client(NonRecordingClient())
        sentry_sdk.get_isolation_scope().clear()
        sentry_sdk.get_global_scope().clear()


@pytest.fixture(autouse=True)
def _reset_sentry_global_state() -> Iterator[None]:
    """Ensure every test starts with a NonRecordingClient and a clean scope.

    The bootstrapped app at module import does not initialize Sentry (test
    config has ``sentry_dsn=null``), but cross-test mutation of the global
    isolation scope (e.g. fingerprint set during a previous ``handle_message``
    call) would still bleed into later assertions.
    """
    sentry_sdk.get_global_scope().set_client(NonRecordingClient())
    sentry_sdk.get_isolation_scope().clear()
    sentry_sdk.get_global_scope().clear()
    yield
    sentry_sdk.get_global_scope().set_client(NonRecordingClient())
    sentry_sdk.get_isolation_scope().clear()
    sentry_sdk.get_global_scope().clear()


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

            async with TestRabbitBroker(broker) as br:
                await br.publish(sample_message, queue=settings.queue_name)
                handle_message.mock.assert_called_once()

    async def test_subscriber_opens_printer_connection(
        self, sample_message: Message
    ) -> None:
        with patch("croupier.main.Network") as mock_network_cls:
            mock_printer = MagicMock()
            mock_network_cls.return_value = mock_printer

            async with TestRabbitBroker(broker) as br:
                await br.publish(sample_message, queue=settings.queue_name)

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

            async with TestRabbitBroker(broker) as br:
                await br.publish(sample_message, queue=settings.queue_name)

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
            async with TestRabbitBroker(broker) as br:
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
            async with TestRabbitBroker(broker) as br:
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
            mock_printer = MagicMock()
            # Breadcrumb pulls the port off the Network instance; mock it
            # explicitly so the breadcrumb data assertion stays exact.
            mock_printer.port = 9100
            mock_network_cls.return_value = mock_printer
            async with TestRabbitBroker(broker) as br:
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
        # DLQ-routing contract is covered by TestDlqPropagationThroughMiddleware;
        # TestRabbitBroker does not model NACK/DLX semantics, so this test only
        # pins the finally-block.
        with patch("croupier.main.Network") as mock_network_cls:
            mock_printer = MagicMock()
            mock_printer._raw.side_effect = RuntimeError("printer offline")
            mock_network_cls.return_value = mock_printer

            async with TestRabbitBroker(broker) as br:
                with pytest.raises(RuntimeError, match="printer offline"):
                    await br.publish(sample_message, queue=settings.queue_name)

            mock_printer.close.assert_called_once()

    async def test_subscriber_closes_printer_on_success(
        self, sample_message: Message
    ) -> None:
        with patch("croupier.main.Network") as mock_network_cls:
            mock_printer = MagicMock()
            mock_network_cls.return_value = mock_printer

            async with TestRabbitBroker(broker) as br:
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

            async with TestRabbitBroker(broker) as br:
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

            async with TestRabbitBroker(broker) as br:
                with pytest.raises(ConnectionError, match="refused"):
                    await br.publish(sample_message, queue=settings.queue_name)

            mock_printer._raw.assert_not_called()
            mock_printer.close.assert_not_called()

    async def test_close_failure_logs_separate_error(
        self,
        sample_message: Message,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # README: "the close failure is captured as a separate Sentry event".
        # The auto-enabled LoggingIntegration promotes ``logger.exception``
        # records to standalone Sentry events. With Sentry inactive in the
        # test config, ``logger.exception`` still lands in the log stream;
        # asserting that the ``"printer close failed"`` record exists pins
        # the contract that the close-failure path emits a distinct signal.
        with patch("croupier.main.Network") as mock_network_cls:
            mock_printer = MagicMock()
            mock_printer._raw.side_effect = RuntimeError("printer offline")
            mock_printer.close.side_effect = OSError("socket already closed")
            mock_network_cls.return_value = mock_printer

            with caplog.at_level(logging.ERROR, logger="croupier.main"):
                async with TestRabbitBroker(broker) as br:
                    with pytest.raises(RuntimeError, match="printer offline"):
                        await br.publish(sample_message, queue=settings.queue_name)

            assert any(
                "printer close failed" in rec.message
                and rec.exc_info is not None
                and rec.exc_info[0] is OSError
                for rec in caplog.records
            )


class TestSentryMiddleware:
    """Tests for the AMQP SentryMiddleware.

    All tests in this class use ``active_sentry`` (autouse) — middleware
    short-circuits via ``is_active()`` when Sentry is not initialized, and
    the global ``_reset_sentry_global_state`` fixture deactivates by default.
    """

    @pytest.fixture(autouse=True)
    def _activate(self, active_sentry: None) -> None:
        _ = active_sentry

    @pytest.fixture
    def stream_message(self) -> StreamMessage[bytes]:
        return MagicMock(spec=StreamMessage)

    async def test_logs_exception_and_reraises(
        self,
        stream_message: StreamMessage[bytes],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The auto-enabled LoggingIntegration promotes ERROR-level records to
        # Sentry events; verifying the log record proves both that the path
        # is reachable and that Sentry would receive an event.
        async def call_next(_msg: StreamMessage[bytes]) -> None:  # noqa: RUF029
            msg = "printer offline"
            raise RuntimeError(msg)

        middleware = SentryMiddleware(None, context=MagicMock())
        with (
            caplog.at_level(logging.ERROR, logger="croupier.main"),
            pytest.raises(RuntimeError, match="printer offline"),
        ):
            await middleware.consume_scope(call_next, stream_message)
        assert any(
            "message handler failed" in rec.message and rec.exc_info is not None
            for rec in caplog.records
        )

    async def test_skips_log_for_ignored_exception(
        self,
        stream_message: StreamMessage[bytes],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        async def call_next(_msg: StreamMessage[bytes]) -> None:  # noqa: RUF029
            raise IgnoredException

        middleware = SentryMiddleware(None, context=MagicMock())
        with (
            caplog.at_level(logging.ERROR, logger="croupier.main"),
            pytest.raises(IgnoredException),
        ):
            await middleware.consume_scope(call_next, stream_message)
        assert not any(
            "message handler failed" in rec.message for rec in caplog.records
        )

    async def test_passes_through_when_no_exception(
        self, stream_message: StreamMessage[bytes]
    ) -> None:
        async def call_next(_msg: StreamMessage[bytes]) -> str:  # noqa: RUF029
            return "ok"

        middleware = SentryMiddleware(None, context=MagicMock())
        result = await middleware.consume_scope(call_next, stream_message)
        assert result == "ok"

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

    async def test_tags_exception_class_before_log(
        self, stream_message: StreamMessage[bytes]
    ) -> None:
        async def call_next(_msg: StreamMessage[bytes]) -> None:  # noqa: RUF029
            raise ValueError

        middleware = SentryMiddleware(None, context=MagicMock())
        with (
            patch("croupier.main.sentry_sdk.set_tag") as mock_set_tag,
            pytest.raises(ValueError),  # noqa: PT011
        ):
            await middleware.consume_scope(call_next, stream_message)
        mock_set_tag.assert_any_call("error.class", "ValueError")

    async def test_event_payload_carries_error_class_tag(
        self,
        stream_message: StreamMessage[bytes],
        recording_transport: _RecordingTransport,
    ) -> None:
        # Pin actual event payload, not just the set_tag call args.
        async def call_next(_msg: StreamMessage[bytes]) -> None:  # noqa: RUF029
            msg = "printer offline"
            raise RuntimeError(msg)

        middleware = SentryMiddleware(None, context=MagicMock())
        with pytest.raises(RuntimeError, match="printer offline"):
            await middleware.consume_scope(call_next, stream_message)
        sentry_sdk.flush(timeout=2)

        assert recording_transport.events, "no event reached transport"
        tags = recording_transport.events[0].get("tags", {})
        assert tags.get("error.class") == "RuntimeError"

    async def test_payload_decode_error_uses_default_fingerprint(
        self,
        stream_message: StreamMessage[bytes],
        recording_transport: _RecordingTransport,
    ) -> None:
        # Errors raised before handle_message runs (e.g. Pydantic ValidationError
        # from a malformed AMQP body) must NOT carry the queue_name fingerprint
        # seed — handle_message is where that fingerprint is set, and decode
        # errors never reach handle_message.
        async def call_next(_msg: StreamMessage[bytes]) -> None:  # noqa: RUF029
            Message.model_validate_json(b"{}")

        middleware = SentryMiddleware(None, context=MagicMock())
        with pytest.raises(PydanticValidationError):
            await middleware.consume_scope(call_next, stream_message)
        sentry_sdk.flush(timeout=2)

        assert recording_transport.events, "no event reached transport"
        fingerprint = recording_transport.events[0].get("fingerprint", [])
        assert settings.queue_name not in fingerprint


def _scope_tags(scope: sentry_sdk.Scope) -> dict[str, object]:
    # Project the scope's tags via the public apply_to_event API instead of
    # reading scope._tags, which is internal to sentry-sdk and brittle to
    # release-to-release renames.
    event: Any = {}
    scope.apply_to_event(event, hint={})
    tags = event.get("tags")
    return tags if isinstance(tags, dict) else {}


class TestBrokerMiddlewareWiring:
    """Tests for conditional SentryMiddleware registration on the broker."""

    def test_broker_middleware_state_matches_dsn(self) -> None:
        # The conftest test config sets sentry_dsn=null by default, so the
        # broker was constructed at import without SentryMiddleware. Inspect
        # the actual broker middleware list (an entry can be either the class
        # itself when used as a factory, or an instance) rather than rebuilding
        # the tuple — that bypasses the previous tautology version.
        # Cast to Any so the type checker does not treat the SentryMiddleware
        # comparison as non-overlapping — at runtime the broker keeps either
        # the middleware class (factory) or an instance.
        registered: tuple[Any, ...] = tuple(broker.middlewares)
        registered_has_sentry = any(
            m is SentryMiddleware or isinstance(m, SentryMiddleware) for m in registered
        )
        if settings.sentry_dsn is None:
            assert not registered_has_sentry
        else:
            assert registered_has_sentry

    async def test_middleware_short_circuits_when_sentry_inactive(self) -> None:
        # Even when SentryMiddleware is wired into the broker (because
        # sentry_dsn was set in config), it must not open isolation_scope or
        # call capture when sentry_sdk init never succeeded — covered by the
        # is_active() check at the top of consume_scope.
        assert sentry_sdk.get_client().is_active() is False
        mw = SentryMiddleware(None, context=MagicMock())

        async def call_next(_msg: StreamMessage[bytes]) -> str:  # noqa: RUF029
            return "passthrough"

        with (
            patch("croupier.main.sentry_sdk.isolation_scope") as mock_scope,
            patch("croupier.main.sentry_sdk.capture_exception") as mock_capture,
        ):
            result = await mw.consume_scope(call_next, MagicMock(spec=StreamMessage))
        assert result == "passthrough"
        mock_scope.assert_not_called()
        mock_capture.assert_not_called()


class TestDlqPropagationThroughMiddleware:
    """Lock the regression fixed by commit 7b2d72c.

    SentryMiddleware must re-raise unhandled exceptions so FastStream's NACK
    machinery sees them — without re-raise, faststream's __aexit__ semantics
    silently ACK the message and the DLQ never sees it. TestRabbitBroker does
    not model NACK/DLX, but it does run the full middleware chain. A test
    that publishes through a broker with SentryMiddleware wired in and
    asserts the exception escapes the whole chain pins this contract.
    """

    async def test_exception_propagates_through_broker_with_middleware(
        self, sample_message: Message, recording_transport: _RecordingTransport
    ) -> None:
        # Build a RabbitBroker that mirrors the production wiring (middleware
        # registered) but uses a local handler that raises. Cannot use the
        # module-level `broker` because it is constructed at import time
        # without SentryMiddleware (conftest sets sentry_dsn=null).
        local_broker = RabbitBroker(
            settings.queue_url.unicode_string(),
            middlewares=(SentryMiddleware,),
        )

        @local_broker.subscriber(
            queue=RabbitQueue(name=settings.queue_name, declare=False),
        )
        async def _local_handle(body: Message) -> None:  # noqa: ARG001, RUF029
            msg = "printer offline"
            raise RuntimeError(msg)

        async with TestRabbitBroker(local_broker) as br:
            with pytest.raises(RuntimeError, match="printer offline"):
                await br.publish(sample_message, queue=settings.queue_name)

        sentry_sdk.flush(timeout=2)
        assert recording_transport.events, "middleware did not capture event"
