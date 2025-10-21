# Rate Limiting

Rate limiting caps how many requests a client (a user, an API key, an IP) can make in a given
window, to protect a service from being overwhelmed — whether by a bug, a runaway retry loop,
or someone deliberately hammering an endpoint. It's also a product lever: free tier gets 100
requests/day, paid tier gets 100,000.

## Algorithms

- Fixed window: count requests in a fixed time bucket (e.g. per calendar minute), reset the
  counter at the boundary. Cheap — one counter per client per window — but bursty at
  boundaries: a client can send the full quota at 0:59 and the full quota again at 1:00,
  getting 2x the intended rate in a two-second span.
- Sliding window log: store a timestamp per request, count how many fall within the last N
  seconds on each check. Accurate, no boundary problem, but memory cost scales with request
  volume — storing every timestamp doesn't scale for a high-traffic API.
- Sliding window counter: approximate the sliding window using two fixed windows and a
  weighted average of the previous window's count based on how far into the current window
  you are. Much cheaper than the log approach, close enough in practice, and what most
  production systems actually use (this is Cloudflare and Stripe's approach, roughly).
- Token bucket: a bucket holds up to B tokens, refills at rate R tokens/sec, each request
  consumes one token, requests are rejected when the bucket is empty. Naturally allows bursts
  up to the bucket size while enforcing a steady-state average rate — this is the standard
  choice when you want to allow short bursts (a client that's idle for a while and then sends
  a flurry) without allowing sustained abuse.
- Leaky bucket: requests enter a queue and are processed at a fixed rate; if the queue is
  full, new requests are dropped. Smooths bursts into a constant output rate rather than
  allowing them through — used more for traffic shaping than for a reject/accept API gate.

## Where to enforce it

- Client-side: cooperative only, doesn't stop a malicious or buggy client, but reduces
  needless server load from well-behaved ones.
- API gateway / edge: the standard place — reject over-limit requests before they consume any
  backend capacity. See api-gateway.md.
- Per-service: finer-grained limits closer to the resource being protected (e.g. a
  particularly expensive search endpoint gets a tighter limit than the rest of the API).

## Distributed rate limiting

A single server can keep counters in memory, but once you have N load-balanced instances, a
per-instance counter lets a client get N times the intended limit by spreading requests
across instances. The fix is a shared counter store — Redis is the standard choice, using
`INCR` with a TTL for fixed-window, or a Lua script for token bucket to make the
check-and-decrement atomic. This adds a network round trip to every request's rate-limit
check, so the Redis call itself needs to be fast and highly available; a Redis outage
shouldn't take down the whole API, so most systems fail open (allow the request) rather than
fail closed if the limiter itself is unreachable, accepting some risk of overload rather than
turning a limiter outage into a full outage.

## Response behavior

Return `429 Too Many Requests` with a `Retry-After` header so well-behaved clients know when
to try again, and expose `X-RateLimit-Limit` / `X-RateLimit-Remaining` headers so clients can
self-throttle before hitting the wall. See case-studies/rate-limiter.md for a full system
design around this, including how the algorithm choice changes under multi-region deployment.
