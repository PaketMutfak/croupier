# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Croupier

Croupier is a receipt printing microservice. It consumes ESC/POS receipt messages from a RabbitMQ queue and forwards raw bytes to network thermal printers. It also exposes the same handler as a REST endpoint via FastAPI.

## Python Version

Requires Python **3.14+** (`requires-python = ">=3.14"`).

## Commands

```bash
uv sync                  # Install dependencies
uv run croupier             # Start the server (reads ~/.croupier.json)
uv run pytest            # Run all tests
uv run pytest tests/test_main.py::TestHealthEndpoint  # Run a single test class
uv run pytest -k "test_returns_204"                   # Run tests matching a pattern
```

### Lint / CI

The full CI pipeline (also available via `xc ci`):

```bash
uv run validate-pyproject pyproject.toml
uv run typos
uv run bandit -c pyproject.toml -r ./src ./tests
uv run pyup-dirs --py314-plus recursive src tests
uv run taplo lint pyproject.toml
uv run taplo format pyproject.toml
uv run ruff check
uv run ruff format
uv run pyrefly check
uv run zuban check
uv run zuban mypy
uv run vulture
uv run pytest
```

Ruff is configured with `select = ["ALL"]` and `unsafe-fixes = true`. Ignored rule sets: `COM812`, `CPY`, `D`, `DOC`.

## Architecture

All application logic lives in `src/croupier/main.py` — a single-module design:

- **Settings** — `pydantic-settings` `BaseSettings` subclass that reads _only_ from `~/.croupier.json` (no env vars, no .env). Fields: `queue_url` (AMQP DSN), `exchange_name`, `queue_name`, `dlx_name`, `dlq_name`.
- **Message** — Pydantic model carrying raw ESC/POS `content: bytes` plus printer network coordinates (`network_host`, `network_timeout`).
- **RabbitRouter** — `faststream.rabbit.fastapi.RabbitRouter` wired to the configured exchange/queue with dead letter routing (DLX/DLQ) for failed messages. The same `handle_message` function serves as both the RabbitMQ subscriber and a `POST /handle-message` HTTP endpoint.
- **Printing** — `handle_message` opens a plain TCP socket to send raw ESC/POS bytes to network printers on port 9100.
- **Health** — `GET /` pings the RabbitMQ broker; returns 204 or 500.
- **Logging** — `RotatingFileHandler` writes to `~/.croupier.log` (10 MB, 5 backups) for `faststream` loggers.

The `croupier` script entry point calls `croupier.main:main`.

## Testing

Tests use `faststream.rabbit.TestRabbitBroker` to simulate RabbitMQ in-memory (no broker needed). The `socket` module is mocked by patching `croupier.main.socket`. pytest-asyncio is configured with `asyncio_mode = "auto"`.

`tests/conftest.py` always overwrites `~/.croupier.json` with test values before collection (backing up any existing file) and restores the original after tests, because `Settings()` runs at module import time.

## Key Dependencies

- **FastAPI** + **uvicorn** — HTTP layer
- **FastStream[rabbit]** — RabbitMQ consumer/producer via `aio-pika`
- **pydantic-settings** — JSON-file-based configuration
