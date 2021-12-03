# URL Shortener

## Requirements

### Functional
- Given a long URL, generate a short unique alias (e.g. `sho.rt/aZ9kLm`).
- Visiting the short URL redirects to the original long URL.
- Users can optionally pick a custom alias.
- Links can optionally expire after a set date.
- Basic click analytics (count, maybe referrer/geo) per link.

### Non-functional
- Redirect latency should be near-instant — this sits on the critical path of every click.
- High availability on the redirect path; a shortener that's down breaks every link that uses
  it, everywhere it was shared.
- Uniqueness: two different long URLs never collide on the same short code.
- Short codes should not be sequentially guessable (avoid leaking creation order/volume).

## Capacity Estimation

Assume 100M new short URLs created per month, and a 100:1 read:write ratio (a link gets
clicked far more often than links get created).

Write QPS: 100M / (30 × 86,400 s) ≈ 100,000,000 / 2,592,000 ≈ 39 writes/sec average. Peak at
3x average ≈ 120 writes/sec.

Read (redirect) QPS: 100:1 ratio → ~3,900 reads/sec average, ~11,700 reads/sec peak.

Storage: each record is roughly 500 bytes (short code, long URL up to a few hundred bytes,
user ID, timestamps, expiry, click count). Over 5 years at 100M/month: 100M × 60 = 6B records.
6B × 500 bytes ≈ 3 TB raw, ~9 TB with 3x replication. Comfortably fits a sharded key-value
store; this is not a storage-bound problem, it's a lookup-latency problem.

Short code length: base62 (`[A-Za-z0-9]`) at 7 characters gives 62^7 ≈ 3.5 trillion possible
codes — enough headroom for decades at this volume without collision pressure.

## API Design

```
POST /api/v1/shorten
  { "long_url": "...", "custom_alias": "optional", "expires_at": "optional" }
  -> 201 { "short_url": "https://sho.rt/aZ9kLm" }

GET /{short_code}
  -> 301/302 redirect to long_url

DELETE /api/v1/links/{short_code}
  -> 204 (owner only)

GET /api/v1/links/{short_code}/stats
  -> { "clicks": 4213, "created_at": "...", "last_clicked": "..." }
```

## Data Model

```
url_mappings
  short_code   (PK)   varchar(10)
  long_url            varchar(2048)
  user_id             bigint, nullable
  created_at          timestamp
  expires_at          timestamp, nullable
  click_count         bigint  (eventually consistent counter)
```

Keyed entirely by `short_code`, looked up by exact match — no range queries, no joins needed
on the hot path. That access pattern is a textbook fit for a key-value store (DynamoDB,
Cassandra) rather than a relational database. Click analytics, if detailed (timestamp,
referrer, geo per click), go in a separate write-heavy store — a wide-column table keyed by
`(short_code, click_timestamp)` — rather than bloating the mapping row itself.

## High-Level Architecture

```
                         ┌────────────┐
 client ── write ──────► │ Load       │
                         │ Balancer   │
                         └─────┬──────┘
                               │
                     ┌─────────▼─────────┐
                     │   App servers      │──► Key Generation Service
                     │ (shorten endpoint) │    (hands out unused short
                     └─────────┬──────────┘     codes in batches)
                               │
                     ┌─────────▼─────────┐
                     │  key-value store   │  (Cassandra / DynamoDB,
                     │  short_code -> url │   sharded by hash(short_code))
                     └─────────────────────┘

 client ── GET /{code} ──► LB ──► App servers ──► Redis cache (hot codes)
                                          │              │ miss
                                          │              ▼
                                          └────►  key-value store
                                          then: 301/302 redirect response
```

Click-count increments are fired asynchronously onto a message queue rather than done
synchronously on the redirect path, so a slow analytics write never adds latency to a
redirect.

## Deep Dives

**Short code generation.** Three approaches, in order of how most systems evolve:

1. Hash the long URL (MD5/SHA-256), take the first 7 characters base62-encoded. Simple, but
   collisions are real at billions of scale and need a check-and-retry loop against the DB
   on every write, which adds latency and gets worse as the table fills up.
2. Random 7-character string, check for collision, retry if taken. Same collision-retry cost
   as above, doesn't scale cleanly under high write concurrency.
3. Key Generation Service (KGS): a separate service pre-generates unique 7-character codes
   offline and stores them in an "unused keys" pool. Each app server, on startup or when it
   runs low, checks out a batch (say 1,000 codes) from the pool and hands them out locally
   with zero collision risk and zero DB round-trip per shorten request. This is what
   production systems actually converge on — it turns a coordination problem (avoid
   collisions across all servers) into a batched allocation problem (avoid collisions within
   one pre-generated pool, solved once, offline).

**301 vs 302 redirect.** A 301 (permanent) lets the browser cache the redirect, so repeat
visits from the same client never hit the server again — great for load, bad for analytics,
since you stop seeing those clicks. A 302 (temporary) forces every click through the server,
giving accurate click counts at the cost of full read load on every visit. Most link
shorteners use 302 specifically to preserve click tracking, and lean on caching (CDN/Redis)
at the server side instead of the browser to keep latency down.

**Hot links.** A small fraction of links (a viral tweet, a marketing campaign) account for a
disproportionate share of redirect traffic — classic power-law distribution. A Redis cache in
front of the key-value store, sized to hold the working set of currently-hot codes, absorbs
this; without it, one viral link can hammer a single shard of the backing store even though
total system load looks fine on average.

## Bottlenecks & Scaling

The redirect path is overwhelmingly read-dominated (100:1) and latency-sensitive, so the
design optimizes almost entirely around that: cache in front of the DB, CDN-level caching for
extremely popular links if the product allows 301s for those, and a KV store sharded by
`hash(short_code)` (consistent hashing, see concepts/consistent-hashing.md) so adding capacity
doesn't require a full data reshuffle.

The write path is comparatively tiny (~40 QPS average) and is decoupled from any coordination
bottleneck by the KGS batching scheme, so it scales horizontally with no shared counter or
lock. The one thing that doesn't parallelize cleanly is the KGS's own pool — but pre-
generating codes offline in bulk (millions at a time) keeps that a rare, cheap batch job
rather than something on any request path.
