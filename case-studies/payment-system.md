# Payment System

## Requirements

### Functional
- Accept a payment request (amount, payment method, merchant) and move money from payer to
  payee, via an external payment processor/bank rail.
- Support refunds and partial refunds.
- Idempotent retries — a client resubmitting the same payment request (due to a timeout,
  a double-click) must never result in a double charge.
- Provide a reliable, queryable transaction record for reconciliation and support.

### Non-functional
- Correctness over latency, always — an incorrectly duplicated or lost charge is a much worse
  outcome than a slow response. This inverts the usual "optimize for latency" instinct that
  drives most of the other systems in this repo.
- Must handle partial failure gracefully: the payment processor call can time out without the
  caller knowing whether the charge actually succeeded on the processor's side.
- Auditability: every state transition on a transaction needs to be recorded, not just the
  final status — this is a regulatory and dispute-resolution requirement, not just good
  practice.
- Strong consistency on balance/ledger operations — this is one of the few systems in this
  repo where eventual consistency is not an acceptable default anywhere near the money-moving
  path.

## Capacity Estimation

Assume 10M transactions/day on the platform. 10M / 86,400 ≈ 116 transactions/sec average,
~350/sec at peak (3x factor) — genuinely modest QPS compared to most other systems in this
repo, because payment volume is nowhere near social-media-scale request volume. The design
challenge here is correctness under failure, not raw throughput.

Storage: each transaction record, plus its full state-transition history (created → processing
→ succeeded/failed → possibly refunded), at maybe 2 KB total per transaction including audit
trail. 10M/day × 2 KB = 20 GB/day, ~7 TB/year — small by this repo's standards, and typically
retained for years for compliance (financial records commonly need 7-year retention in many
jurisdictions), which is a data-retention policy decision as much as a technical one.

## API Design

```
POST /v1/payments
  Idempotency-Key: <client-generated UUID>
  { amount, currency, payment_method_token, merchant_id }
  -> 201 { payment_id, status: "processing" }
  -> (same Idempotency-Key retried) -> same 201 response, no new charge

GET  /v1/payments/{id}       -> current status + full state history
POST /v1/payments/{id}/refund  { amount? }   -> partial or full refund
```

The `Idempotency-Key` header is not optional or an afterthought — it's the mechanism the
entire correctness story depends on, required on every write.

## Data Model

```
payments
  payment_id (PK)
  idempotency_key (unique index — this is what prevents double-processing)
  amount, currency, merchant_id, payer_id
  status: created | processing | succeeded | failed | refunded
  processor_reference   (the external processor's own transaction ID, once known)
  created_at, updated_at

payment_events (append-only, full audit trail)
  event_id, payment_id, from_status, to_status, timestamp, metadata

ledger_entries (double-entry bookkeeping)
  entry_id, account_id, amount, direction: debit|credit, payment_id, timestamp
  # every payment produces at least two balanced ledger entries —
  # this is what makes the books provably consistent, independent
  # of the payments table's own status field
```

The `payment_events` and `ledger_entries` tables are append-only by design — nothing is ever
updated or deleted, only new events/entries are added, so the full history is always
reconstructable and auditable. This is a deliberate departure from the "just update a status
field" pattern used elsewhere in this repo, because for money, the history of what happened
matters as much as the current state.

## High-Level Architecture

```
 client ──POST /payments (with Idempotency-Key)──► Payment API
                                                         │
                                          check idempotency_key against
                                          payments table (unique constraint)
                                                         │
                                    already exists? ──yes──► return existing
                                          │                   payment's status,
                                          no                  no new charge
                                          │
                                    create payment row, status=created
                                          │
                                    write payment_event (created)
                                          │
                                          ▼
                                Payment Processing Service
                                          │
                                status -> processing, write event
                                          │
                                    call external processor
                                    (Visa/Mastercard network,
                                     bank rail, or a processor
                                     like Stripe/Adyen upstream)
                                          │
                          success ────────┼──────── ambiguous/timeout
                              │                            │
                    status -> succeeded          reconciliation job:
                    write ledger_entries          poll processor's status
                    write event                    API for the actual
                              │                    outcome (see deep dive),
                              │                    then resolve status
                              ▼                            │
                       notify caller,                      ▼
                    (webhook/callback)              resolve status,
                                                      write event, notify
```

## Deep Dives

**Idempotency.** The `Idempotency-Key` (client-generated, typically a UUID stored by the
client before the first attempt) is checked against a unique index before any processing
starts. If a request with the same key arrives again — because the client's original request
timed out and it retried, or a user double-clicked "pay" — the system returns the existing
payment's current status instead of initiating a second charge. This is the single most
important mechanism in the whole design: without it, network unreliability alone (not even a
bug) guarantees eventual double-charges at any meaningful transaction volume, since clients
retrying on timeout is both expected and correct client behavior that the server must be safe
against.

**Handling ambiguous processor responses.** A call to the external payment processor can fail
in a way that's genuinely ambiguous — a timeout doesn't tell you whether the processor
received and executed the charge before the connection dropped, or never received it at all.
Blindly retrying risks a double charge on the processor's side (outside your own system's
idempotency protection, since this is now the *outbound* call to a system that has its own,
separate idempotency semantics — most processors support their own idempotency keys on
inbound requests for exactly this reason, which should be set to match your internal
payment_id or idempotency_key). Blindly marking it failed risks under-reporting a charge that
did succeed. The correct handling is a reconciliation step: query the processor's own status
API for that transaction reference before taking any further action, and only resolve the
internal payment's status once you have a definitive answer — status stays `processing`
(not `failed`) during this window, specifically to avoid a premature wrong conclusion in
either direction.

**Double-entry ledger.** Rather than trusting a single `status` field as the sole source of
truth for where money is, every payment produces balanced debit/credit entries in a ledger
(money leaves the payer's account, money enters the merchant's account, in matching amounts).
This gives an independent, mathematically checkable consistency property — total debits must
equal total credits system-wide at all times — that a simple status field can't provide, and
is standard practice in any real financial system, not an optional embellishment. Reconciling
the ledger against the processor's own settlement reports (typically a daily batch file) is
the actual mechanism that catches any drift between what your system believes happened and
what the processor's records show.

**Refunds.** Modeled as their own transaction (referencing the original payment_id) with the
same idempotency and ledger discipline, rather than as a mutation of the original payment
record — the original charge stays an immutable historical fact, and a refund is a new,
separate, reversing entry. This matters for audit trails (you want to see "charged $50, then
refunded $20" as two clear events, not a payment record that's been edited to say "$30").

**Webhooks and eventual notification.** Because processor calls can be slow or genuinely
asynchronous (some payment methods settle over minutes or hours, not milliseconds), the
system typically returns `processing` immediately to the caller and later notifies via
webhook once a definitive outcome is known — the caller's own webhook endpoint needs the same
idempotency treatment on the receiving end (dedup by payment_id/event_id), since webhook
delivery itself is commonly at-least-once, not exactly-once.

## Bottlenecks & Scaling

Transaction volume itself is modest relative to most systems in this repo, so this system is
rarely bottlenecked on raw throughput — it's bottlenecked on correctness under partial
failure, which is a design and testing discipline problem more than a scaling problem. The one
place genuine scale pressure shows up is the ledger and reconciliation jobs, which need to
process the full daily transaction volume against processor settlement files in a bounded
batch window (typically overnight) — as transaction volume grows, that reconciliation job's
runtime, not the live payment API's request throughput, is usually the first thing to need
active scaling attention.
