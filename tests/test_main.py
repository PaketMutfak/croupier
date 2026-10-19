import logging
from typing import TYPE_CHECKING
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
import sentry_sdk
from escpos.printer import Dummy
from faststream import AckPolicy
from faststream.asgi import AsgiFastStream
from faststream.exceptions import IgnoredException
from faststream.message import StreamMessage
from faststream.rabbit import RabbitBroker
from faststream.rabbit import RabbitQueue
from faststream.rabbit import TestRabbitBroker
from sentry_sdk.client import NonRecordingClient
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.transport import Transport

from croupier.main import ENABLE_EXTENDED_FLAGS
from croupier.main import ENABLE_QUICK_EDIT_MODE
from croupier.main import ERROR_INVALID_HANDLE
from croupier.main import INVALID_HANDLE_VALUE
from croupier.main import SentryMiddleware
from croupier.main import build_broker
from croupier.main import create_app
from croupier.main import disable_console_quick_edit
from croupier.main import restore_console_mode

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sentry_sdk.envelope import Envelope

    from croupier.printing import Message
    from croupier.settings import Settings


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
    sentry-sdk 2.x, so subsequent tests would still see ``is_initialized() is True``
    and the production short-circuit path could not be exercised.

    Also resets the global isolation scope so tag mutations
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
    ``is_initialized()`` short-circuit's *non-skipped* branch must opt back in.
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
    isolation scope (e.g. tags set during a previous ``handle_message`` call)
    would still bleed into later assertions.
    """
    sentry_sdk.get_global_scope().set_client(NonRecordingClient())
    sentry_sdk.get_isolation_scope().clear()
    sentry_sdk.get_global_scope().clear()
    yield
    sentry_sdk.get_global_scope().set_client(NonRecordingClient())
    sentry_sdk.get_isolation_scope().clear()
    sentry_sdk.get_global_scope().clear()


class TestSubscriber:
    async def test_prints_message_content(
        self,
        broker: RabbitBroker,
        settings: Settings,
        printer: Dummy,
        sample_message: Message,
    ) -> None:
        async with TestRabbitBroker(broker) as br:
            await br.publish(sample_message, queue=settings.queue_name)

        assert printer.output == sample_message.content

    async def test_closes_printer_after_printing(
        self,
        settings: Settings,
        sample_message: Message,
    ) -> None:
        printer = Dummy()
        closed: list[bool] = []
        printer.close = lambda: closed.append(True)
        broker = build_broker(settings, connect=lambda _m: printer)

        async with TestRabbitBroker(broker) as br:
            await br.publish(sample_message, queue=settings.queue_name)

        assert closed == [True]


class TestSubscriberFailure:
    async def test_printer_error_reaches_the_broker_so_message_is_rejected(
        self,
        settings: Settings,
        sample_message: Message,
    ) -> None:
        printer = Dummy()
        broker = build_broker(settings, connect=lambda _m: printer)

        with patch.object(printer, "_raw", side_effect=OSError("printer offline")):
            async with TestRabbitBroker(broker) as br:
                with pytest.raises(OSError, match="printer offline"):
                    await br.publish(sample_message, queue=settings.queue_name)

    async def test_subscriber_rejects_on_error_and_never_declares_the_queue(
        self,
        broker: RabbitBroker,
        settings: Settings,
    ) -> None:
        [subscriber] = broker.subscribers

        assert subscriber.ack_policy is AckPolicy.REJECT_ON_ERROR
        assert subscriber.queue.name == settings.queue_name
        assert subscriber.queue.declare is False


class TestSentryMiddleware:
    """Tests for the AMQP SentryMiddleware.

    All tests in this class use ``active_sentry`` (autouse) — middleware
    short-circuits via ``is_initialized()`` when Sentry is not initialized, and
    the global ``_reset_sentry_global_state`` fixture deactivates by default.
    """

    @pytest.fixture(autouse=True)
    def _activate(self, active_sentry: None) -> None:
        _ = active_sentry

    @pytest.fixture
    def stream_message(self) -> StreamMessage[bytes]:
        # ``correlation_id`` is set on the StreamMessage instance (not as a
        # class attribute), so ``MagicMock(spec=...)`` does not expose it.
        # Pin a concrete value because SentryMiddleware reads it for the
        # per-message tag.
        msg = MagicMock(spec=StreamMessage)
        msg.correlation_id = "test-correlation-id"
        return msg

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

    async def test_event_payload_carries_native_exception_type(
        self,
        stream_message: StreamMessage[bytes],
        recording_transport: _RecordingTransport,
    ) -> None:
        # Sentry's built-in ``error.type`` index covers exception-class
        # filtering, so the middleware no longer sets a custom tag — the
        # auto-promoted event still carries the exception type natively under
        # ``exception.values[].type``.
        async def call_next(_msg: StreamMessage[bytes]) -> None:  # noqa: RUF029
            msg = "printer offline"
            raise RuntimeError(msg)

        middleware = SentryMiddleware(None, context=MagicMock())
        with pytest.raises(RuntimeError, match="printer offline"):
            await middleware.consume_scope(call_next, stream_message)
        sentry_sdk.flush(timeout=2)

        assert recording_transport.events, "no event reached transport"
        exception_values = (
            recording_transport.events[0].get("exception", {}).get("values", [])
        )
        assert exception_values, "event missing exception payload"
        assert exception_values[0].get("type") == "RuntimeError"

    async def test_event_payload_carries_correlation_id_tag(
        self,
        stream_message: StreamMessage[bytes],
        recording_transport: _RecordingTransport,
    ) -> None:
        # The middleware tags the per-message isolation scope with
        # ``correlation_id`` so a Sentry event can be pivoted from a log line
        # carrying the same field in one filter.
        async def call_next(_msg: StreamMessage[bytes]) -> None:  # noqa: RUF029
            msg = "printer offline"
            raise RuntimeError(msg)

        middleware = SentryMiddleware(None, context=MagicMock())
        with pytest.raises(RuntimeError, match="printer offline"):
            await middleware.consume_scope(call_next, stream_message)
        sentry_sdk.flush(timeout=2)

        assert recording_transport.events, "no event reached transport"
        tags: Any = recording_transport.events[0].get("tags") or {}
        # ``tags`` may be a dict or a list of [key, value] pairs depending on
        # SDK version — normalize before asserting.
        if isinstance(tags, list):
            tags = dict(tags)
        assert tags.get("correlation_id") == "test-correlation-id"


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

    @staticmethod
    def _has_sentry(broker: RabbitBroker) -> bool:
        # An entry can be either the class itself (factory) or an instance.
        registered: tuple[Any, ...] = tuple(broker.middlewares)
        return any(
            m is SentryMiddleware or isinstance(m, SentryMiddleware) for m in registered
        )

    def test_not_registered_without_dsn(self, settings: Settings) -> None:
        assert settings.sentry_dsn is None
        assert not self._has_sentry(build_broker(settings))

    def test_registered_with_dsn(self, settings: Settings) -> None:
        with_dsn = settings.model_copy(
            update={"sentry_dsn": "https://public@example.com/1"}
        )
        assert self._has_sentry(build_broker(with_dsn))

    async def test_middleware_short_circuits_when_sentry_inactive(self) -> None:
        # Even when SentryMiddleware is wired into the broker (because
        # sentry_dsn was set in config), it must not open isolation_scope or
        # call capture when sentry_sdk init never succeeded — covered by the
        # is_initialized() check at the top of consume_scope.
        assert sentry_sdk.is_initialized() is False
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


class TestCreateApp:
    """Smoke-test the bootstrap wiring."""

    def test_create_app_returns_asgi_faststream(self, settings: Settings) -> None:
        app = create_app(settings)
        assert isinstance(app, AsgiFastStream)


class TestDlqPropagationThroughMiddleware:
    """SentryMiddleware must re-raise unhandled exceptions.

    Without re-raise, FastStream's ``__aexit__`` semantics silently ACK the
    message and the DLQ never sees it. TestRabbitBroker does not model
    reject/DLX, but it does run the full middleware chain, so asserting the
    exception escapes pins the contract.
    """

    async def test_exception_propagates_through_broker_with_middleware(
        self,
        settings: Settings,
        sample_message: Message,
        recording_transport: _RecordingTransport,
    ) -> None:
        with_dsn = settings.model_copy(
            update={"sentry_dsn": "https://public@example.com/1"}
        )
        printer = Dummy()
        with patch.object(printer, "_raw", side_effect=OSError("printer offline")):
            broker = build_broker(with_dsn, connect=lambda _m: printer)
            async with TestRabbitBroker(broker) as br:
                with pytest.raises(OSError, match="printer offline"):
                    await br.publish(sample_message, queue=settings.queue_name)

        sentry_sdk.flush(timeout=2)
        assert recording_transport.events, "middleware did not capture event"


class TestSentryNoiseControl:
    def test_faststream_logger_errors_do_not_become_events(
        self,
        recording_transport: _RecordingTransport,
    ) -> None:
        logging.getLogger("faststream").error("Processing error")
        sentry_sdk.flush(timeout=2)
        assert recording_transport.events == []

    def test_other_logger_errors_still_become_events(
        self,
        recording_transport: _RecordingTransport,
    ) -> None:
        logging.getLogger("croupier.main").error("still captured")
        sentry_sdk.flush(timeout=2)
        assert len(recording_transport.events) == 1


class TestDisableConsoleQuickEdit:
    """Tests for disable_console_quick_edit."""

    @staticmethod
    def _kernel32(*, handle: int | None = 1234, mode: int = 0x00F7) -> MagicMock:
        kernel32 = MagicMock()
        kernel32.CreateFileW.return_value = handle

        def fake_get_console_mode(_handle: int, mode_ref: MagicMock) -> int:
            mode_ref.value = mode
            return 1

        kernel32.GetConsoleMode.side_effect = fake_get_console_mode
        kernel32.SetConsoleMode.return_value = 1
        kernel32.CloseHandle.return_value = 1
        return kernel32

    def _run(
        self, kernel32: MagicMock, *, last_error: int = 5
    ) -> tuple[MagicMock, MagicMock]:
        with (
            patch("croupier.main.sys.platform", "win32"),
            patch("croupier.main.ctypes") as mock_ctypes,
            patch("croupier.main.atexit") as mock_atexit,
            patch("croupier.main.logger") as mock_logger,
        ):
            mock_ctypes.WinDLL.return_value = kernel32
            mock_ctypes.c_uint32.return_value = MagicMock()
            mock_ctypes.byref.side_effect = lambda x: x
            mock_ctypes.get_last_error.return_value = last_error
            disable_console_quick_edit()
        return mock_atexit, mock_logger

    def test_is_noop_on_non_windows(self) -> None:
        with (
            patch("croupier.main.sys.platform", "linux"),
            patch("croupier.main.ctypes") as mock_ctypes,
        ):
            disable_console_quick_edit()
        mock_ctypes.WinDLL.assert_not_called()

    def test_clears_quick_edit_flag_on_windows(self) -> None:
        kernel32 = self._kernel32()
        self._run(kernel32)

        new_mode = kernel32.SetConsoleMode.call_args.args[1]
        assert not new_mode & ENABLE_QUICK_EDIT_MODE
        assert new_mode & ENABLE_EXTENDED_FLAGS

    def test_opens_conin_instead_of_std_handle(self) -> None:
        kernel32 = self._kernel32()
        self._run(kernel32)

        assert kernel32.CreateFileW.call_args.args[0] == "CONIN$"
        kernel32.CloseHandle.assert_called_once_with(1234)

    def test_registers_restore_of_original_mode(self) -> None:
        kernel32 = self._kernel32(mode=0x00F7)
        mock_atexit, _ = self._run(kernel32)

        mock_atexit.register.assert_called_once_with(restore_console_mode, 0x00F7)

    def test_restore_resets_original_mode(self) -> None:
        kernel32 = self._kernel32()
        with (
            patch("croupier.main.ctypes") as mock_ctypes,
        ):
            mock_ctypes.WinDLL.return_value = kernel32
            restore_console_mode(0x00F7)

        assert kernel32.SetConsoleMode.call_args.args[1] == 0x00F7

    def test_skips_when_no_console(self) -> None:
        kernel32 = self._kernel32(handle=INVALID_HANDLE_VALUE)
        mock_atexit, mock_logger = self._run(kernel32, last_error=ERROR_INVALID_HANDLE)

        mock_logger.warning.assert_not_called()

        kernel32.GetConsoleMode.assert_not_called()
        kernel32.SetConsoleMode.assert_not_called()
        kernel32.CloseHandle.assert_not_called()
        mock_atexit.register.assert_not_called()

    def test_logs_when_conin_open_fails_for_other_reason(self) -> None:
        kernel32 = self._kernel32(handle=INVALID_HANDLE_VALUE)
        mock_atexit, mock_logger = self._run(kernel32, last_error=5)

        mock_logger.warning.assert_called_once()
        kernel32.GetConsoleMode.assert_not_called()
        mock_atexit.register.assert_not_called()

    def test_logs_when_close_handle_fails(self) -> None:
        kernel32 = self._kernel32()
        kernel32.CloseHandle.return_value = 0
        mock_atexit, mock_logger = self._run(kernel32)

        mock_logger.warning.assert_called_once()
        mock_atexit.register.assert_called_once()

    def test_restore_logs_when_set_console_mode_fails(self) -> None:
        kernel32 = self._kernel32()
        kernel32.SetConsoleMode.return_value = 0
        with (
            patch("croupier.main.ctypes") as mock_ctypes,
            patch("croupier.main.logger") as mock_logger,
        ):
            mock_ctypes.WinDLL.return_value = kernel32
            mock_ctypes.get_last_error.return_value = 5
            restore_console_mode(0x00F7)

        mock_logger.warning.assert_called_once()

    def test_logs_and_continues_when_win32_call_fails(self) -> None:
        with (
            patch("croupier.main.sys.platform", "win32"),
            patch("croupier.main.ctypes") as mock_ctypes,
            patch("croupier.main.logger") as mock_logger,
        ):
            mock_ctypes.WinDLL.side_effect = OSError("boom")
            disable_console_quick_edit()
        mock_logger.warning.assert_called_once()

    def test_logs_when_get_console_mode_fails(self) -> None:
        kernel32 = self._kernel32()
        kernel32.GetConsoleMode.side_effect = None
        kernel32.GetConsoleMode.return_value = 0
        mock_atexit, mock_logger = self._run(kernel32)

        mock_logger.warning.assert_called_once()
        kernel32.SetConsoleMode.assert_not_called()
        mock_atexit.register.assert_not_called()
        kernel32.CloseHandle.assert_called_once_with(1234)

    def test_logs_when_set_console_mode_returns_zero(self) -> None:
        kernel32 = self._kernel32()
        kernel32.SetConsoleMode.return_value = 0
        mock_atexit, mock_logger = self._run(kernel32)

        mock_logger.warning.assert_called_once()
        mock_atexit.register.assert_not_called()
        kernel32.CloseHandle.assert_called_once_with(1234)
