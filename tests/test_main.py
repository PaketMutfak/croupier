from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from faststream.exceptions import IgnoredException
from faststream.message import StreamMessage
from faststream.rabbit import TestRabbitBroker

from croupier.main import Message
from croupier.main import SentryMiddleware
from croupier.main import handle_message
from croupier.main import health
from croupier.main import main
from croupier.main import router
from croupier.main import settings


@pytest.fixture
def sample_message() -> Message:
    return Message(
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
                )

            mock_printer._raw.assert_called_once_with(
                b"\x1bt\x00Hello World!\x1bd\x06\x1dV\x00"
            )

    async def test_handler_sets_printer_id_tag_and_fingerprint(
        self, sample_message: Message
    ) -> None:
        fake_iso_scope = MagicMock()
        with (
            patch("croupier.main.Network") as mock_network_cls,
            patch("croupier.main.sentry_sdk.set_tag") as mock_set_tag,
            patch(
                "croupier.main.sentry_sdk.get_isolation_scope",
                return_value=fake_iso_scope,
            ),
        ):
            mock_network_cls.return_value = MagicMock()
            async with TestRabbitBroker(router.broker) as br:
                await br.publish(sample_message, queue=settings.queue_name)

        mock_set_tag.assert_any_call(
            "printer.id", f"{settings.queue_name}:192.168.1.100"
        )
        assert fake_iso_scope.fingerprint == ["{{ default }}", settings.queue_name]


class TestSentryMiddleware:
    """Tests for the AMQP SentryMiddleware."""

    @pytest.fixture
    def stream_message(self) -> StreamMessage[bytes]:
        return MagicMock(spec=StreamMessage)

    async def test_captures_unhandled_exception_and_reraises(
        self, stream_message: StreamMessage[bytes]
    ) -> None:
        async def call_next(_msg: StreamMessage[bytes]) -> None:  # noqa: RUF029
            msg = "printer offline"
            raise RuntimeError(msg)

        middleware = SentryMiddleware(stream_message, context=MagicMock())
        with (
            patch("croupier.main.sentry_sdk.capture_exception") as mock_capture,
            pytest.raises(RuntimeError, match="printer offline"),
        ):
            await middleware.consume_scope(call_next, stream_message)
        mock_capture.assert_called_once()

    async def test_skips_capture_for_ignored_exception(
        self, stream_message: StreamMessage[bytes]
    ) -> None:
        async def call_next(_msg: StreamMessage[bytes]) -> None:  # noqa: RUF029
            raise IgnoredException

        middleware = SentryMiddleware(stream_message, context=MagicMock())
        with (
            patch("croupier.main.sentry_sdk.capture_exception") as mock_capture,
            pytest.raises(IgnoredException),
        ):
            await middleware.consume_scope(call_next, stream_message)
        mock_capture.assert_not_called()

    async def test_passes_through_when_no_exception(
        self, stream_message: StreamMessage[bytes]
    ) -> None:
        async def call_next(_msg: StreamMessage[bytes]) -> str:  # noqa: RUF029
            return "ok"

        middleware = SentryMiddleware(stream_message, context=MagicMock())
        with patch("croupier.main.sentry_sdk.capture_exception") as mock_capture:
            result = await middleware.consume_scope(call_next, stream_message)
        assert result == "ok"
        mock_capture.assert_not_called()


class TestSentryInit:
    """Tests for opt-in/opt-out Sentry initialization in main()."""

    def test_init_skipped_when_dsn_is_none(self) -> None:
        with (
            patch("croupier.main.settings") as mock_settings,
            patch("croupier.main.sentry_sdk.init") as mock_init,
            patch("croupier.main.uvicorn.run"),
            patch("croupier.main.RotatingFileHandler"),
        ):
            mock_settings.sentry_dsn = None
            main()
            mock_init.assert_not_called()

    def test_init_called_with_pii_safe_kwargs_when_dsn_set(self) -> None:
        with (
            patch("croupier.main.settings") as mock_settings,
            patch("croupier.main.sentry_sdk.init") as mock_init,
            patch("croupier.main.sentry_sdk.set_tag") as mock_set_tag,
            patch("croupier.main.uvicorn.run"),
            patch("croupier.main.RotatingFileHandler"),
        ):
            mock_settings.sentry_dsn = "https://k@o.ingest.sentry.io/1"
            mock_settings.sentry_environment = "production"
            mock_settings.sentry_release = "croupier@0.1.0"
            mock_settings.queue_name = "test.queue"

            main()
            mock_init.assert_called_once()
            kwargs = mock_init.call_args.kwargs
            assert kwargs["send_default_pii"] is False
            assert kwargs["include_local_variables"] is False
            assert kwargs["environment"] == "production"
            assert kwargs["release"] == "croupier@0.1.0"
            mock_set_tag.assert_called_with("queue_name", "test.queue")


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
