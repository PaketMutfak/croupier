from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from faststream.rabbit import TestRabbitBroker

from croupier.main import ENABLE_EXTENDED_FLAGS
from croupier.main import ENABLE_QUICK_EDIT_MODE
from croupier.main import INVALID_HANDLE_VALUE
from croupier.main import Message
from croupier.main import disable_console_quick_edit
from croupier.main import handle_message
from croupier.main import health
from croupier.main import restore_console_mode
from croupier.main import router
from croupier.main import settings


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
        return kernel32

    def _run(self, kernel32: MagicMock) -> tuple[MagicMock, MagicMock]:
        with (
            patch("croupier.main.sys.platform", "win32"),
            patch("croupier.main.ctypes") as mock_ctypes,
            patch("croupier.main.atexit") as mock_atexit,
            patch("croupier.main.logger") as mock_logger,
        ):
            mock_ctypes.WinDLL.return_value = kernel32
            mock_ctypes.c_uint32.return_value = MagicMock()
            mock_ctypes.byref.side_effect = lambda x: x
            mock_ctypes.get_last_error.return_value = 5
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
        mock_atexit, _ = self._run(kernel32)

        kernel32.GetConsoleMode.assert_not_called()
        kernel32.SetConsoleMode.assert_not_called()
        kernel32.CloseHandle.assert_not_called()
        mock_atexit.register.assert_not_called()

    def test_logs_and_continues_when_win32_call_fails(self) -> None:
        with (
            patch("croupier.main.sys.platform", "win32"),
            patch("croupier.main.ctypes") as mock_ctypes,
            patch("croupier.main.logger") as mock_logger,
        ):
            mock_ctypes.WinDLL.side_effect = OSError("boom")
            disable_console_quick_edit()
        mock_logger.warning.assert_called_once()

    def test_logs_when_set_console_mode_returns_zero(self) -> None:
        kernel32 = self._kernel32()
        kernel32.SetConsoleMode.return_value = 0
        mock_atexit, mock_logger = self._run(kernel32)

        mock_logger.warning.assert_called_once()
        mock_atexit.register.assert_not_called()
        kernel32.CloseHandle.assert_called_once_with(1234)
