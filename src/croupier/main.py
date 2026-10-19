import asyncio
import atexit
import ctypes
import logging
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING
from typing import Any
from typing import NoReturn
from typing import override

import sentry_sdk
import uvicorn
from faststream import AckPolicy
from faststream import BaseMiddleware
from faststream.asgi import AsgiFastStream
from faststream.exceptions import IgnoredException
from faststream.rabbit import RabbitBroker
from faststream.rabbit import RabbitQueue
from lite_bootstrap import FastStreamBootstrapper
from lite_bootstrap import FastStreamConfig
from sentry_sdk.integrations.logging import ignore_logger

from croupier.printing import Message
from croupier.printing import PrinterFactory
from croupier.printing import connect_network
from croupier.printing import print_receipt
from croupier.settings import Settings
from croupier.settings import load_settings

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from collections.abc import Iterator

    from faststream.message import StreamMessage


logger = logging.getLogger(__name__)

# FastStream logs every handler failure on its own ``faststream`` logger. The
# Sentry SDK's auto-enabled LoggingIntegration would promote that ERROR record
# to a second event with no exception and no ``correlation_id``, duplicating the
# one ``SentryMiddleware`` already emits. Breadcrumbs and events from this
# logger are dropped; the middleware event is the canonical one.
ignore_logger("faststream")

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 3
ERROR_FILE_NOT_FOUND = 2
ERROR_INVALID_HANDLE = 6
# CreateFileW("CONIN$") fails with one of these when no console is attached.
NO_CONSOLE_ERRORS = frozenset({ERROR_FILE_NOT_FOUND, ERROR_INVALID_HANDLE})
ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080


def disable_console_quick_edit() -> None:
    """Disable Windows console QuickEdit mode for the life of the process.

    With QuickEdit on, a click inside the console starts a text selection,
    which blocks writes to stdout and freezes the service until a key is
    pressed.

    The console input mode is shared with the parent shell, so the original
    mode is restored at exit. This is a conhost setting; it may have no effect
    under Windows Terminal. Failures are logged and never prevent startup.
    """
    if sys.platform == "win32":
        try:
            _clear_quick_edit_flag()
        except OSError:
            logger.warning("Could not disable console QuickEdit mode", exc_info=True)


def _kernel32() -> Any:  # noqa: ANN401
    kernel32 = ctypes.WinDLL(  # type: ignore[attr-defined]  # pyrefly: ignore[missing-attribute]
        "kernel32", use_last_error=True
    )
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.GetConsoleMode.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    return kernel32


def _raise_last_error(call: str) -> NoReturn:
    error = ctypes.get_last_error()  # type: ignore[attr-defined]  # pyrefly: ignore[missing-attribute]
    msg = f"{call} failed (error {error})"
    raise OSError(msg)


