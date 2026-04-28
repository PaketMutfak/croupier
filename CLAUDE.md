# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Croupier

Croupier is a receipt printing microservice. It consumes ESC/POS receipt messages from a RabbitMQ queue and forwards raw bytes to network thermal printers. Receipts arrive only via the queue — there is no HTTP receipt-ingress route. (Operational HTTP surface — `/health/` and `/metrics` — is mounted by lite-bootstrap.)

## Python Version

Requires Python **3.14+** (`requires-python = ">=3.14"`).

## Commands

```bash
uv sync                  # Install dependencies
uv run main.py           # Start the worker (reads ~/.croupier.json, serves on uvicorn)
uv run pytest            # Run all tests
uv run pytest tests/test_main.py::TestHandleMessageSubscriber  # Run a single test class
uv run pytest -k "isolation_scope"                              # Run tests matching a pattern
```

### Lint / CI

The full CI pipeline (run manually; no task-runner config in the repo):

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

All application logic lives in `src/croupier/main.py` — a single-module design wrapped by [`lite-bootstrap`](https://lite-bootstrap.readthedocs.io/integrations/faststream/):

- **Settings** — `pydantic-settings` `BaseSettings` subclass that reads _only_ from `~/.croupier.json` (no env vars, no .env). Fields: `queue_url` (AMQP DSN), `exchange_name`, `queue_name`, `dlx_name`, `dlq_name`, optional `sentry_dsn` (`HttpUrl`, default `None`), `sentry_environment` (`Literal["development","staging","production"]`, default `"development"`). `model_config` sets `frozen=True` and `extra="ignore"`. No DSN-shape validation beyond `HttpUrl`; `sentry-sdk` surfaces malformed DSNs as transport warnings at runtime.
- **Per-deployment branching** — `queue_name` is the fleet-unique identifier reused across the Sentry `queue_name` tag, the `printer.id` composite (`<queue_name>:<network_host>`), and the `handle_message` fingerprint seed. Append a branch suffix per deployment (e.g. `receipt.dispatch.istanbul-1`) so signals stay separable when multiple instances share infrastructure. The shipped `.croupier.json` carries this suffix as the canonical example.
- **Message** — Pydantic model carrying raw ESC/POS `content: bytes` plus printer network coordinates (`network_host`, `network_timeout`).
- **Broker** — `faststream.rabbit.RabbitBroker` (no FastAPI). `handle_message` is registered as a `@broker.subscriber` against the queue declared externally (`declare=False`). DLX/DLQ routing relies on the broker-side queue policy plus the FastStream NACK path; `SentryMiddleware` re-raises so NACK still fires.
- **SentryMiddleware** — Custom `BaseMiddleware[Any, bytes]` registered conditionally on the broker (only when `sentry_dsn` is set). Opens a per-message `sentry_sdk.isolation_scope()`, tags `error.class`, calls `logger.exception`, and re-raises. Short-circuits to a passthrough when `sentry_sdk.get_client().is_active()` is `False`.
- **Printing** — Uses `python-escpos` `Network` printer. `open()` and `_raw()` run inside a `try/finally` so a half-open socket from a failed connect still gets a `close()` attempt. Close-failure narrow-except uses PEP 758 unparenthesized form (`except OSError, AttributeError:`) — broader exceptions intentionally surface (programming-bug visibility trade-off).
- **Health** — `GET /health/` — payload comes from lite-bootstrap (no override).
- **Metrics** — `/metrics` Prometheus endpoint exporting per-message FastStream counters/histograms via `RabbitPrometheusMiddleware`.
- **Logging** — structlog → JSON on stdout via lite-bootstrap's `LoggingInstrument` (`service_debug=False`). No file handler, no rotating logs.
- **OpenTelemetry / Pyroscope** — `RabbitTelemetryMiddleware` is wired and the `lite-bootstrap[pyroscope]` extra is installed; both stay inert until their endpoints are configured on `FastStreamConfig`.
- **AsyncioIntegration** — Registered explicitly via `FastStreamConfig.sentry_integrations` (not in sentry-sdk's default set); catches unhandled exceptions in background asyncio tasks.

`main.py` at the project root is just the entrypoint that calls `croupier.main.main()`, which runs `uvicorn.run(create_app())`.

`create_app() -> AsgiFastStream` is the ASGI app factory: builds the `FastStreamConfig`, runs it through `FastStreamBootstrapper`, and returns the bootstrapped ASGI app. Compatible with `uvicorn --factory`: `uvicorn croupier.main:create_app --factory`. Bootstrap reconfigures global state (structlog, Sentry init); call once per process.

## Testing

Tests use `faststream.rabbit.TestRabbitBroker` to simulate RabbitMQ in-memory (no broker needed). The `Network` printer is mocked by patching `croupier.main.Network` (not `escpos.printer.Network`). pytest-asyncio is configured with `asyncio_mode = "auto"`.

Sentry tests use a `_RecordingTransport` plus `sentry_sdk.init(...)` per-test. An autouse `_reset_sentry_global_state` fixture replaces the global client with `NonRecordingClient()` and clears the isolation/global scopes between tests so fingerprint/tag mutations cannot leak.

`tests/conftest.py` always overwrites `~/.croupier.json` with test values before collection (backing up any existing file) and restores the original after tests, because `Settings()` runs at module import time. Test config omits `sentry_dsn` (defaults to `None`) so the production short-circuit path is exercised by default. `sentry_environment` defaults to `"development"`.

## Key Dependencies

- **uvicorn** — ASGI server (the worker is an `AsgiFastStream` app)
- **FastStream[rabbit]** — RabbitMQ consumer/producer via `aio-pika`
- **lite-bootstrap[faststream-all,pyroscope]** — composes Sentry + structlog + Prometheus + OTel + Pyroscope behind one `FastStreamConfig` object
- **sentry-sdk** — error tracking (transitively via `lite-bootstrap`)
- **python-escpos** — ESC/POS printer protocol
- **pydantic-settings** — JSON-file-based configuration
