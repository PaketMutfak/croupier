import asyncio
import atexit
import ctypes
import logging
import sys
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import NoReturn

import uvicorn
from fastapi import FastAPI
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from faststream import AckPolicy
from faststream.rabbit import RabbitQueue
from faststream.rabbit.fastapi import RabbitRouter
from starlette.responses import Response
from starlette.status import HTTP_204_NO_CONTENT
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR

from croupier.printing import Message
from croupier.printing import PrinterFactory
from croupier.printing import connect_network
from croupier.printing import print_receipt
from croupier.settings import Settings
from croupier.settings import load_settings

if TYPE_CHECKING:
    from collections.abc import Iterator


def build_app(
    settings: Settings,
    connect: PrinterFactory = connect_network,
) -> FastAPI:
    """Assemble the app that consumes the receipt queue and serves the HTTP routes.

    The producing side owns the RabbitMQ topology (exchange, per-branch queues,
    DLX/DLQ, TTL), so the subscriber binds to the existing queue with ``declare=False``
    and never declares anything; see ``docs/adr/0001``.

    Failure path: ``print_receipt`` errors propagate out of ``handle_message``.
    ``AckPolicy.REJECT_ON_ERROR`` then rejects the message without requeue, the
    queue's dead-letter arguments route it to ``receipt.dispatch.dlq``, and the
    producing side alerts on it. Swallowing errors or requeueing here would break that.
    Over HTTP the same error is a 500.
    """
    router = RabbitRouter(settings.queue_url.unicode_string())

    @router.subscriber(
        queue=RabbitQueue(name=settings.queue_name, declare=False),
        ack_policy=AckPolicy.REJECT_ON_ERROR,
    )
    @router.post("/handle-message")
    async def handle_message(body: Message) -> None:
        await asyncio.to_thread(print_receipt, body, connect)

    @router.get("/")
    async def health(request: Request) -> Response:
        if await request.state.broker.ping(6):
            return Response(status_code=HTTP_204_NO_CONTENT)
        return Response(status_code=HTTP_500_INTERNAL_SERVER_ERROR)

    app = FastAPI(debug=True)
    app.state.broker = router.broker
    app.include_router(router)
    app.add_middleware(
        CORSMiddleware,  # ty:ignore[invalid-argument-type]
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )
    return app


logger = logging.getLogger(__name__)

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


def _configure_logging() -> None:
    file_handler = RotatingFileHandler(
        filename=Path.home() / ".croupier.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"),
    )
    for name in ("faststream", "faststream.access.rabbit", __name__):
        logging.getLogger(name).addHandler(file_handler)


def main() -> None:
    settings = load_settings()
    _configure_logging()
    disable_console_quick_edit()
    uvicorn.run(build_app(settings))