@contextmanager
def _console_input(kernel32: Any) -> Iterator[int | None]:  # noqa: ANN401
    """Yield a handle to the console input buffer, or None without a console.

    ``CONIN$`` is opened explicitly because the standard input handle points
    at a file or pipe when stdin is redirected. Only the errors Windows
    reports for a missing console mean "no console"; any other failure raises.
    """
    handle = kernel32.CreateFileW(
        "CONIN$",
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if handle in {None, INVALID_HANDLE_VALUE}:
        if ctypes.get_last_error() in NO_CONSOLE_ERRORS:  # type: ignore[attr-defined]  # pyrefly: ignore[missing-attribute]
            yield None
            return
        _raise_last_error("CreateFileW(CONIN$)")
    try:
        yield handle
    finally:
        if not kernel32.CloseHandle(handle):
            # Cleanup only: raising here would mask the real outcome.
            logger.warning(
                "CloseHandle failed (error %s)",
                ctypes.get_last_error(),  # type: ignore[attr-defined]  # pyrefly: ignore[missing-attribute]
            )


def _clear_quick_edit_flag() -> None:
    kernel32 = _kernel32()
    with _console_input(kernel32) as handle:
        if handle is None:
            return  # Not attached to a console (e.g. windowless).
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            _raise_last_error("GetConsoleMode")
        original_mode = mode.value
        new_mode = (original_mode & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS
        if not kernel32.SetConsoleMode(handle, new_mode):
            _raise_last_error("SetConsoleMode")
    atexit.register(restore_console_mode, original_mode)


def restore_console_mode(original_mode: int) -> None:
    try:
        kernel32 = _kernel32()
        with _console_input(kernel32) as handle:
            if handle is not None and not kernel32.SetConsoleMode(
                handle, original_mode
            ):
                _raise_last_error("SetConsoleMode")
    except OSError:
        logger.warning("Could not restore console mode", exc_info=True)




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
            if msg.correlation_id:
                scope.set_tag("correlation_id", msg.correlation_id)
            try:
                return await call_next(msg)
            except IgnoredException:
                raise
            except Exception:
                # Sentry SDK's auto-enabled LoggingIntegration promotes
                # ERROR-and-up records to standalone events; logger.exception
                # is enough — no explicit capture_exception() needed. Exception
                # class is filterable in the Sentry UI via the built-in
                # ``error.type`` index, so no custom tag is needed.
                logger.exception("message handler failed")
                # Re-raise so FastStream's reject -> DLX/DLQ path runs.
                raise


def build_broker(
    settings: Settings,
    connect: PrinterFactory = connect_network,
) -> RabbitBroker:
    """Assemble the broker that consumes the receipt queue.

    The producing side owns the RabbitMQ topology (exchange, per-branch queues,
    DLX/DLQ, TTL), so the subscriber binds to the existing queue with ``declare=False``
    and never declares anything; see ``docs/adr/0001``.

    Failure path: ``print_receipt`` errors propagate out of ``handle_message``.
    ``AckPolicy.REJECT_ON_ERROR`` then rejects the message without requeue, the
    queue's dead-letter arguments route it to ``receipt.dispatch.dlq``, and the
    producing side alerts on it. ``SentryMiddleware`` re-raises, so that still holds.
    """
    broker = RabbitBroker(
        settings.queue_url.unicode_string(),
        middlewares=(SentryMiddleware,) if settings.sentry_dsn else (),
    )

    @broker.subscriber(
        queue=RabbitQueue(name=settings.queue_name, declare=False),
        ack_policy=AckPolicy.REJECT_ON_ERROR,
    )
    async def handle_message(body: Message) -> None:
        await asyncio.to_thread(print_receipt, body, connect)

    return broker


def create_app(
    settings: Settings | None = None,
    connect: PrinterFactory = connect_network,
) -> AsgiFastStream:
    """ASGI app factory that wires Sentry and structlog through lite-bootstrap.

    Compatible with ``uvicorn --factory``. Bootstrap reconfigures global state
    (structlog, Sentry init); call once per process.
    """
    settings = settings if settings is not None else load_settings()
    config = FastStreamConfig(
        application=AsgiFastStream(build_broker(settings, connect)),
        service_name="croupier",
        service_environment=settings.sentry_environment,
        # Required for the LoggingInstrument to take effect (structlog ->
        # JSON stdout). Toggle if a future requirement asks for plain
        # logging in development.
        service_debug=False,
        sentry_dsn=(
            settings.sentry_dsn.unicode_string() if settings.sentry_dsn else None
        ),
        # AsyncioIntegration is deliberately NOT registered: on every SIGTERM
        # shutdown it reported the cancelled aiormq channel tasks as four
        # ``ChannelClosed`` error events per process. The auto-enabled
        # LoggingIntegration handles ERROR-and-up log records, which is how
        # SentryMiddleware emits events.
        # Process-wide Sentry tags. lite-bootstrap calls sentry_sdk.set_tags()
        # after init.
        sentry_tags={"queue_name": settings.queue_name},
    )
    return FastStreamBootstrapper(config).bootstrap()


def main() -> None:
    app = create_app()
    disable_console_quick_edit()
    uvicorn.run(app)
