# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Croupier

Croupier is a receipt printing microservice. It consumes ESC/POS receipt messages from a RabbitMQ queue and forwards raw bytes to network thermal printers. Receipts arrive only via the queue — there is no HTTP receipt-ingress route. (Operational HTTP surface — `/health/` — is mounted by lite-bootstrap.)

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

Domain terms are defined in `GLOSSARY.md`. Nothing is created at import time. The app is wrapped by [`lite-bootstrap`](https://lite-bootstrap.readthedocs.io/integrations/faststream/).

- `src/croupier/settings.py` — **Settings** (frozen pydantic `BaseModel`) and `load_settings(path=None)`, which reads _only_ the JSON file (default `~/.croupier.json`; no env vars, no .env). Fields: `queue_url` (AMQP DSN), `queue_name` (the **Receipt queue**), optional `sentry_dsn` (`HttpUrl`, default `None`), `sentry_environment` (`Literal["development","staging","production"]`, default `"development"`). Unknown keys, including the retired `exchange_name`, `dlx_name` and `dlq_name`, are ignored. No DSN-shape validation beyond `HttpUrl`; `sentry-sdk` surfaces malformed DSNs as transport warnings at runtime.
- `src/croupier/printing.py` — **Message** (raw ESC/POS `content: bytes` plus `network_host`, `network_timeout`) and **Receipt printing**: `print_receipt(message, connect=connect_network)`. Blocking; always attempts `close()`; errors propagate. The `connect` parameter is the seam: production uses `escpos.Network`, tests use `escpos.printer.Dummy`. `open()` and `_raw()` run inside a `try/finally`; a close failure is narrow-excepted (PEP 758 unparenthesized form, `except OSError, AttributeError:`), logged via `logger.exception` (promoted to a Sentry event) and never replaces the original error — broader exceptions intentionally surface. This is the only place that calls `printer._raw()`.
- `src/croupier/main.py` — `build_broker(settings, connect=...)` builds the `RabbitBroker` whose single `handle_message` subscriber runs `print_receipt` via `asyncio.to_thread`; `create_app(settings=None, connect=...)` wraps it in an `AsgiFastStream` through lite-bootstrap's `FastStreamBootstrapper` (Sentry + structlog); `main()` calls `create_app()`, disables Windows console QuickEdit and runs `uvicorn.run`. Compatible with `uvicorn --factory`. Bootstrap reconfigures global state (structlog, Sentry init); call once per process.
- **SentryMiddleware** — `BaseMiddleware[Any, bytes]` registered on the broker only when `sentry_dsn` is set. Opens a per-message `sentry_sdk.isolation_scope()`, tags it with `correlation_id` (skipped when empty), `logger.exception(...)` on unhandled exceptions (auto-promoted to a Sentry event by `LoggingIntegration`) and re-raises so the reject→DLX/DLQ path still runs. `IgnoredException` is re-raised without capture. Plain passthrough when `sentry_sdk.is_initialized()` is `False`. Exception class is filterable via Sentry's built-in `error.type` index.
- **Per-deployment branching** — `queue_name` is the fleet-unique identifier surfaced as the Sentry `queue_name` tag (`receipt.dispatch.<branch_id>`), so signals stay separable when multiple instances share infrastructure.
- **Health** — `GET /health/` payload comes from lite-bootstrap (no override). There is no HTTP receipt route; receipts arrive only via the queue.
- **Logging** — structlog → JSON on stdout via lite-bootstrap's `LoggingInstrument` (`service_debug=False`). No file handler.
- **Sentry noise control** — `AsyncioIntegration` is intentionally NOT registered (it emitted four `ChannelClosed` events per clean shutdown from cancelled aiormq tasks), and `ignore_logger("faststream")` is called at import so FastStream's own failure log does not duplicate the `SentryMiddleware` event.

**Queue topology is owned by the producing side**: exchange `receipt.dispatch`, per-branch queues, DLX/DLQ and TTL. Croupier only consumes (`declare=False`, `AckPolicy.REJECT_ON_ERROR`) so failures dead-letter to its DLQ, and the Message JSON wire format must not change. See `docs/adr/0001-producers-own-receipt-queue-topology.md`.

`main.py` at the project root is just the entrypoint that calls `croupier.main.main()`.

## Testing

Tests build a broker per test with `build_broker(settings, connect=lambda _m: Dummy())` and assert on the bytes the `Dummy` printer received. `faststream.rabbit.TestRabbitBroker(broker)` simulates RabbitMQ in-memory (no broker needed). Settings come from a `tmp_path` JSON file via the `settings` fixture, so tests never touch `~/.croupier.json`. pytest-asyncio is configured with `asyncio_mode = "auto"`.

Sentry tests use a `_RecordingTransport` plus `sentry_sdk.init(...)` per-test. An autouse `_reset_sentry_global_state` fixture replaces the global client with `NonRecordingClient()` and clears the isolation/global scopes between tests so tag mutations cannot leak. The test settings omit `sentry_dsn`, so the production short-circuit path is exercised by default.

## Key Dependencies

- **uvicorn** — ASGI server (the worker is an `AsgiFastStream` app)
- **FastStream[rabbit]** — RabbitMQ consumer/producer via `aio-pika`
- **lite-bootstrap[faststream-logging,faststream-sentry]** — composes Sentry + structlog behind one `FastStreamConfig` object. Prometheus, OpenTelemetry, and Pyroscope extras intentionally omitted: Croupier runs on branch computers (NAT'd edge hosts) where pull-based scraping is impractical and there are no downstream hops to stitch into traces. Continuous profiling (Pyroscope) was considered and declined in [#46](https://github.com/PaketMutfak/croupier/issues/46): the service is I/O-bound on the printer socket, so profiles add little.
- **sentry-sdk** — error tracking (transitively via `lite-bootstrap`)
- **python-escpos** — ESC/POS printer protocol
- **pydantic** — Settings and Message models
