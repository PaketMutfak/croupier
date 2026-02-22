"""Demo script that generates ESC/POS receipt bytes and publishes to the queue."""

from __future__ import annotations

import asyncio
import uuid

from escpos.printer import Dummy

from croupier.main import Message
from croupier.main import router
from croupier.main import settings


def build_receipt() -> bytes:
    printer = Dummy()
    printer.set(align="center", bold=True)
    printer.text("DEMO RECEIPT!")
    printer.set(align="left", bold=False)
    printer.text("Item 1          $10.00")
    printer.ln()
    printer.text("Item 2          $5.50!")
    printer.ln()
    printer.text("Total           $5.50!")
    printer.ln(count=4)
    printer.cut()
    printer.buzzer()
    return printer.output


async def main() -> None:
    message = Message(
        id=str(uuid.uuid4()),
        content=build_receipt(),
        network_host="192.168.1.114",
        network_timeout=10,
    )

    async with router.broker:
        _ = await router.broker.publish(
            message=message,
            queue=settings.queue_name,
            exchange=settings.exchange_name,
            timeout=60,
        )
        print(f"Published message {message.id}")  # noqa: T201


if __name__ == "__main__":
    asyncio.run(main())
