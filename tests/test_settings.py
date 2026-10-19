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


def _write(tmp_path: Path, extra: dict[str, str]) -> Path:
    path = tmp_path / "c.json"
    path.write_text(
        json.dumps({"queue_url": "amqp://u:p@host", "queue_name": "q", **extra}),
    )
    return path


def test_sentry_defaults_to_disabled_development(tmp_path: Path) -> None:
    settings = load_settings(_write(tmp_path, {}))

    assert settings.sentry_dsn is None
    assert settings.sentry_environment == "development"


def test_sentry_dsn_accepts_well_formed(tmp_path: Path) -> None:
    settings = load_settings(
        _write(
            tmp_path,
            {
                "sentry_dsn": "https://public@example.com/1",
                "sentry_environment": "production",
            },
        ),
    )

    assert settings.sentry_dsn is not None
    assert settings.sentry_dsn.username == "public"
    assert settings.sentry_dsn.path == "/1"
    assert settings.sentry_environment == "production"


def test_environment_rejects_invalid_literal(tmp_path: Path) -> None:
    # A typo like "prod" must fail loudly at config load, not silently land
    # events in Sentry's default bucket.
    with pytest.raises(ValidationError):
        load_settings(_write(tmp_path, {"sentry_environment": "prod"}))


def test_misspelled_key_is_silently_ignored(tmp_path: Path) -> None:
    # extra="ignore" keeps retired keys loading; the trade-off is that a typo
    # is dropped instead of raising. Pin the policy so a flip to "forbid"
    # shows up in tests.
    settings = load_settings(_write(tmp_path, {"snetry_environment": "production"}))

    assert settings.sentry_environment == "development"
