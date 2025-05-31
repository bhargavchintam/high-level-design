# Caching

Caching trades staleness for speed: keep a copy of expensive-to-compute or expensive-to-fetch
data somewhere cheaper and faster to read, and serve from there instead of hitting the source
of truth every time. The entire practice of caching is managing the tradeoff between hit rate,
staleness, and complexity.

## Where caches live

- Client-side: browser cache, mobile app cache. Zero network cost on hit, but you can't
  invalidate it remotely — you're stuck with whatever TTL you set.
- CDN: caches static assets and sometimes API responses at edge locations close to users.
  See cdn.md.
- Application-level (in-process): a hash map inside the app process. Fastest possible cache,
  but not shared across instances, and it disappears on every deploy.
- Distributed cache: Redis or Memcached, shared across all app instances. Adds a network hop
  but the hop is sub-millisecond on the same rack, and everyone sees the same data.
- Database-level: query cache, buffer pool, materialized views. Mostly managed by the DB
  itself.

Real systems stack several of these — browser cache, then CDN, then a Redis layer, then the
database's own buffer pool — each layer catching what the one before it missed.

## Eviction policies

Caches are bounded in size, so something has to decide what gets thrown out.

- LRU (least recently used): evict whatever hasn't been touched in the longest time. The
  default choice, works well for most access patterns.
- LFU (least frequently used): evict by access count instead of recency. Better when some
  keys are consistently hot regardless of when they were last touched, but more bookkeeping.
- FIFO / TTL-based: simplest, evict by age. Fine when data has a natural expiry (a session
  token, a rate-limit counter).

Redis defaults to a form of approximated LRU (sampling, not a strict global LRU list) because
a strict LRU list is expensive to maintain at scale.

## Write strategies

- Cache-aside (lazy loading): app reads cache first, on a miss reads the DB and populates the
  cache. Most common pattern. Downside: first request after eviction always pays full latency,
  and you can get thundering-herd misses on a hot key.
- Write-through: writes go to the cache and the DB together, synchronously. Keeps cache
  consistent but adds write latency.
- Write-behind (write-back): writes go to the cache immediately and get flushed to the DB
  asynchronously. Fast writes, but you can lose data if the cache dies before the flush.
- Read-through: the cache itself owns the logic to fetch from the DB on a miss, so the app
  only ever talks to the cache. Cleaner API, common in managed caching layers.

## Invalidation

The famous hard part. Options, roughly in order of freshness vs. simplicity:

- TTL expiry: set a time and let it go stale automatically. Simple, bounded staleness, no
  coordination needed. Most systems lean on this.
- Explicit invalidation: on write, delete or update the cache key. Requires the writer to
  know every place that key is cached — easy in a monolith, hard once multiple services
  write the same entity.
- Write-through with versioning: attach a version or timestamp, and readers can detect stale
  entries.

## Thundering herd and hot keys

If a popular key expires and 10,000 requests hit it simultaneously, they all miss the cache
at once and slam the database. Mitigations: lock/single-flight so only one request
recomputes and the rest wait on it, add jitter to TTLs so keys don't all expire together, or
serve stale data while a background refresh runs. A single overloaded hot key (a celebrity's
profile, a viral post) can also exceed one cache node's throughput regardless of eviction
policy — the fix there is usually replicating that specific key across multiple nodes, not
just adding more nodes to the ring.
