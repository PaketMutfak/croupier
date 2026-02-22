from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from faststream.rabbit import TestRabbitBroker

from croupier.main import Message
from croupier.main import handle_message
from croupier.main import health
from croupier.main import router
from croupier.main import settings


@pytest.fixture
def sample_message() -> Message:
    return Message(
        id="test-123",
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
                    exchange=settings.exchange_name,
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
                    exchange=settings.exchange_name,
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
                    exchange=settings.exchange_name,
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
