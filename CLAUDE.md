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

All application logic lives in `src/croupier/main.py` — a single-module design wrapped by [`lite-bootstrap`](https://lite-bootstrap.readthedocs.io/integrations/faststream/):

- **Settings** — `pydantic-settings` `BaseSettings` subclass that reads _only_ from `~/.croupier.json` (no env vars, no .env). Fields: `queue_url` (AMQP DSN), `queue_name`, optional `sentry_dsn` (`HttpUrl`, default `None`), `sentry_environment` (`Literal["development","staging","production"]`, default `"development"`). `model_config` sets `frozen=True` and `extra="ignore"` (legacy `exchange_name`/`dlx_name`/`dlq_name` keys in older JSON files are silently ignored). No DSN-shape validation beyond `HttpUrl`; `sentry-sdk` surfaces malformed DSNs as transport warnings at runtime.
- **Per-deployment branching** — `queue_name` is the fleet-unique identifier surfaced as the Sentry `queue_name` tag. Append a branch suffix per deployment (e.g. `receipt.dispatch.istanbul-1`) so signals stay separable when multiple instances share infrastructure. The shipped `.croupier.json` carries this suffix as the canonical example.
- **Message** — Pydantic model carrying raw ESC/POS `content: bytes` plus printer network coordinates (`network_host`, `network_timeout`).
- **Broker** — `faststream.rabbit.RabbitBroker` (no FastAPI). `handle_message` is registered as a `@broker.subscriber` against the queue declared externally (`declare=False`). DLX/DLQ routing relies on the broker-side queue policy plus the FastStream NACK path; `SentryMiddleware` re-raises so NACK still fires.
- **SentryMiddleware** — Custom `BaseMiddleware[Any, bytes]` registered conditionally on the broker (only when `sentry_dsn` is set). Opens a per-message `sentry_sdk.isolation_scope()`, tags it with `correlation_id` (from `StreamMessage.correlation_id`, indexed for log↔event pivot), then `logger.exception(...)` on unhandled exceptions (auto-promoted to a Sentry event by the SDK's `LoggingIntegration`) and re-raises so FastStream's NACK→DLX/DLQ path still runs. `IgnoredException` is re-raised without capture. Short-circuits to a plain passthrough when `sentry_sdk.is_initialized()` is `False`. Exception class is filterable in the Sentry UI via the built-in `error.type` index — no custom tag needed.
- **Printing** — Uses `python-escpos` `Network` printer. `open()` and `_raw()` run inside a `try/finally` so a half-open socket from a failed connect still gets a `close()` attempt. Close-failure narrow-except uses PEP 758 unparenthesized form (`except OSError, AttributeError:`) — broader exceptions intentionally surface (programming-bug visibility trade-off).
- **Health** — `GET /health/` — payload comes from lite-bootstrap (no override).
- **Logging** — structlog → JSON on stdout via lite-bootstrap's `LoggingInstrument` (`service_debug=False`). No file handler, no rotating logs.
- **AsyncioIntegration** — Registered explicitly via `FastStreamConfig.sentry_integrations` (not in sentry-sdk's default set); catches unhandled exceptions in background asyncio tasks.

`main.py` at the project root is just the entrypoint that calls `croupier.main.main()`, which runs `uvicorn.run(create_app())`.

`create_app() -> AsgiFastStream` is the ASGI app factory: builds the `FastStreamConfig`, runs it through `_Bootstrapper` (a `FastStreamBootstrapper` subclass), and returns the bootstrapped ASGI app. Compatible with `uvicorn --factory`: `uvicorn croupier.main:create_app --factory`. Bootstrap reconfigures global state (structlog, Sentry init); call once per process.

### lite-bootstrap 0.28.0 workarounds

The pinned `lite-bootstrap` release has two NameError bugs in the FastStream bootstrapper path that the `prometheus` / `opentelemetry` extras would mask. Croupier intentionally omits both extras (edge-host deployment, see Key Dependencies), so the bugs are reachable and are pinned in code:

1. **`FastStreamPrometheusInstrument` `__init__` crash** — the dataclass `default_factory` calls `prometheus_client.CollectorRegistry()` before `check_dependencies()` is consulted; the symbol is unbound when the extra is missing. Worked around by `_Bootstrapper`, a `FastStreamBootstrapper` subclass whose `instruments_types` whitelists only `FastStreamSentryInstrument`, `FastStreamHealthChecksInstrument`, `FastStreamLoggingInstrument` — the three that match the declared extras.
2. **Health-check `tracer` reference** — `FastStreamHealthChecksInstrument.bootstrap` references the module-level `tracer` symbol whenever `opentelemetry_generate_health_check_spans` is `True` (the upstream default). The symbol is gated behind `is_opentelemetry_installed`. Worked around by passing `opentelemetry_generate_health_check_spans=False` on the `FastStreamConfig` in `create_app`.

Both are short, commented in `main.py`, and should be deleted when upstream evaluates `check_dependencies()` before instantiation and guards the `tracer` reference.

## Testing

Tests use `faststream.rabbit.TestRabbitBroker` to simulate RabbitMQ in-memory (no broker needed). The `Network` printer is mocked by patching `croupier.main.Network` (not `escpos.printer.Network`). pytest-asyncio is configured with `asyncio_mode = "auto"`.

Sentry tests use a `_RecordingTransport` plus `sentry_sdk.init(...)` per-test. An autouse `_reset_sentry_global_state` fixture replaces the global client with `NonRecordingClient()` and clears the isolation/global scopes between tests so tag mutations cannot leak.

`tests/conftest.py` always overwrites `~/.croupier.json` with test values before collection (backing up any existing file) and restores the original after tests, because `Settings()` runs at module import time. Test config omits `sentry_dsn` (defaults to `None`) so the production short-circuit path is exercised by default. `sentry_environment` defaults to `"development"`.

## Key Dependencies

- **uvicorn** — ASGI server (the worker is an `AsgiFastStream` app)
- **FastStream[rabbit]** — RabbitMQ consumer/producer via `aio-pika`
- **lite-bootstrap[faststream-logging,faststream-sentry]** — composes Sentry + structlog behind one `FastStreamConfig` object. Prometheus, OpenTelemetry, and Pyroscope extras intentionally omitted: Croupier runs on branch computers (NAT'd edge hosts) where pull-based scraping is impractical and there are no downstream hops to stitch into traces. Continuous profiling (Pyroscope) is tracked as future work in [#46](https://github.com/PaketMutfak/croupier/issues/46).
- **sentry-sdk** — error tracking (transitively via `lite-bootstrap`)
- **python-escpos** — ESC/POS printer protocol
- **pydantic-settings** — JSON-file-based configuration
