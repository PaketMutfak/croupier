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

Leave `sentry_dsn` set to `null` (the default), or omit the key, to disable Sentry entirely.

### Settings reference

| Field | Type | Default | Purpose |
|---|---|---|---|
| `sentry_dsn` | `HttpUrl \| null` | `null` | Sentry project DSN. `null` disables Sentry. Validated as a URL at config load. |
| `sentry_environment` | `Literal["development", "staging", "production"]` | `"production"` | Deploy stage. Drives Sentry alert rules and release health. |

`queue_name` doubles as the per-branch identifier in Sentry: it's set as the `queue_name` tag at startup, used in the `printer.id` composite, and appended to Sentry's default issue fingerprint so identical errors from different branches stay grouped separately.

### What gets captured

- Unhandled exceptions raised inside any FastAPI route handler (e.g. `POST /handle-message`)
- RabbitMQ subscriber exceptions (printer failures, broker errors, validation errors)
- Tags: `queue_name` (process-wide), `printer.id` (`<queue_name>:<network_host>`, per-message; the `queue_name` prefix makes it fleet-unique because `network_host` alone collides across branches that reuse RFC 1918 ranges)
- Fingerprint: appends `queue_name` to Sentry's default so identical errors from different branches stay grouped separately
- Context: printer host, timeout, payload size

### Capture architecture

- **HTTP path**: handled by Sentry's auto-enabled FastAPI/Starlette integrations.
- **AMQP path**: a FastStream middleware captures unhandled exceptions and re-raises so FastStream's NACK → DLQ routing still runs.
- `IgnoredException` is excluded from capture (FastStream uses it for normal control flow).

### Branch deployment tip

Croupier deploys per restaurant location with one queue per branch, so `queue_name` already identifies the location in Sentry. Filter by the `queue_name` tag to isolate per-location issues.
