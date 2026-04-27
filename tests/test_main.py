from typing import Literal
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
import sentry_sdk
from fastapi import FastAPI
from fastapi.testclient import TestClient
from faststream.exceptions import IgnoredException
from faststream.message import StreamMessage
from faststream.rabbit import TestRabbitBroker
from pydantic import AmqpDsn
from pydantic import HttpUrl

from croupier.main import Message
from croupier.main import SentryMiddleware
from croupier.main import Settings
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

    async def test_subscriber_exception_propagates_for_dlq_routing(
        self, sample_message: Message
    ) -> None:
        with patch("croupier.main.Network") as mock_network_cls:
            mock_printer = MagicMock()
            mock_printer._raw.side_effect = RuntimeError("printer offline")
            mock_network_cls.return_value = mock_printer

            async with TestRabbitBroker(router.broker) as br:
                with pytest.raises(RuntimeError, match="printer offline"):
                    await br.publish(sample_message, queue=settings.queue_name)

            mock_printer.close.assert_called_once()

    async def test_subscriber_closes_printer_on_success(
        self, sample_message: Message
    ) -> None:
        with patch("croupier.main.Network") as mock_network_cls:
            mock_printer = MagicMock()
            mock_network_cls.return_value = mock_printer

            async with TestRabbitBroker(router.broker) as br:
                await br.publish(sample_message, queue=settings.queue_name)

            mock_printer.close.assert_called_once()


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

        middleware = SentryMiddleware(None, context=MagicMock())
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

        middleware = SentryMiddleware(None, context=MagicMock())
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

        middleware = SentryMiddleware(None, context=MagicMock())
        with patch("croupier.main.sentry_sdk.capture_exception") as mock_capture:
            result = await middleware.consume_scope(call_next, stream_message)
        assert result == "ok"
        mock_capture.assert_not_called()

    async def test_isolation_scope_does_not_leak_between_messages(
        self, stream_message: StreamMessage[bytes]
    ) -> None:
        outer_scope = sentry_sdk.get_isolation_scope()
        inner_scopes: list[sentry_sdk.Scope] = []

        async def cn(_msg: StreamMessage[bytes]) -> None:  # noqa: RUF029
            inner_scopes.append(sentry_sdk.get_isolation_scope())

        middleware = SentryMiddleware(None, context=MagicMock())
        await middleware.consume_scope(cn, stream_message)
        await middleware.consume_scope(cn, stream_message)

        assert inner_scopes[0] is not outer_scope
        assert inner_scopes[1] is not outer_scope
        assert inner_scopes[0] is not inner_scopes[1]
        assert sentry_sdk.get_isolation_scope() is outer_scope


def _build_settings(
    sentry_dsn: HttpUrl | None = None,
    sentry_environment: Literal["development", "staging", "production"] = "production",
) -> Settings:
    return Settings.model_construct(
        queue_url=AmqpDsn("amqp://guest:guest@127.0.0.1"),
        exchange_name="x",
        queue_name="test.queue",
        dlx_name="x.dlx",
        dlq_name="x.dlq",
        sentry_dsn=sentry_dsn,
        sentry_environment=sentry_environment,
    )


class TestSentryInit:
    """Tests for opt-in/opt-out Sentry initialization in main()."""

    def test_init_skipped_when_dsn_is_none(self) -> None:
        test_settings = _build_settings()
        with (
            patch("croupier.main.settings", test_settings),
            patch("croupier.main.sentry_sdk.init") as mock_init,
            patch("croupier.main.sentry_sdk.set_tag") as mock_set_tag,
            patch("croupier.main.uvicorn.run"),
            patch("croupier.main.RotatingFileHandler"),
        ):
            main()
            mock_init.assert_not_called()
            mock_set_tag.assert_not_called()

    def test_init_called_with_pii_safe_kwargs_when_dsn_set(self) -> None:
        test_settings = _build_settings(
            sentry_dsn=HttpUrl("https://k@o.ingest.sentry.io/1"),
            sentry_environment="production",
        )
        with (
            patch("croupier.main.settings", test_settings),
            patch("croupier.main.sentry_sdk.init") as mock_init,
            patch("croupier.main.sentry_sdk.set_tag") as mock_set_tag,
            patch("croupier.main.uvicorn.run"),
            patch("croupier.main.RotatingFileHandler"),
        ):
            main()
            mock_init.assert_called_once()
            kwargs = mock_init.call_args.kwargs
            assert kwargs["dsn"] == "https://k@o.ingest.sentry.io/1"
            assert kwargs["send_default_pii"] is False
            assert kwargs["include_local_variables"] is False
            assert kwargs["attach_stacktrace"] is True
            assert kwargs["environment"] == "production"
            mock_set_tag.assert_called_with("queue_name", "test.queue")

    def test_handler_attached_before_sentry_init(self) -> None:
        test_settings = _build_settings(
            sentry_dsn=HttpUrl("https://k@o.ingest.sentry.io/1"),
        )
        order: list[str] = []

        def _record_init(**_: object) -> None:
            order.append("init")

        def _record_handler(**_: object) -> MagicMock:
            order.append("handler")
            return MagicMock()

        with (
            patch("croupier.main.settings", test_settings),
            patch("croupier.main.sentry_sdk.init", side_effect=_record_init),
            patch("croupier.main.sentry_sdk.set_tag"),
            patch("croupier.main.uvicorn.run"),
            patch(
                "croupier.main.RotatingFileHandler",
                side_effect=_record_handler,
            ),
        ):
            main()

        assert order.index("handler") < order.index("init")


class TestHandleMessageHTTP:
    """Tests for handle_message exposed as a FastAPI route."""

    def _make_client(self) -> TestClient:
        app = FastAPI()
        app.include_router(router)
        return TestClient(app, raise_server_exceptions=False)

    def test_http_route_returns_500_on_handler_exception(
        self, sample_message: Message
    ) -> None:
        with patch("croupier.main.Network") as mock_network_cls:
            mock_printer = MagicMock()
            mock_printer._raw.side_effect = RuntimeError("printer offline")
            mock_network_cls.return_value = mock_printer

            client = self._make_client()
            response = client.post(
                "/handle-message",
                content=sample_message.model_dump_json(),
                headers={"Content-Type": "application/json"},
            )

        assert response.status_code == 500
        mock_printer.close.assert_called_once()


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
