import json
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from croupier.settings import load_settings

if TYPE_CHECKING:
    from pathlib import Path


def test_loads_from_given_path(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text(
        json.dumps(
            {
                "queue_url": "amqp://u:p@host",
                "queue_name": "q",
                # retired keys must keep loading
                "exchange_name": "e",
                "dlx_name": "x",
                "dlq_name": "d",
            },
        ),
    )

    settings = load_settings(path)

    assert settings.queue_name == "q"
    assert settings.queue_url.unicode_string().startswith("amqp://u:p@host")


def test_missing_field_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text("{}")

    with pytest.raises(ValidationError):
        load_settings(path)
