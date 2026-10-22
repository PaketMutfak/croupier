# Queue topology

Consolidated from the former `docs/adr/0001` (accepted 2026-10-19).

## The producing side owns the receipt queue topology

**Id:** 244f3606-40a7-4776-a4f3-a5a60a7728c7
**Type:** decision
**Status:** active
**Evidence:** confirmed
**Source:** accepted decision record `docs/adr/0001` (2026-10-19), now consolidated here
**Revisit when:** Croupier starts publishing messages, or the producing side hands queue declaration to consumers

Croupier only consumes. The subscriber binds with `declare=False` and Croupier never declares an exchange, queue, DLX or DLQ. `Settings` holds only `queue_url` and `queue_name`; the retired `exchange_name`, `dlx_name` and `dlq_name` keys are ignored if still present in `~/.croupier.json`.

**Context:** the producing system defines the exchange `receipt.dispatch`, a dead-letter exchange and queue (`receipt.dispatch.dlx`, `receipt.dispatch.dlq`), and one Receipt queue per branch, `receipt.dispatch.<branch_id>`, declared with dead-letter arguments and `x-message-ttl`. It also consumes the dead-letter queue and alerts on it.

**Reason:** RabbitMQ rejects re-declaring a queue with different arguments (`PRECONDITION_FAILED`), so a consumer that declares its own copy of the topology would collide with the producer's.

**Consequence:** `demo.py` publishes to an existing queue (`declare=False`), so the queue must be created first by whatever owns the topology. Changing topology, TTL or the Message payload is a change on the producing side first; Croupier follows.

**Rejected alternative:** Croupier declaring its own exchange, queue, DLX and DLQ (the earlier configurable `exchange_name`, `dlx_name` and `dlq_name` settings). Retired for the collision described above.

## Failed printing is rejected, never swallowed or requeued

**Id:** 5a65f37a-8d99-4df4-b9e1-50ad140ff0f4
**Type:** decision
**Status:** active
**Evidence:** confirmed
**Source:** accepted decision record `docs/adr/0001` (2026-10-19), now consolidated here
**See:** queue-topology.md#the-producing-side-owns-the-receipt-queue-topology — 244f3606-40a7-4776-a4f3-a5a60a7728c7 — as of 2026-10-22

A failed Receipt printing must reach the dead-letter queue. Errors propagate out of the handler and the subscriber uses `AckPolicy.REJECT_ON_ERROR` (reject, no requeue), so the queue's dead-letter arguments apply.

**Reason:** the dead-letter arguments live on the producer-owned queue, and the producing side consumes and alerts on the DLQ; swallowing the error or requeueing in Croupier would hide failed receipts from that path.

**Consequence:** Croupier must not swallow errors or requeue.

## The Message wire format does not change

**Id:** 98aa28ee-5be9-4e74-9e77-3333920dbf09
**Type:** constraint
**Status:** active
**Evidence:** confirmed
**Source:** accepted decision record `docs/adr/0001` (2026-10-19), now consolidated here; `tests/test_message.py` locks it
**See:** queue-topology.md#the-producing-side-owns-the-receipt-queue-topology — 244f3606-40a7-4776-a4f3-a5a60a7728c7 — as of 2026-10-22
**Revisit when:** the producing side agrees to a coordinated payload change

`content` is the UTF-8 string form of the bytes. Producers send ASCII text plus ESC/POS control codes. Non-UTF-8 bytes cannot be serialised.

**Rejected alternative:** Base64 or any other content encoding. It would change what goes through RabbitMQ and needs a coordinated producer change.
