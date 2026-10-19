import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from faststream import AckPolicy
from faststream.rabbit import RabbitQueue
from faststream.rabbit.fastapi import RabbitRouter
from starlette.responses import Response
from starlette.status import HTTP_204_NO_CONTENT
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR

from croupier.printing import Message
from croupier.printing import PrinterFactory
from croupier.printing import connect_network
from croupier.printing import print_receipt
from croupier.settings import Settings
from croupier.settings import load_settings


def build_app(
    settings: Settings,
    connect: PrinterFactory = connect_network,
) -> FastAPI:
    """Assemble the app that consumes the receipt queue and serves the HTTP routes.

    The producing side owns the RabbitMQ topology (exchange, per-branch queues,
    DLX/DLQ, TTL), so the subscriber binds to the existing queue with ``declare=False``
    and never declares anything; see ``docs/adr/0001``.

    Failure path: ``print_receipt`` errors propagate out of ``handle_message``.
    ``AckPolicy.REJECT_ON_ERROR`` then rejects the message without requeue, the
    queue's dead-letter arguments route it to ``receipt.dispatch.dlq``, and the
    producing side alerts on it. Swallowing errors or requeueing here would break that.
    Over HTTP the same error is a 500.
    """
    router = RabbitRouter(settings.queue_url.unicode_string())

    @router.subscriber(
        queue=RabbitQueue(name=settings.queue_name, declare=False),
        ack_policy=AckPolicy.REJECT_ON_ERROR,
    )
    @router.post("/handle-message")
    async def handle_message(body: Message) -> None:
        await asyncio.to_thread(print_receipt, body, connect)

    @router.get("/")
    async def health(request: Request) -> Response:
        if await request.state.broker.ping(6):
            return Response(status_code=HTTP_204_NO_CONTENT)
        return Response(status_code=HTTP_500_INTERNAL_SERVER_ERROR)

    app = FastAPI(debug=True)
    app.state.broker = router.broker
    app.include_router(router)
    app.add_middleware(
        CORSMiddleware,  # ty:ignore[invalid-argument-type]
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )
    return app


def _configure_logging() -> None:
    file_handler = RotatingFileHandler(
        filename=Path.home() / ".croupier.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"),
    )
    for name in ("faststream", "faststream.access.rabbit"):
        logging.getLogger(name).addHandler(file_handler)


def main() -> None:
    settings = load_settings()
    _configure_logging()
    uvicorn.run(build_app(settings))
