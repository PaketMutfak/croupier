import json
from pathlib import Path

_CONFIG_PATH = Path.home() / ".croupier.json"
_TEST_CONFIG = {
    "queue_url": "amqp://guest:guest@127.0.0.1",
    "exchange_name": "test.receipt.dispatch",
    "queue_name": "test.receipt.dispatch",
    "dlx_name": "test.receipt.dispatch.dlx",
    "dlq_name": "test.receipt.dispatch.dlq",
}


def pytest_configure(config: object) -> None:
    """Back up any existing config and write test config before collection triggers module-level Settings()."""
    backup = None
    if _CONFIG_PATH.exists():
        backup = _CONFIG_PATH.read_text()
    _CONFIG_PATH.write_text(json.dumps(_TEST_CONFIG))
    config._croupier_original_config = backup  # type: ignore[attr-defined]


def pytest_unconfigure(config: object) -> None:
    """Restore original config or remove test config."""
    original = getattr(config, "_croupier_original_config", None)
    if original is not None:
        _CONFIG_PATH.write_text(original)
    else:
        _CONFIG_PATH.unlink(missing_ok=True)
