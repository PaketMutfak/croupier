# Observability

Sources for this file: the maintained notes in `CLAUDE.md` and the comments at the code sites named in each entry.

## AsyncioIntegration is deliberately not registered

**Id:** e1733c91-7e0a-41c7-8711-ffb2dc105e5c
**Type:** workaround
**Status:** active
**Evidence:** confirmed
**Source:** `CLAUDE.md` (Sentry noise control); comment in `create_app` in `src/croupier/main.py`
**Revisit when:** the aiormq/FastStream shutdown no longer cancels channel tasks noisily, or Sentry's asyncio integration stops reporting them

Sentry's `AsyncioIntegration` is left out of the integrations passed through lite-bootstrap.

**Reason:** on every SIGTERM shutdown it reported the cancelled aiormq channel tasks as four `ChannelClosed` error events per process, even on a clean shutdown.

**Consequence:** the auto-enabled `LoggingIntegration` (ERROR-and-up log records) is what emits events, which is exactly how `SentryMiddleware` reports failures.

## The faststream logger is ignored by Sentry

**Id:** 1313b9f5-b8c7-4471-a293-be230e633067
**Type:** decision
**Status:** active
**Evidence:** confirmed
**Source:** `CLAUDE.md` (Sentry noise control); comment above `ignore_logger("faststream")` in `src/croupier/main.py`
**Revisit when:** `SentryMiddleware` is removed or FastStream changes how it logs handler failures

`ignore_logger("faststream")` runs at import time, so breadcrumbs and events from that logger are dropped.

**Reason:** FastStream logs every handler failure on its own `faststream` logger. The auto-enabled `LoggingIntegration` would promote that ERROR record to a second event with no exception and no `correlation_id`, duplicating the one `SentryMiddleware` already emits. The middleware event is the canonical one.

## Prometheus, OpenTelemetry and Pyroscope extras are omitted

**Id:** d00762d2-32a8-49e8-870b-84a828e16601
**Type:** decision
**Status:** active
**Evidence:** confirmed
**Source:** `CLAUDE.md` (Key Dependencies); tracking issue #46 for profiling
**Revisit when:** Croupier is deployed somewhere pull-based scraping works, gets downstream hops to trace, or issue #46 (continuous profiling with Pyroscope) is picked up

Only the `faststream-logging` and `faststream-sentry` extras of lite-bootstrap are used.

**Reason:** Croupier runs on branch computers, which are NAT'd edge hosts where pull-based scraping is impractical, and it has no downstream hops to stitch into traces.

**Rejected alternative:** Prometheus metrics and OpenTelemetry tracing, for the reasons above. Continuous profiling with Pyroscope is not rejected, only deferred; it is tracked as future work in issue #46.
