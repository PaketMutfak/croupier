# 1. The producing side owns the receipt queue topology and wire format

Date: 2026-10-19 — Status: accepted

## Context

Croupier consumes Messages published by another system. That system defines the
exchange `receipt.dispatch`, a dead-letter exchange and queue
(`receipt.dispatch.dlx`, `receipt.dispatch.dlq`), and one **Receipt queue** per
branch, `receipt.dispatch.<branch_id>`, declared with dead-letter arguments and
`x-message-ttl`. It also consumes the dead-letter queue and alerts on it.
RabbitMQ rejects re-declaring a queue with different arguments
(`PRECONDITION_FAILED`).

## Decision

- Croupier only consumes. The subscriber binds with `declare=False` and Croupier
  never declares an exchange, queue, DLX or DLQ. `Settings` therefore holds only
  `queue_url` and `queue_name`; the retired `exchange_name`, `dlx_name` and
  `dlq_name` keys are ignored if still present in `~/.croupier.json`.
- A failed Receipt printing must reach the dead-letter queue: errors propagate
  out of the handler and the subscriber uses `AckPolicy.REJECT_ON_ERROR`
  (reject, no requeue), so the queue's dead-letter arguments apply. Do not
  swallow errors or requeue in Croupier.
- The wire format of Message must not change. `content` is the UTF-8 string
  form of the bytes. Producers send ASCII text plus ESC/POS control codes.
  Non-UTF-8 bytes cannot be serialised. (`tests/test_message.py` locks this.)

## Consequences

- `demo.py` publishes to an existing queue (`declare=False`); the queue must be
  created first by whatever owns the topology.
- Changing topology, TTL or the Message payload is a change on the producing
  side first; Croupier follows.
- Base64 or any other content encoding was considered and rejected: it would
  change what goes through RabbitMQ and needs a coordinated producer change.
