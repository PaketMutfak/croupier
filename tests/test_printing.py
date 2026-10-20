import logging

import pytest
from escpos.printer import Dummy

from croupier.printing import Message
from croupier.printing import print_receipt


def test_sends_raw_content_to_connected_printer(sample_message: Message) -> None:
    printer = Dummy()

    print_receipt(sample_message, connect=lambda _m: printer)

    assert printer.output == sample_message.content


def test_connect_receives_the_message(sample_message: Message) -> None:
    seen: list[Message] = []

    def connect(message: Message) -> Dummy:
        seen.append(message)
        return Dummy()

    print_receipt(sample_message, connect=connect)

    assert seen == [sample_message]


def test_closes_printer_when_write_fails(sample_message: Message) -> None:
    class Failing(Dummy):  # type: ignore[misc]
        closed = False

        def _raw(self, msg: bytes) -> None:
            del msg
            msg_0 = "printer offline"
            raise OSError(msg_0)

        def close(self) -> None:
            self.closed = True

    printer = Failing()

    with pytest.raises(OSError, match="printer offline"):
        print_receipt(sample_message, connect=lambda _m: printer)

    assert printer.closed


class _BrokenClose(Dummy):  # type: ignore[misc]
    def __init__(self, error: Exception, *, raw_error: Exception | None = None) -> None:
        super().__init__()
        self._close_error = error
        self._raw_error = raw_error

    def _raw(self, msg: bytes) -> None:
        if self._raw_error is not None:
            raise self._raw_error
        super()._raw(msg)

    def close(self) -> None:
        raise self._close_error


def test_close_oserror_is_logged_and_original_error_propagates(
    sample_message: Message,
    caplog: pytest.LogCaptureFixture,
) -> None:
    printer = _BrokenClose(
        OSError("socket already closed"),
        raw_error=RuntimeError("printer offline"),
    )

    with (
        caplog.at_level(logging.ERROR, logger="croupier.printing"),
        pytest.raises(RuntimeError, match="printer offline"),
    ):
        print_receipt(sample_message, connect=lambda _m: printer)

    assert any(
        "printer close failed" in rec.message
        and rec.exc_info is not None
        and rec.exc_info[0] is OSError
        for rec in caplog.records
    )


def test_close_failure_after_success_is_logged_not_raised(
    sample_message: Message,
) -> None:
    print_receipt(sample_message, connect=lambda _m: _BrokenClose(OSError("x")))


def test_broader_close_errors_still_surface(sample_message: Message) -> None:
    # Narrow except on purpose: a RuntimeError from close() is a bug and must
    # not be hidden.
    with pytest.raises(RuntimeError, match="driver bug"):
        print_receipt(
            sample_message,
            connect=lambda _m: _BrokenClose(RuntimeError("driver bug")),
        )


def test_close_is_attempted_when_open_fails(sample_message: Message) -> None:
    class OpenFails(Dummy):  # type: ignore[misc]
        closed = False

        def open(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            msg = "refused"
            raise ConnectionError(msg)

        def close(self) -> None:
            self.closed = True

    printer = OpenFails()

    with pytest.raises(ConnectionError, match="refused"):
        print_receipt(sample_message, connect=lambda _m: printer)

    assert printer.closed
