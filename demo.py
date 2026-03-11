"""Demo script that generates ESC/POS receipt bytes and publishes to the queue."""

import asyncio

from escpos.printer import Dummy

from croupier.main import Message
from croupier.main import router
from croupier.main import settings


def justify(left: str, right: str, width: int = 32) -> str:
    space = width - len(left) - len(right)
    return f"{left}{' ' * max(space, 1)}{right}"


def build_receipt() -> bytes:
    printer = Dummy()
    printer.set(align="center", bold=True)
    printer.text("DEMO RECEIPT!")
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
    message = Message(
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
        print("Published message")  # noqa: T201


if __name__ == "__main__":
    asyncio.run(main())
