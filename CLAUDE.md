# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Croupier

Croupier is a receipt printing microservice. It consumes ESC/POS receipt messages from a RabbitMQ queue and forwards raw bytes to network thermal printers. It also exposes the same handler as a REST endpoint via FastAPI.

## Python Version

Requires Python **3.14+** (`requires-python = ">=3.14"`).

## Commands

```bash
uv sync                  # Install dependencies
uv run main.py           # Start the server (reads ~/.croupier.json)
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

Domain terms are defined in `GLOSSARY.md`. Nothing is created at import time.

- `src/croupier/settings.py` — **Settings** (pydantic `BaseModel`) and `load_settings(path=None)`, which reads _only_ the JSON file (default `~/.croupier.json`; no env vars, no .env). Fields: `queue_url` (AMQP DSN) and `queue_name` (the **Receipt queue**). Unknown keys, including the retired `exchange_name`, `dlx_name` and `dlq_name`, are ignored.
- `src/croupier/printing.py` — **Message** (raw ESC/POS `content: bytes` plus `network_host`, `network_timeout`) and **Receipt printing**: `print_receipt(message, connect=connect_network)`. Blocking; always closes the printer; errors propagate. The `connect` parameter is the seam: production uses `escpos.Network`, tests use `escpos.printer.Dummy`. This is the only place that calls `printer._raw()` (python-escpos has no public raw-bytes method).
- `src/croupier/main.py` — `build_app(settings, connect=...)` assembles the FastAPI app: a `RabbitRouter` whose single `handle_message` is both the queue subscriber and `POST /handle-message` (runs `print_receipt` via `asyncio.to_thread`), `GET /` health (pings the broker; 204 or 500), and CORS. The broker is exposed as `app.state.broker`. `main()` loads settings, sets up logging (`RotatingFileHandler` to `~/.croupier.log`, 10 MB, 5 backups, `faststream` loggers) and runs uvicorn.

**Queue topology is owned by the producing side**: exchange `receipt.dispatch`, per-branch queues, DLX/DLQ and TTL. Croupier only consumes (`declare=False`, `AckPolicy.REJECT_ON_ERROR`) so failures dead-letter to its DLQ, and the Message JSON wire format must not change. See `docs/adr/0001-producers-own-receipt-queue-topology.md`.

`main.py` at the project root is just the entrypoint that calls `croupier.main.main()`.

## Testing

Tests build an app per test with `build_app(settings, connect=lambda _m: Dummy())` and assert on the bytes the `Dummy` printer received. `faststream.rabbit.TestRabbitBroker(app.state.broker)` simulates RabbitMQ in-memory (no broker needed) and Starlette's `TestClient` drives the HTTP routes. Settings come from a `tmp_path` JSON file via the `settings` fixture, so tests never touch `~/.croupier.json`. pytest-asyncio is configured with `asyncio_mode = "auto"`.

## Key Dependencies

- **FastAPI** + **uvicorn** — HTTP layer
- **FastStream[rabbit]** — RabbitMQ consumer/producer via `aio-pika`
- **python-escpos** — ESC/POS printer protocol
- **pydantic** — Settings and Message models
