from pathlib import Path
from typing import ClassVar

from pydantic import AmqpDsn
from pydantic import BaseModel
from pydantic import ConfigDict

DEFAULT_SETTINGS_PATH = Path.home() / ".croupier.json"


class Settings(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")
    queue_url: AmqpDsn
    queue_name: str


def load_settings(path: Path | None = None) -> Settings:
    """Read Settings from a JSON file (default ``~/.croupier.json``).

    Unknown keys are ignored, so config files that still carry the retired
    ``exchange_name``, ``dlx_name`` and ``dlq_name`` keep loading.
    """
    source = path if path is not None else DEFAULT_SETTINGS_PATH
    return Settings.model_validate_json(source.read_text(encoding="utf-8"))
