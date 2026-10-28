# Ticketmaster-Style Ticket Booking

## Requirements

### Functional
- Browse events, view seat maps and availability.
- Reserve seats temporarily while a user completes checkout (hold, don't sell, during
  payment).
- Purchase (convert a hold into a confirmed, paid booking).
- Release holds that expire without completing payment, back to available inventory.

### Non-functional
- No double-selling a seat — this is the one property the whole system exists to guarantee,
  and it must hold even under extreme concurrent demand.
- Handle massive simultaneous demand spikes at on-sale time (a popular concert can see
  hundreds of thousands of users hitting "buy" within seconds of tickets going live).
- Fairness under contention — some mechanism (queueing) so the system doesn't just favor
  whoever has the fastest connection/script.

## Capacity Estimation

Assume a high-demand on-sale: 500K concurrent users trying to buy tickets for a 20,000-seat
venue within the first minute of sale opening. That's a 25:1 demand-to-supply ratio at peak —
the defining characteristic of this system's load profile, wildly different from its average-
day traffic (which might be a few hundred requests/sec browsing listings). Design for this
spike explicitly; average QPS across a whole day is close to meaningless for capacity planning
here.

Seat-hold contention: in the worst seconds of an on-sale, effectively every request is trying
to acquire a lock on a small set of desirable seats (front rows, aisle seats) simultaneously —
this is a hot-row/hot-key problem at the extreme end, not a generically distributed load
pattern.

Storage: total seat inventory across all events is small (a large venue has tens of thousands
of seats, a platform running thousands of events might have tens of millions of seat records
total) — this is not a storage-scale problem at all, it's entirely a concurrency-control and
burst-throughput problem.

## API Design

```
GET  /v1/events/{id}/seats          -> seat map + availability status
POST /v1/holds                      { event_id, seat_ids[] }
                                     -> 201 { hold_id, expires_at }  (e.g. 10 min TTL)
                                     -> 409 if any seat already held/sold
POST /v1/bookings                   { hold_id, payment_token }
                                     -> 201 { booking_id }  (converts hold to sale)
                                     -> 410 if hold expired before payment completed
POST /v1/queue/join                 { event_id }  -> { queue_position, poll_token }
                                     (virtual waiting room for on-sale spikes — see deep dive)
```

## Data Model

```
seats
  seat_id (PK), event_id, section, row, number
  status: available | held | sold
  hold_id, hold_expires_at   (null when available/sold)
  version   (optimistic concurrency token)

holds
  hold_id (PK), user_id, event_id, seat_ids[], created_at, expires_at

bookings
  booking_id (PK), user_id, event_id, seat_ids[], amount, payment_status, created_at
```

The `version` column on `seats` is the load-bearing detail: every hold/release operation is a
conditional update (`UPDATE seats SET status='held', version=version+1 WHERE seat_id=? AND
version=? AND status='available'`), so two concurrent requests for the same seat can't both
succeed — the second one's conditional update simply affects zero rows and the request fails
with a clear "already taken" response, no locking required.

## High-Level Architecture

```
 on-sale spike:
 client ──► Virtual Waiting Room ──► admits users at a controlled rate
                                       into the booking flow (see deep dive)
                                              │
                                     admitted users proceed to:
                                              │
                                              ▼
                                     Seat Selection Service
                                              │
                                     POST /holds ──► conditional update on
                                                       seats table (optimistic
                                                       concurrency, see above)
                                              │
                                     success ──► Hold created, TTL timer starts
                                              │
                                     client completes payment within TTL window
                                              │
                                     POST /bookings ──► Payment Service (see
                                                          case-studies/payment-system.md)
                                                     ──► on success: seat status
                                                          -> sold, hold_id cleared
                                                     ──► on failure/timeout: seat
                                                          reverts to available
                                                          (background TTL sweep,
                                                           see deep dive)
```

## Deep Dives

**Preventing double-selling.** The core mechanism is optimistic concurrency control on each
seat row, not pessimistic locking — under extreme contention (thousands of requests racing for
the same seat), taking a distributed lock per seat would serialize all of them through lock
acquisition and create its own bottleneck and potential deadlock risk. Optimistic
concurrency (conditional update, check the version/status matched what was expected) lets all
requests attempt the update in parallel; exactly one succeeds per seat, the rest get an
immediate, cheap failure response — no request has to wait on another request's lock to be
released.

**Hold expiry.** A seat held during checkout can't stay held forever if the user abandons the
flow — a TTL (commonly 5-10 minutes) on the hold, with a background sweeper process that
periodically finds expired holds and reverts those seats to `available`. This needs to run
frequently enough that abandoned holds don't meaningfully reduce effective inventory during a
high-demand sale (a sweep interval of seconds, not minutes, during active on-sales), while
being cheap enough (a simple indexed query on `hold_expires_at < now()`) not to itself become
a load problem.

**Virtual waiting room.** For a genuinely oversubscribed on-sale (500K users, 20K seats), even
perfect seat-locking doesn't solve the user experience or backend-load problem of 500K
simultaneous requests hitting the seat-selection service at once. A waiting room admits users
into the actual booking flow at a controlled rate (matching roughly what the seat-selection and
payment backends can actually handle per second) rather than letting everyone in
simultaneously — implemented as a queue (often Redis-backed, assigning each user a position
and periodically admitting the front of the queue), decoupling "how many people want in" from
"how many people are actively hammering the booking backend at once." This is as much a
backend-protection mechanism as it is a fairness feature — without it, the seat-selection
service itself would be the thing that falls over, independent of whether the seat-locking
logic is correct.

**Fairness.** A pure first-come-first-served queue is vulnerable to bots/scripts that connect
faster than any human possibly could, systematically crowding out real users. Mitigations
(CAPTCHA before queue entry, randomized admission within a short window rather than strict
FCFS, rate-limiting by account/device rather than by request) are a real and ongoing arms race
in this space rather than a one-time solved problem — worth acknowledging explicitly rather
than presenting FCFS as sufficient.

**Payment integration.** Converting a hold into a booking requires a successful payment before
the seat is marked sold — this is a two-phase flow (hold seats first, charge second) precisely
because you don't want to charge a user's card and then discover the seat is somehow gone, and
you don't want to mark seats permanently sold before payment is confirmed. See
[case-studies/payment-system.md](payment-system.md) for how the payment step itself handles idempotency and
failure — a failed or ambiguous payment response here must not leave a seat stuck in `held`
status indefinitely, which is exactly what the TTL sweep protects against.

## Bottlenecks & Scaling

The seat-selection/hold step is the single point of maximum contention in the entire system,
concentrated on a small number of hot rows for the most desirable seats — optimistic
concurrency keeps this from becoming a serialization bottleneck, but the waiting room is what
actually keeps the request volume hitting that step within a range the backend can sustain.
Everything downstream of an admitted, successfully-held request (payment processing, booking
confirmation) operates at a far lower and more manageable rate, since the waiting room has
already smoothed the spike by the time requests get there.
