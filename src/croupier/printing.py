from collections.abc import Callable
from typing import ClassVar

from escpos.escpos import Escpos
from escpos.printer import Network
from pydantic import BaseModel
from pydantic import ConfigDict


class Message(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")
    content: bytes
    network_host: str
    network_timeout: int


type PrinterFactory = Callable[[Message], Escpos]


def connect_network(message: Message) -> Escpos:
    return Network(host=message.network_host, timeout=message.network_timeout)


def print_receipt(
    message: Message,
    connect: PrinterFactory = connect_network,
) -> None:
    """Send the raw ESC/POS bytes of ``message`` to its printer.

    Blocking. Always closes the connection; connection and write errors propagate.
    """
    printer = connect(message)
    try:
        printer.open()
        # python-escpos has no public raw-bytes method.
        printer._raw(message.content)  # noqa: SLF001  # pylint: disable=W0212
    finally:
        printer.close()
