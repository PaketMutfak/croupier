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
uv run main
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
- A `/` health endpoint that pings the broker
- A `/metrics` Prometheus endpoint exporting per-message FastStream counters / histograms
- An ASGI surface (`AsgiFastStream`) so the worker runs under `uvicorn`

There is no FastAPI HTTP route. Receipts arrive only via the RabbitMQ queue declared at startup. The `POST /handle-message` route that earlier versions exposed has been removed.

OpenTelemetry tracing and Pyroscope continuous profiling are wired but inert until their endpoints are configured: pass `opentelemetry_endpoint=` (and an OTLP collector URL) and/or `pyroscope_endpoint=` to the `FastStreamConfig` call in `main()`. The middleware classes are already plumbed in, so activation is a single config edit.

## Error Tracking (Optional)

Croupier ships with optional [Sentry](https://sentry.io) integration. `lite-bootstrap` calls `sentry_sdk.init` only when `sentry_dsn` is set in `~/.croupier.json`; otherwise no Sentry code path runs. PII posture (sample rates, breadcrumb buffer, default integrations) follows `lite-bootstrap`'s defaults.

### Enable

Add `sentry_dsn` to `~/.croupier.json`:

```json
{
    "queue_url": "amqp://guest:guest@127.0.0.1",
    "exchange_name": "receipt.dispatch",
    "queue_name": "receipt.dispatch",
    "dlx_name": "receipt.dispatch.dlx",
    "dlq_name": "receipt.dispatch.dlq",
    "sentry_dsn": "https://<key>@<org>.ingest.sentry.io/<project>",
    "sentry_environment": "production"
}
```

Leave `sentry_dsn` set to `null` (the default), or omit the key, to disable Sentry entirely.

### Settings reference

| Field | Type | Default | Purpose |
|---|---|---|---|
| `sentry_dsn` | `HttpUrl \| null` | `null` | Sentry project DSN. `null` disables Sentry; in that case `lite-bootstrap` skips `sentry_sdk.init` and `SentryMiddleware` is not registered on the broker. |
| `sentry_environment` | `Literal["development", "staging", "production"] \| null` | `null` | Deploy stage; mapped to `service_environment` on `FastStreamConfig` so it propagates to Sentry events, the health endpoint payload, and (when added later) OpenTelemetry resource attributes. |

`queue_name` is reused as the per-deployment identifier across all Sentry signals (tag, `printer.id` composite, fingerprint seed) — see the bullets below for details.

### What gets captured

- RabbitMQ subscriber exceptions, including:
    - In-handler errors (printer connection failures, broker errors, `python-escpos` faults raised from `handle_message`)
    - Payload-decode errors (Pydantic `ValidationError` raised before `handle_message` runs because the AMQP body did not match the `Message` schema)
- Unhandled exceptions in background asyncio tasks, via `AsyncioIntegration`
- Tags:
    - `queue_name` (process-wide, set by `lite-bootstrap` via `FastStreamConfig.sentry_tags`)
    - `printer.id` (`<queue_name>:<network_host>`, set inside `handle_message`; the `queue_name` prefix makes it fleet-unique because `network_host` alone collides across branches that reuse RFC 1918 ranges)
    - `error.class` (set in `SentryMiddleware` to `type(exc).__name__` before capture; lets operators distinguish payload-decode failures from in-handler errors at a glance)
- Fingerprint: inside `handle_message`, the isolation scope's fingerprint is set to `["{{ default }}", queue_name]`, so identical printer/payload errors from different deployments stay grouped separately. Payload-decode errors (raised before `handle_message` runs) use Sentry's default fingerprint.
- Context (`printer`): `host`, `timeout`, `payload_size` — set inside `handle_message`.
- Breadcrumbs: two breadcrumbs are recorded under `category="printer"` — one with `message="open"` (data: `host`, `port`, `timeout`; `port` is read from the `Network` printer instance, defaulting to 9100) and one with `message="raw"` (data: `bytes`) — so the captured event shows whether the failure was at TCP connect or after data started flowing.

### Capture architecture

- **AMQP path**: a custom FastStream broker middleware (`SentryMiddleware`) captures unhandled exceptions and re-raises so FastStream's NACK → DLQ routing still runs. The middleware is registered on the broker only when `sentry_dsn` is set in config; when `lite-bootstrap` later finds Sentry inactive (`sentry_dsn=null`, init failure, or transport disabled), `SentryMiddleware` short-circuits via `sentry_sdk.get_client().is_active()` — no isolation-scope wrapping, no capture call, just a passthrough to `call_next`.
- **Asyncio safety net**: `AsyncioIntegration` is registered through `FastStreamConfig.sentry_integrations` so any unhandled exception in a background asyncio task surfaces in Sentry — defense in depth in case an exception ever escapes the middleware.
- **Logs as breadcrumbs**: `LoggingIntegration` is registered through `FastStreamConfig.sentry_integrations` with `level=INFO` and `event_level=CRITICAL`. INFO-and-up log records (including python-escpos's connection logs and Croupier's own log messages) become breadcrumbs on the captured event; auto-promotion of ERROR logs to standalone Sentry events is silenced so we do not double-fire alongside the explicit `capture_exception` calls in `SentryMiddleware` and in the `printer.close()` finally block. **Maintainer caveat**: a future `logger.error("...")` call will _not_ auto-promote to a Sentry event under this configuration; pair any error log that should reach Sentry with an explicit `sentry_sdk.capture_exception()` / `capture_message()`.
- `IgnoredException` (FastStream's control-flow signal for ack/nack/reject) is excluded from capture.

### Deployment tip

If you deploy one queue per logical group (per restaurant, per tenant, per region), the `queue_name` tag becomes that group's filter in Sentry — no extra setting required.
