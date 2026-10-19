import atexit
import ctypes
import logging
import sys
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import ClassVar

import uvicorn
from escpos.printer import Network
from fastapi import FastAPI
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
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
    from collections.abc import Iterator


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


settings = Settings()  # type: ignore[call-arg]
router = RabbitRouter(settings.queue_url.unicode_string())


@router.subscriber(queue=RabbitQueue(name=settings.queue_name, declare=False))
@router.post("/handle-message")
async def handle_message(body: Message) -> None:  # noqa: RUF029
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


logger = logging.getLogger(__name__)

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 3
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


@contextmanager
def _console_input(kernel32: Any) -> Iterator[int | None]:  # noqa: ANN401
    """Yield a handle to the console input buffer, or None without a console.

    ``CONIN$`` is opened explicitly because the standard input handle points
    at a file or pipe when stdin is redirected.
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
        yield None
        return
    try:
        yield handle
    finally:
        kernel32.CloseHandle(handle)


def _set_console_mode(kernel32: Any, handle: int, mode: int) -> None:  # noqa: ANN401
    if not kernel32.SetConsoleMode(handle, mode):
        error = ctypes.get_last_error()  # type: ignore[attr-defined]  # pyrefly: ignore[missing-attribute]
        msg = f"SetConsoleMode failed (error {error})"
        raise OSError(msg)


def _clear_quick_edit_flag() -> None:
    kernel32 = _kernel32()
    with _console_input(kernel32) as handle:
        mode = ctypes.c_uint32()
        if handle is None or not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return  # Not attached to a console (e.g. windowless).
        original_mode = mode.value
        new_mode = (original_mode & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS
        _set_console_mode(kernel32, handle, new_mode)
    atexit.register(restore_console_mode, original_mode)


def restore_console_mode(original_mode: int) -> None:
    try:
        kernel32 = _kernel32()
        with _console_input(kernel32) as handle:
            if handle is not None:
                _set_console_mode(kernel32, handle, original_mode)
    except OSError:
        logger.warning("Could not restore console mode", exc_info=True)


def main() -> None:
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

    disable_console_quick_edit()

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
