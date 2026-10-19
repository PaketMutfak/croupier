# Glossary

**Message** — What Croupier consumes: raw ESC/POS `content` bytes plus the printer's network coordinates (`network_host`, `network_timeout`). Arrives only from the RabbitMQ queue.

**Receipt printing** — Sending a Message's bytes to its printer over the network and closing the connection. Blocking; failures propagate to the caller. Lives in `croupier.printing`.

**Receipt queue** — The per-branch RabbitMQ queue `receipt.dispatch.<branch_id>` that one Croupier instance consumes. Declared and owned by the producing side, together with its exchange and dead-letter setup (see `docs/adr/0001`). A Message that fails to print is rejected into the dead-letter queue.

**Settings** — Service configuration (`queue_url`, `queue_name`) read only from a JSON file (default `~/.croupier.json`). Lives in `croupier.settings`.
