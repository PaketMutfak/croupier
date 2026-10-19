from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from escpos.printer import Dummy
from fastapi.testclient import TestClient
from faststream import AckPolicy
from faststream.rabbit import TestRabbitBroker

from croupier.main import ENABLE_EXTENDED_FLAGS
from croupier.main import ENABLE_QUICK_EDIT_MODE
from croupier.main import ERROR_INVALID_HANDLE
from croupier.main import INVALID_HANDLE_VALUE
from croupier.main import build_app
from croupier.main import disable_console_quick_edit
from croupier.main import restore_console_mode

if TYPE_CHECKING:
    from fastapi import FastAPI

    from croupier.printing import Message
    from croupier.settings import Settings


class TestSubscriber:
    async def test_prints_message_content(
        self,
        app: FastAPI,
        settings: Settings,
        printer: Dummy,
        sample_message: Message,
    ) -> None:
        async with TestRabbitBroker(app.state.broker) as br:
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
        app = build_app(settings, connect=lambda _m: printer)

        async with TestRabbitBroker(app.state.broker) as br:
            await br.publish(sample_message, queue=settings.queue_name)

        assert closed == [True]


class TestSubscriberFailure:
    async def test_printer_error_reaches_the_broker_so_message_is_rejected(
        self,
        settings: Settings,
        sample_message: Message,
    ) -> None:
        printer = Dummy()
        app = build_app(settings, connect=lambda _m: printer)

        with patch.object(printer, "_raw", side_effect=OSError("printer offline")):
            async with TestRabbitBroker(app.state.broker) as br:
                with pytest.raises(OSError, match="printer offline"):
                    await br.publish(sample_message, queue=settings.queue_name)

    async def test_subscriber_rejects_on_error_and_never_declares_the_queue(
        self,
        app: FastAPI,
        settings: Settings,
    ) -> None:
        [subscriber] = app.state.broker.subscribers

        assert subscriber.ack_policy is AckPolicy.REJECT_ON_ERROR
        assert subscriber.queue.name == settings.queue_name
        assert subscriber.queue.declare is False


class TestHandleMessageEndpoint:
    async def test_prints_posted_message(
        self,
        app: FastAPI,
        printer: Dummy,
        sample_message: Message,
    ) -> None:
        async with TestRabbitBroker(app.state.broker):
            with TestClient(app) as client:
                response = client.post(
                    "/handle-message",
                    json={
                        "content": sample_message.content.decode("latin-1"),
                        "network_host": sample_message.network_host,
                        "network_timeout": sample_message.network_timeout,
                    },
                )

        assert response.status_code == 200
        assert printer.output == sample_message.content

    async def test_printer_failure_is_an_error_and_still_closes(
        self,
        settings: Settings,
        sample_message: Message,
    ) -> None:
        printer = Dummy()
        closed: list[bool] = []
        printer.close = lambda: closed.append(True)
        with patch.object(printer, "_raw", side_effect=OSError("printer offline")):
            app = build_app(settings, connect=lambda _m: printer)
            async with TestRabbitBroker(app.state.broker):
                with TestClient(app, raise_server_exceptions=False) as client:
                    response = client.post(
                        "/handle-message",
                        json={
                            "content": "x",
                            "network_host": sample_message.network_host,
                            "network_timeout": sample_message.network_timeout,
                        },
                    )

        assert response.status_code == 500
        assert closed == [True]


class TestHealthEndpoint:
    @pytest.mark.parametrize(
        ("ping_result", "expected_status"),
        [(True, 204), (False, 500)],
    )
    async def test_status_follows_broker_ping(
        self,
        app: FastAPI,
        ping_result: bool,  # noqa: FBT001
        expected_status: int,
    ) -> None:
        async with TestRabbitBroker(app.state.broker) as br:
            with (
                patch.object(br, "ping", AsyncMock(return_value=ping_result)),
                TestClient(app) as client,
            ):
                response = client.get("/")

        assert response.status_code == expected_status


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
