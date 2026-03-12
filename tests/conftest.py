import json
from pathlib import Path

_CONFIG_PATH = Path.home() / ".croupier.json"
_TEST_CONFIG = {
    "queue_url": "amqp://guest:guest@127.0.0.1",
    "exchange_name": "test.receipt.dispatch",
    "queue_name": "test.receipt.dispatch",
}


def pytest_configure(config: object) -> None:
    """Create test config before test collection triggers module-level Settings()."""
    if not _CONFIG_PATH.exists():
        _CONFIG_PATH.write_text(json.dumps(_TEST_CONFIG))
        config._croupier_test_config = True  # type: ignore[attr-defined]


def pytest_unconfigure(config: object) -> None:
    """Remove test config if we created it."""
    if getattr(config, "_croupier_test_config", False):
        _CONFIG_PATH.unlink(missing_ok=True)
