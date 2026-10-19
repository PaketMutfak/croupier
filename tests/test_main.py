from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from escpos.printer import Dummy
from fastapi.testclient import TestClient
from faststream import AckPolicy
from faststream.rabbit import TestRabbitBroker

from croupier.main import build_app

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
        printer.close = lambda: closed.append(True)  # type: ignore[method-assign]
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
        printer.close = lambda: closed.append(True)  # type: ignore[method-assign]
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
