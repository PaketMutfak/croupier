# Croupier

## Demo

### 1. Install dependencies

```bash
uv sync
```

### 2. Create the configuration file

Croupier reads its settings from `~/.croupier.json`. Copy the example config shipped with the repo:

```bash
cp .croupier.json ~/.croupier.json
```

Edit the file if your RabbitMQ instance is running on a different host or with different credentials.

### 3. Start the server

```bash
uv run main.py
```

The server will start listening for messages on the configured RabbitMQ queue.

### 4. Update `network_host` and run the demo

Open `demo.py` and change the `network_host` value to the IP address of your ESC/POS network printer:

```python
message = Message(
    content=build_receipt(),
    network_host="<YOUR_PRINTER_IP>",  # e.g. "192.168.1.114"
    network_timeout=10,
)
```

Then, in a separate terminal, publish a test receipt:

```bash
uv run demo.py
```

## Architecture

Croupier is a FastStream RabbitMQ subscriber wrapped by [`lite-bootstrap`](https://lite-bootstrap.readthedocs.io/integrations/faststream/), which provides:

- Sentry init (opt-in, see below)
- structlog → JSON-on-stdout logging (replaces the previous `~/.croupier.log` file handler)
- A `/health/` endpoint that pings the broker (lite-bootstrap default; not overridden)
- An ASGI surface (`AsgiFastStream`) so the worker runs under `uvicorn`

There is no FastAPI HTTP route. Receipts arrive only via the RabbitMQ queue declared at startup. The `POST /handle-message` route that earlier versions exposed has been removed.

Prometheus metrics, OpenTelemetry tracing, and Pyroscope profiling are intentionally not wired. Croupier runs on branch computers (NAT'd edge hosts): a `/metrics` endpoint cannot be scraped from a central Prometheus, and the service is a leaf — AMQP in, raw TCP bytes to the printer — so distributed traces have no downstream hops to stitch. Sentry (push, outbound HTTPS) and structlog JSON-on-stdout are the supported observability surface. Continuous profiling (Pyroscope) is tracked as future work in [#46](https://github.com/PaketMutfak/croupier/issues/46).

## Error Tracking (Optional)

Croupier ships with optional [Sentry](https://sentry.io) integration. `lite-bootstrap` calls `sentry_sdk.init` only when `sentry_dsn` is set in `~/.croupier.json`; otherwise no Sentry code path runs. PII posture (sample rates, breadcrumb buffer, default integrations) follows `lite-bootstrap`'s defaults.

### Enable

Add `sentry_dsn` to `~/.croupier.json`:

```json
{
    "queue_url": "amqp://guest:guest@127.0.0.1",
    "queue_name": "receipt.dispatch.istanbul-1",
    "sentry_dsn": "https://<key>@<org>.ingest.sentry.io/<project>",
    "sentry_environment": "production"
}
```

The `queue_name` carries a branch suffix (`.istanbul-1`) so the string identifies the deployment in the Sentry tag. See "Per-deployment branching" below.

Leave `sentry_dsn` set to `null` (the default), or omit the key, to disable Sentry entirely. `sentry_environment` defaults to `"development"`; override to `"staging"` or `"production"` when deploying so Sentry's per-stage alert rules route correctly. The field is also passed to `FastStreamConfig.service_environment` when no DSN is set, so it surfaces in the health endpoint payload regardless of Sentry state.

Per-deployment branching: when running multiple instances against shared infrastructure, append a dot-delimited branch suffix to `queue_name` (e.g. `receipt.dispatch.istanbul-1`). The string surfaces as the Sentry `queue_name` tag, so a unique value per deployment keeps signals separable across the fleet.

### Settings reference

| Field | Type | Default | Purpose |
|---|---|---|---|
| `sentry_dsn` | `HttpUrl \| null` | `null` | Sentry project DSN. `null` disables Sentry; in that case `lite-bootstrap` skips `sentry_sdk.init` and `SentryMiddleware` is not registered on the broker. Validated only as an `HttpUrl` at config load — Sentry-specific shape (public key, project id) is left to `sentry-sdk`'s own runtime warnings. |
| `sentry_environment` | `Literal["development", "staging", "production"]` | `"development"` | Deploy stage; mapped to `service_environment` on `FastStreamConfig` so it propagates to Sentry events and the health endpoint payload. Default keeps the smallest config valid; override per deploy stage so Sentry alert routing matches reality. |

`queue_name` is the per-deployment identifier surfaced as the Sentry `queue_name` tag — see the bullets below for details.

### What gets captured

- RabbitMQ subscriber exceptions, including:
    - In-handler errors (printer connection failures, broker errors, `python-escpos` faults raised from `handle_message`)
    - Payload-decode errors (Pydantic `ValidationError` raised before `handle_message` runs because the AMQP body did not match the `Message` schema)
- Unhandled exceptions in background asyncio tasks, via `AsyncioIntegration`
- Tags:
    - `queue_name` (process-wide, set by `lite-bootstrap` via `FastStreamConfig.sentry_tags`)
- Stage-of-failure (open vs `_raw`) is read from the exception traceback location. All Sentry events use Sentry's default fingerprint (exception type + stack frame); per-branch separation is via the `queue_name` tag. Exception class is filterable via Sentry's built-in `error.type` index — no custom tag needed.

### Capture architecture

- **AMQP path**: a custom FastStream broker middleware (`SentryMiddleware`) captures unhandled exceptions and re-raises so FastStream's NACK → DLQ routing still runs. The middleware is registered on the broker only when `sentry_dsn` is set in config; when `sentry_dsn=null` (lite-bootstrap skips `sentry_sdk.init`, leaving the global as a `NonRecordingClient`), `SentryMiddleware` short-circuits via `sentry_sdk.is_initialized()` — no isolation-scope wrapping, no capture call, just a passthrough to `call_next`. A misconfigured-but-syntactically-valid DSN does NOT flip `is_initialized()` to False — `sentry_sdk` installs a real client and surfaces transport-layer failures as warnings, which is loud-by-design. `handle_message` itself does not touch Sentry state — all capture-side work lives in the middleware.
- **Asyncio safety net**: `AsyncioIntegration` is registered through `FastStreamConfig.sentry_integrations` so any unhandled exception in a background asyncio task surfaces in Sentry — defense in depth in case an exception ever escapes the middleware.
- **Logs as breadcrumbs and events**: `lite-bootstrap` leaves `sentry_default_integrations=True`, so sentry-sdk's `LoggingIntegration` is auto-enabled with its defaults — `level=INFO` (records at INFO and above become breadcrumbs on the captured event) and `event_level=ERROR` (records at ERROR and above are auto-promoted to standalone Sentry events). Both `SentryMiddleware` (`logger.exception("message handler failed")`) and the `printer.close()` finally block (`logger.exception("printer close failed")`) rely on that auto-promotion as the capture path; no explicit `sentry_sdk.capture_exception()` calls are needed for unhandled exceptions. **Maintainer caveat**: any future `logger.error(...)` / `logger.exception(...)` _will_ auto-promote to a Sentry event under this configuration. If an INFO-level log record should not be visible in Sentry breadcrumbs, raise the threshold by passing an explicit `LoggingIntegration(level=logging.WARNING, event_level=logging.ERROR)` via `FastStreamConfig.sentry_integrations`.
- `IgnoredException` (FastStream's control-flow signal for ack/nack/reject) is excluded from capture.

### Deployment tip

If you deploy one queue per logical group (per restaurant, per tenant, per region), the `queue_name` tag becomes that group's filter in Sentry — no extra setting required.
