import json
from typing import TYPE_CHECKING

import pytest
from escpos.printer import Dummy

from croupier.main import build_app
from croupier.printing import Message
from croupier.settings import Settings
from croupier.settings import load_settings

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

_TEST_CONFIG = {
    "queue_url": "amqp://guest:guest@127.0.0.1",
    "queue_name": "receipt.dispatch.test-branch",
}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    path = tmp_path / "croupier.json"
    path.write_text(json.dumps(_TEST_CONFIG))
    return load_settings(path)


@pytest.fixture
def printer() -> Dummy:
    return Dummy()


@pytest.fixture
def app(settings: Settings, printer: Dummy) -> FastAPI:
    return build_app(settings, connect=lambda _message: printer)


@pytest.fixture
def sample_message() -> Message:
    return Message(
        content=b"\x1bt\x00Hello World!\x1bd\x06\x1dV\x00",
        network_host="192.168.1.100",
        network_timeout=10,
    )
