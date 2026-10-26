from pathlib import Path
from typing import ClassVar
from typing import Literal

from pydantic import AmqpDsn
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import HttpUrl

DEFAULT_SETTINGS_PATH = Path.home() / ".croupier.json"
DEFAULT_SPOOL_DIR = Path.home() / ".croupier-sentry-spool"


class Settings(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)
    queue_url: AmqpDsn
    queue_name: str
    sentry_dsn: HttpUrl | None = None
    sentry_environment: Literal["development", "staging", "production"] = "development"


def load_settings(path: Path | None = None) -> Settings:
    """Read Settings from a JSON file (default ``~/.croupier.json``).

    Unknown keys are ignored, so config files that still carry the retired
    ``exchange_name``, ``dlx_name`` and ``dlq_name`` keep loading.
    """
    source = path if path is not None else DEFAULT_SETTINGS_PATH
    return Settings.model_validate_json(source.read_text(encoding="utf-8"))
