"""Demo script that generates ESC/POS receipt bytes and publishes to the queue.

The producing side owns the RabbitMQ topology, so this publishes to an existing queue
and declares nothing (re-declaring it with different arguments would fail with
PRECONDITION_FAILED). The queue must already exist.
"""

import asyncio

from escpos.printer import Dummy
from faststream.rabbit import RabbitBroker
from faststream.rabbit import RabbitQueue

from croupier.printing import Message
from croupier.settings import load_settings

RECEIPT_EXCHANGE = "receipt.dispatch"


def justify(left: str, right: str, width: int = 32) -> str:
    space = width - len(left) - len(right)
    return f"{left}{' ' * max(space, 1)}{right}"


def build_receipt() -> bytes:
    printer = Dummy()
    printer.set(align="center", bold=True)
    printer.text("DEMO RECEIPT!")
    printer.ln()
    printer.set(align="left", bold=False)
    printer.text(justify("Item 1", "10.00 TL"))
    printer.ln()
    printer.text(justify("Item 2", "5.50 TL"))
    printer.ln()
    printer.text(justify("Total", "5.50 TL"))
    printer.ln(count=4)
    printer.cut()
    printer.buzzer()
    return printer.output


async def main() -> None:
    settings = load_settings()
    message = Message(
        content=build_receipt(),
        network_host="localhost",
        network_timeout=10,
    )

    async with RabbitBroker(settings.queue_url.unicode_string()) as broker:
        _ = await broker.publish(
            message=message,
            queue=RabbitQueue(name=settings.queue_name, declare=False),
            exchange=RECEIPT_EXCHANGE,
            timeout=60,
        )
        print("Published message")  # noqa: T201


if __name__ == "__main__":
    asyncio.run(main())
