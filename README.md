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

## Error Tracking (Optional)

Croupier ships with optional [Sentry](https://sentry.io) integration for error reporting. When enabled, unhandled exceptions in both the FastAPI HTTP layer and the FastStream RabbitMQ subscriber are captured automatically. If `sentry_sdk.init` raises for any reason — DSN parsing, integration setup, transport bootstrap — the failure is logged to `~/.croupier.log` and Croupier keeps running with Sentry disabled. Receipt printing is never blocked by an observability misconfiguration.

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
| `sentry_dsn` | `HttpUrl \| null` | `null` | Sentry project DSN. `null` disables Sentry. Validated at config load: must include a public key (URL userinfo) and a project id (URL path). |
| `sentry_environment` | `Literal["development", "staging", "production"] \| null` | `null` | Deploy stage. Required when `sentry_dsn` is set; drives Sentry alert rules and release health. |

`queue_name` is reused as the per-deployment identifier across all Sentry signals (tag, `printer.id` composite, fingerprint seed) — see the bullets below for details.

### What gets captured

- Unhandled exceptions raised inside any FastAPI route handler (e.g. `POST /handle-message`)
- RabbitMQ subscriber exceptions (printer connection failures, broker errors)
- Tags: `queue_name` (process-wide), `printer.id` (`<queue_name>:<network_host>`, set inside `handle_message`; the `queue_name` prefix makes it fleet-unique because `network_host` alone collides across branches that reuse RFC 1918 ranges)
- Fingerprint: inside `handle_message`, the isolation scope's fingerprint is set to `["{{ default }}", queue_name]`, so identical printer/payload errors from different deployments stay grouped separately. Errors raised outside the handler use Sentry's default fingerprint.
- Context (`printer`): `host`, `timeout`, `payload_size` — set inside `handle_message`. With `include_local_variables=False`, raw payload bytes never ride along in stack frames.
- Breadcrumbs: `printer.open` (with `host`, `port`, `timeout`) and `printer.raw` (with `bytes`) are recorded around each printer interaction so the captured event shows whether the failure was at TCP connect or after data started flowing.

### Capture architecture

- **HTTP path**: `FastApiIntegration` and `StarletteIntegration` are passed explicitly to `sentry_sdk.init`, so the wiring does not depend on auto-enabling defaults.
- **AMQP path**: a custom FastStream middleware (`SentryMiddleware`) captures unhandled exceptions and re-raises so FastStream's NACK → DLQ routing still runs. The middleware is only registered when `sentry_dsn` is set; with Sentry disabled there is no per-message scope wrapping at all.
- **Asyncio safety net**: `AsyncioIntegration` is registered so any unhandled exception in a background asyncio task surfaces in Sentry — defense in depth in case an exception ever escapes the middleware.
- **Logs as breadcrumbs**: `LoggingIntegration` is registered with `level=INFO` and `event_level=CRITICAL`. INFO-and-up log records (including python-escpos's connection logs and Croupier's own log messages) become breadcrumbs on the captured event; auto-promotion of ERROR logs to standalone Sentry events is silenced so we do not double-fire alongside the explicit `capture_exception` calls in `handle_message`.
- `IgnoredException` (FastStream's control-flow signal for ack/nack/reject) is excluded from capture.

### Deployment tip

If you deploy one queue per logical group (per restaurant, per tenant, per region), the `queue_name` tag becomes that group's filter in Sentry — no extra setting required.
