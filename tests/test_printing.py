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
