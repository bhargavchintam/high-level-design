# Rate Limiter

## Requirements

### Functional
- Limit the number of requests a client (API key, user ID, or IP) can make within a time
  window, per endpoint or globally.
- Reject requests over the limit with a clear signal (HTTP 429) and a retry hint.
- Support different limits for different clients/tiers (free vs. paid).
- Limits should be configurable without a redeploy.

### Non-functional
- The limiter must add negligible latency to every request it checks (it sits in front of
  everything).
- Must work correctly across many load-balanced instances of the service being protected —
  a per-instance-only limiter is trivially bypassed by spreading requests across instances.
- Should fail open (allow traffic) rather than fail closed if the limiter's own storage is
  unavailable — an outage in the limiter shouldn't become an outage for the whole API.

## Capacity Estimation

Assume this sits in front of an API doing 500K requests/sec at peak globally (large-scale
platform, comparable to a major API gateway's traffic). Every single request needs a
rate-limit check, so the limiter's own read/write throughput has to match: ~500K ops/sec
against whatever store holds the counters.

If counters live in Redis and each check is a single round trip (using a Lua script for
atomicity, see below), a single Redis node can handle roughly 100K-200K ops/sec for simple
operations, so this needs a sharded Redis cluster of at least 5-10 nodes just for headroom,
sharded by client ID so each client's counter always lands on the same node (avoids
cross-node coordination per check).

Storage per counter: a client ID + counter value + window timestamp, well under 100 bytes.
Even at 100M distinct active clients, that's under 10 GB — trivial; this is a throughput and
latency problem, not a storage one.

## API Design

The rate limiter is usually not a client-facing API — it's a library or sidecar the gateway
calls internally. The interface it exposes to the gateway:

```
check_and_increment(client_id, endpoint) -> { allowed: bool, remaining: int, reset_at: ts }
```

Client-facing behavior on the actual API:

```
200 OK
  X-RateLimit-Limit: 1000
  X-RateLimit-Remaining: 214
  X-RateLimit-Reset: 1720971600

429 Too Many Requests
  Retry-After: 37
```

## Data Model

For token bucket (the algorithm covered below), per client:

```
rate_limit:{client_id}:{endpoint}
  tokens          float   (current tokens in bucket)
  last_refill_ts  float   (last time tokens were topped up)
```

Stored in Redis as a hash or two separate keys, with a TTL slightly longer than the bucket's
refill period so idle clients' counters expire naturally instead of accumulating forever.

## High-Level Architecture

```
 client ──► API Gateway ──► Rate Limiter check ──► allowed? ──► backend service
                    │              │
                    │        ┌─────▼─────┐
                    │        │  Redis     │  sharded by hash(client_id)
                    │        │  cluster   │  (counters / token buckets)
                    │        └────────────┘
                    │
                    └──► 429 response (rejected, no backend call made)
```

The check happens at the edge (gateway or a sidecar next to each service) so a rejected
request never consumes backend capacity — that's the entire point of putting it there instead
of inside the business logic.

## Deep Dives

**Algorithm choice: token bucket.** Each client gets a bucket with capacity B, refilling at
rate R tokens/sec. A request consumes 1 token; if the bucket's empty, reject. This allows
short bursts up to B (a client idle for a while can send a burst) while enforcing a long-run
average rate of R — closer to how real client behavior looks than a strict fixed window. See
concepts/rate-limiting.md for how this compares to fixed window, sliding window, and leaky
bucket.

**Making the check atomic.** "Read current tokens, compute refill, check if >= 1, decrement,
write back" is four operations — if done as separate Redis calls, two concurrent requests from
the same client can race and both pass when only one should. The fix is a single Lua script
executed atomically inside Redis (`EVAL`), so the whole read-compute-write happens as one
op with no race window. This is the standard pattern for any check-and-decrement rate limiter
built on Redis.

**Distributed correctness vs. latency tradeoff.** A single shared Redis cluster gives exact
global counts but adds a network hop to every request and creates a central dependency. An
alternative is local, per-instance approximate limiting (each app instance tracks its own
counter and only periodically syncs with a shared store), which is faster and has no single
point of failure, but allows a client to briefly exceed the true limit by roughly (instance
count × local burst allowance) before the sync catches up. Most production systems accept
this approximation deliberately — being off by a small margin on a rate limit is a much
smaller problem than adding a synchronous cross-datacenter round trip to every request.

**Multi-region.** If the API is deployed in multiple regions and a client can hit any of them,
a single global Redis cluster becomes a cross-region dependency on every request — unacceptable
latency-wise. Real systems either accept per-region limits (each region enforces R/N of the
global limit, where N is the number of regions) or replicate counters asynchronously across
regions and accept that the true global count is only eventually accurate. Exact global
rate limiting across regions with low added latency is not really achievable — pick the
approximation that fits the product.

## Bottlenecks & Scaling

The Redis layer is the central bottleneck: shard it by client ID (consistent hashing, see
concepts/consistent-hashing.md) so it scales horizontally, and run it as a proper cluster with
replication so a single node failure doesn't wipe out rate-limit state for the clients it
owned (worst case on node loss: those clients get a fresh bucket, which is a minor,
self-correcting problem, not a correctness disaster).

The fail-open requirement matters operationally: if the Redis cluster is unreachable, the
limiter should let requests through rather than reject everything, and alert loudly. A rate
limiter that fails closed turns its own infrastructure blip into a full outage of the system
it was supposed to protect — a strictly worse outcome than temporarily having no rate
limiting at all.
