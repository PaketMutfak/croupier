# 2. Sentry events spool to disk until Sentry confirms them

Date: 2026-10-21 — Status: accepted

## Context

Croupier runs on branch computers with poor ethernet that also lose power. The
stock `sentry-sdk` transport keeps events in an in-memory queue and, when a
send fails, counts the loss and drops the event. Anything captured during an
outage, or still queued when the machine loses power, never reaches Sentry.
Logs do not reach disk either (stdout only), so for an outage the spool is the
only on-disk record.

Sentry ships offline buffering for its JavaScript, Electron and .NET SDKs but
not for Python (`getsentry/sentry-python#3514` was closed without
implementation; `getsentry/develop#476` collects the cross-SDK design notes).
The only third-party option, `sentry-offline-transport` 1.0.0, saves an
envelope only *after* a failed send, ignores non-2xx responses, has no size
cap, replays only at startup, deletes before re-sending and writes
non-atomically.

## Decision

`croupier.spool.SpoolTransport`, installed through lite-bootstrap's
`sentry_additional_params={"transport": ...}`:

- Every envelope is written to `~/.croupier-sentry-spool/<dsn-hash>/` (temp
  file, `fsync`, rename) before the send is attempted.
- The file is deleted only on a definitive answer: 2xx, or a permanent 4xx
  (400, 401, 403, 413). A dead link, 429, 5xx, or a send the SDK skipped while
  rate-limited keeps it. `sentry-sdk` does not raise on non-2xx, so the status
  is read by overriding `_request`.
- Leftovers replay at startup and every 60 seconds. A path is claimed under a
  lock before it is written visible or read for replay, so an envelope is never
  sent twice concurrently.
- It never takes the app down: an unusable spool directory at startup degrades
  to the stock in-memory transport, a write failure falls back to a plain send,
  and a file that cannot be read (for example locked by antivirus) is kept
  rather than deleted. Filenames stay in capture order even if the clock jumps
  back after a power loss.
- The spool holds at most 500 files; the oldest are evicted first. Corrupt files
  and client-report envelopes are discarded or never spooled.

## Consequences

- A crash after Sentry accepted an event but before the file is deleted replays
  it as a duplicate. Sentry dedups by event ID; do not add machinery to prevent
  it. Eviction can likewise delete a file that is mid-send, which is harmless.
- The transport depends on private `sentry-sdk` internals: `_worker`,
  `_send_envelope`, `_flush_client_reports` and `_request`. A `sentry-sdk` bump
  that changes them fails `tests/test_spool.py` (including the kill-and-recover
  and stress tests), so a Renovate PR will not go green silently.
- Events replayed after a long outage arrive late but keep their original
  timestamps.
- Every event costs one small synchronous disk write on the capturing thread.
  Croupier's error volume is low.
