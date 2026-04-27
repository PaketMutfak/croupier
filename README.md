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

Croupier ships with optional [Sentry](https://sentry.io) integration for error reporting. When enabled, unhandled exceptions in both the FastAPI HTTP layer and the FastStream RabbitMQ subscriber are captured automatically.

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

Leave `sentry_dsn` `null` (or omit) to disable Sentry entirely. No outbound calls, no overhead.

### Settings reference

| Field | Type | Default | Purpose |
|---|---|---|---|
| `sentry_dsn` | `HttpUrl \| null` | `null` | Sentry project DSN. `null` disables Sentry. Validated as a URL at config load. |
| `sentry_environment` | `Literal["development", "staging", "production"]` | `"production"` | Deploy stage. Drives Sentry alert rules and release health. |

`queue_name` doubles as the per-branch identifier in Sentry: it's set as the `queue_name` tag at startup, used in the `printer.id` composite, and seeds the issue fingerprint so identical errors from different branches stay grouped separately.

### What gets captured

- HTTP exceptions on `/handle-message` and `/`
- RabbitMQ subscriber exceptions (printer failures, broker errors, validation errors)
- Tags: `queue_name` (process-wide), `printer.id` (`<queue_name>:<network_host>`, per-message; fleet-unique because branches share RFC 1918 ranges)
- Fingerprint: includes `queue_name` so identical errors from different branches stay grouped separately
- Context: printer host, timeout, payload size

### Capture architecture

- **HTTP path**: handled by Sentry's auto-enabled `FastApiIntegration` + `StarletteIntegration` (no explicit registration).
- **AMQP path**: a custom `SentryMiddleware(BaseMiddleware)` wraps each subscriber message in `sentry_sdk.isolation_scope()`, calls `capture_exception` on unhandled exceptions, then re-raises so FastStream's default NACK → DLQ flow runs.
- `IgnoredException` is excluded from capture (FastStream uses it for normal control flow).

### Branch deployment tip

Croupier deploys per restaurant location. Each branch already has a unique `queue_name` (one queue per branch), so that doubles as the location identifier in Sentry — no extra setting needed. Keep `sentry_environment` aligned with deploy stage (`development` / `staging` / `production`); filter errors by the `queue_name` tag in Sentry to isolate per-location issues without fragmenting release-health stats.
