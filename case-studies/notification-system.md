# Notification System

## Requirements

### Functional
- Send notifications across multiple channels: push (mobile), email, SMS, in-app.
- Support both triggered notifications (a user liked your post, right now) and scheduled/
  batch ones (a daily digest email).
- Respect user preferences (opted out of email, push only, quiet hours).
- Avoid duplicate/spammy delivery of the same logical event.

### Non-functional
- High throughput at bursty peaks (a breaking-news push to 50M devices within seconds).
- At-least-once delivery with dedup — better to occasionally double-send than to silently
  drop a notification.
- Each channel (APNs, FCM, SMTP, SMS gateways) has different rate limits and failure modes,
  and the system must isolate one channel's outage from affecting the others.

## Capacity Estimation

Assume 500M DAU, average 5 notification-worthy events per user per day across all channels →
2.5B notifications/day → 2.5B / 86,400 ≈ 29,000/sec average.

Peak burst case: a single high-fanout event (breaking news, a major sports score) pushed to
50M subscribed devices within a target 60-second delivery window → ~833,000 pushes/sec for
that one burst — an order of magnitude above steady-state average, and the number that
actually dictates provisioning, not the daily average. This is the defining characteristic of
notification systems: design for burst, because average load tells you almost nothing about
peak.

Storage: each notification record (recipient, channel, payload, status, timestamps) is a few
hundred bytes. 2.5B/day × 300 bytes ≈ 750 GB/day if every notification and its delivery status
is retained — usually kept for a bounded window (weeks, for debugging/support) rather than
indefinitely, since notification history has little long-term value compared to its storage
cost.

## API Design

```
POST /v1/notifications
  { recipient_id, event_type, channel_preference?, payload, priority }
  -> 202 Accepted { notification_id }

# internal, called by other services rather than end users
POST /v1/notifications/batch    (for digest/bulk sends)

GET  /v1/users/{id}/notification-preferences
PUT  /v1/users/{id}/notification-preferences
```

Always `202 Accepted`, never a synchronous delivery confirmation — actual delivery to APNs/
FCM/SMTP happens asynchronously and can take anywhere from milliseconds to seconds depending
on the channel and downstream provider.

## Data Model

```
notifications
  notification_id (PK), recipient_id, event_type, channel, payload
  status: queued | sent | delivered | failed
  created_at, sent_at

user_preferences
  user_id -> { push: bool, email: bool, sms: bool, quiet_hours: {...} }

device_tokens
  user_id -> [ { platform: ios|android, token, updated_at } ]

dedup_log (short-TTL)
  idempotency_key -> already_sent (bool)   # prevents double-delivery on retry
```

## High-Level Architecture

```
 producing service (e.g. the "like" service) ──► Notification API
                                                       │
                                              enrich: look up user_preferences,
                                              device_tokens, apply quiet-hours /
                                              opt-out filtering
                                                       │
                                              write to dedup_log (idempotency
                                              check — skip if already sent
                                              for this event)
                                                       │
                                              ┌────────┴────────┐
                                              ▼                 ▼
                                     Message Queue,      Message Queue,
                                     partitioned per      partitioned per
                                     channel (push)       channel (email)
                                              │                 │
                                     Push workers         Email workers
                                     ──► APNs / FCM        ──► SMTP / SES
                                              │                 │
                                     retry with backoff   retry with backoff
                                     on transient failure  on transient failure
```

Each channel gets its own queue and worker pool so that, for example, an SMS gateway outage
backs up only the SMS queue — push and email keep flowing unaffected. This channel isolation
is the single most important structural decision in the architecture.

## Deep Dives

**Fanout for high-reach events.** A single "breaking news" event targeting 50M users can't be
50M synchronous API calls from the producing service — the event is published once, and a
fanout worker expands it into per-user notification jobs pushed onto the channel queues, the
same push-side fanout problem as case-studies/twitter-news-feed.md, just triggering delivery
jobs instead of populating a read cache. This fanout step itself needs to be horizontally
parallelized (partition the recipient list across many fanout workers) to hit a 60-second
delivery window at 50M scale.

**Channel-specific rate limits.** APNs, FCM, and SMS gateways all impose their own rate limits
and have different failure semantics (a bad device token is a permanent failure, worth
removing from `device_tokens`; a 5xx from the provider is transient, worth retrying with
backoff). Each channel's worker pool needs its own rate limiter tuned to that provider's actual
limits (see concepts/rate-limiting.md) — treating all three channels identically either
under-utilizes headroom on the more permissive ones or gets the stricter ones throttled/banned.

**Idempotency and dedup.** At-least-once delivery through a queue means a worker crash after
sending but before acknowledging the queue message can cause the same notification to be
processed twice. A dedup log keyed by an idempotency key (a hash of recipient + event +
event-specific dedup window) lets the send path check "have I already delivered this" before
calling out to APNs/SMTP, turning at-least-once queue semantics into effectively-once user-
visible delivery. This only needs a short TTL (long enough to cover realistic retry windows,
not permanent storage).

**Priority and quiet hours.** Not all notifications are equal — a security alert ("new login
from unrecognized device") should bypass quiet hours and batching; a "someone liked your
photo" notification should respect quiet hours and can be batched into a digest rather than
delivered instantly. This means the enrichment step needs a priority classification per
event_type, and lower-priority notifications route to a batching/digest path (accumulate over
a window, e.g. hourly, and send one combined notification) rather than the immediate-delivery
path, which both improves user experience (fewer interruptions) and meaningfully reduces
total notification volume.

**Delivery tracking and retries.** Transient failures (provider 5xx, timeout) get retried with
exponential backoff up to a limit; after that, the notification is marked failed rather than
retried indefinitely, and — same pattern as concepts/message-queues.md — persistently failing
messages go to a dead-letter queue for investigation rather than blocking the channel's main
queue.

## Bottlenecks & Scaling

Burst fanout capacity is the defining scaling requirement, not steady-state throughput — the
fanout workers and per-channel queues need enough elastic headroom (autoscaling on queue
depth) to absorb an 800K+/sec burst without that backlog taking minutes to clear, since a
delayed breaking-news push loses most of its value. Per-channel isolation via separate queues
is what prevents a single downstream provider's outage or rate-limit throttling from cascading
into a full notification-system outage.
