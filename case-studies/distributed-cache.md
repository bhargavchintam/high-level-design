# Distributed Cache

## Requirements

### Functional
- `get(key)`, `set(key, value, ttl)`, `delete(key)`.
- Support for common data structures beyond plain strings where useful (lists, sets, hashes) —
  this is effectively designing something like Redis or Memcached at a distributed scale.

### Non-functional
- Sub-millisecond to low-single-digit-millisecond latency at p99 — the entire reason a cache
  exists is to be dramatically faster than the source of truth.
- High throughput per node (100K+ ops/sec on commodity hardware).
- Graceful degradation: a cache node failing should degrade hit rate, not correctness — the
  system behind it (the real database) must still be the source of truth.
- Even key distribution across nodes, and controlled rebalancing when nodes are added/removed.

## Capacity Estimation

Assume this cache sits in front of a system doing 200K reads/sec at peak, with a target 90%
cache hit rate (so 180K/sec served from cache, 20K/sec falling through to the database).
Average cached value size: 2 KB (a serialized API response or a hydrated object).

Working set size: if the cache holds the "hot" 5% of a 200M-row dataset to sustain a 90% hit
rate (power-law access pattern, most traffic to a small key set), that's 10M keys × 2 KB ≈
20 GB. Comfortably fits across a handful of cache nodes with room to spare — this is a
throughput and node-count problem more than a storage problem, unlike the backing database.

Per-node throughput: a single well-provisioned Redis node handles roughly 100K-150K simple
ops/sec (single-threaded event loop, so this is largely CPU-per-op bound, not I/O bound, since
data is in memory). To serve 180K/sec with headroom for node loss, run at least 3-4 nodes even
though 20 GB total data would technically fit on one — throughput, not memory, is the actual
driver of node count.

## API Design

Client-facing, typically via a client library rather than raw HTTP for latency reasons:

```
GET key                              -> value or nil
SET key value [EX seconds]           -> OK
DEL key                              -> count removed
MGET key1 key2 ...                   -> batched get, reduces round trips
INCR key                             -> atomic counter increment
```

## Data Model

The cache itself is schemaless — keys are strings, values are whatever the client serializes
(commonly JSON or a compact binary format like MessagePack/Protobuf to save network and
memory). What matters is the key design, since that's what determines hit rate and
invalidation granularity: `user:1234:profile`, `feed:1234:page:0`, structured so a single
entity's cache entries can be targeted for invalidation without touching unrelated keys.

## High-Level Architecture

```
             ┌──────────────┐
 app server ─┤ cache client │── consistent hash of key ──┐
             │  (routing)   │                             │
             └──────────────┘                             │
                                    ┌──────┬──────┬────────┼────────┐
                                    ▼      ▼      ▼        ▼        ▼
                                 node A  node B  node C  node D  node E
                                (each holds a slice of the keyspace,
                                 optionally with 1 replica each for HA)

 on miss: app server falls through to the database, then writes the
 result back into the cache (cache-aside pattern) before returning it.
```

Routing can live client-side (each app instance computes which node owns a key, as classic
Memcached clients do) or be handled by the cache cluster itself (Redis Cluster resolves
`MOVED`/`ASK` redirects internally). Client-side routing has one less network hop; cluster-side
routing is simpler for clients and handles rebalancing more transparently.

## Deep Dives

**Cache-aside vs. write-through.** Cache-aside (app checks cache, falls through to DB on miss,
populates cache after) is the default because it's simple and the cache only ever holds
what's actually been requested — no wasted memory on cold data. Write-through (write to cache
and DB together) trades write latency for guaranteed freshness; worth it for data where a
stale cache read after a write would visibly break the product (e.g. an inventory count right
after checkout). See [concepts/caching.md](../concepts/caching.md) for the full comparison.

**Hot key mitigation.** Consistent hashing sends one key to one node — if that key is
extremely popular (a trending post, a viral product), that single node's throughput becomes
the ceiling regardless of cluster size. Detect this (sampling access frequency) and
mitigate by replicating the specific hot key across multiple nodes, with the client
round-robining reads across the replicas — this is a targeted fix, not something the general
partitioning scheme handles for free.

**Thundering herd on expiry.** A popular key expiring and a burst of concurrent requests
missing it simultaneously can send a spike of duplicate load straight to the database. Mitigate
with request coalescing (the first miss triggers a DB fetch and populates the cache; concurrent
requests for the same key wait on that in-flight fetch instead of triggering their own) or by
adding random jitter to TTLs so keys don't expire in lockstep.

**Eviction under memory pressure.** Once the cache is full, something has to be evicted to make
room for a new key. LRU is the default and works well for most access patterns; Redis
implements an approximated LRU (samples a small set of keys and evicts the oldest of the
sample) rather than a strict global LRU list, because maintaining an exact recency order on
every access would itself be a throughput bottleneck. See [concepts/caching.md](../concepts/caching.md) for LRU vs. LFU
tradeoffs.

## Bottlenecks & Scaling

Single-node throughput is usually the first ceiling (Redis is largely single-threaded per
shard), which is why horizontal sharding — not bigger boxes — is the standard scaling path;
Redis Cluster and client-side consistent hashing both exist specifically to add nodes rather
than grow one node indefinitely. Memory is the second constraint — once working-set size
exceeds available RAM across the cluster, hit rate degrades and eviction churn increases,
which is a signal to either add nodes or shrink TTLs/value sizes rather than a signal to add
disk-backed cache tiers (which defeats most of the latency benefit that justified having a
cache in the first place).
