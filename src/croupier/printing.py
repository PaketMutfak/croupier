import logging
from collections.abc import Callable
from typing import ClassVar

from escpos.escpos import Escpos
from escpos.printer import Network
from pydantic import BaseModel
from pydantic import ConfigDict

logger = logging.getLogger(__name__)


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

    Blocking. Always attempts to close the connection; connection and write
    errors propagate. A failing ``close()`` is logged (and so reported to Sentry)
    but never replaces the original error.
    """
    printer = connect(message)
    try:
        printer.open()  # type: ignore[no-untyped-call]
        # python-escpos has no public raw-bytes method.
        printer._raw(message.content)  # noqa: SLF001  # pylint: disable=W0212
    finally:
        try:
            printer.close()  # type: ignore[no-untyped-call]
        # PEP 758 unparenthesized except — requires Python 3.14+. close() on a
        # printer whose open() never completed raises AttributeError; narrow to
        # socket cleanup errors so programming bugs still surface.
        except OSError, AttributeError:
            logger.exception("printer close failed")
