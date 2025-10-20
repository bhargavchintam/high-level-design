# Search Autocomplete (Typeahead)

## Requirements

### Functional
- As a user types, suggest completions in real time (after each keystroke).
- Rank suggestions by relevance — typically a mix of overall popularity and the individual
  user's history/context.
- Suggestions update over time as query popularity shifts (trending topics should surface
  quickly).

### Non-functional
- Latency budget is extremely tight — this fires on every keystroke, so it needs to feel
  instant (sub-100ms end-to-end, ideally sub-50ms server-side, or the UI feels laggy).
- High read QPS: far higher than the underlying search volume, since one search often
  generates 5-10 autocomplete requests (one per keystroke) before the user commits to a query.
- Freshness vs. stability tradeoff: rankings shouldn't be recomputed so eagerly that
  suggestions flicker, but stale-for-days rankings miss trending queries.

## Capacity Estimation

Assume 1B searches/day, and each search is preceded by an average of 6 autocomplete requests
(one per keystroke before the user picks a suggestion or hits enter). That's 6B autocomplete
requests/day → 6B / 86,400 ≈ 69,000/sec average, ~210,000/sec at peak — this dwarfs the actual
search QPS by 6x, which is the main reason autocomplete is designed as a mostly-separate,
heavily-cached system rather than just hitting the main search index with a prefix query.

Data size: assume 100M distinct historical queries tracked for ranking purposes, average query
string 20 bytes, plus a frequency counter and metadata — call it 100 bytes/entry → 100M × 100
bytes = 10 GB. Small enough to fit substantially in memory across a modest cluster, which
matters a lot given the latency budget — this should not be a disk-bound system on the read
path.

## API Design

```
GET /v1/autocomplete?prefix=piz&limit=10
  -> [ "pizza near me", "pizza hut", "pizza delivery", ... ]  ranked by score
```

Deliberately a single, simple, cacheable GET — no POST body, no complex query language, because
this endpoint needs to be fast above almost everything else, and simple GETs are the easiest
thing to cache aggressively at every layer (browser, CDN, application cache).

## Data Model

The core structure is a trie (prefix tree), not a relational table:

```
trie node:
  children: { 'a': node, 'b': node, ... }
  is_end_of_word: bool
  top_k_completions: [ (query_string, score), ... ]   (precomputed, cached at the node)

query_frequency (separate, for building/updating the trie)
  query_string -> { count, last_updated }
```

Storing the top-K completions directly at each trie node (rather than recomputing them by
walking the subtree on every request) is the key design choice — it trades some memory and
update complexity for O(1)-ish lookup at query time: a request for prefix "piz" walks to that
node and returns its precomputed top-K list directly, no subtree traversal on the read path.

## High-Level Architecture

```
 client (each keystroke) ──► CDN / edge cache (very short TTL, e.g. a few seconds,
                               or keyed such that identical prefixes from many users
                               share one cached response)
                                        │ miss
                                        ▼
                              Autocomplete Service
                                        │
                              in-memory Trie (sharded by prefix range
                              if too large for one node, e.g. 'a-m' /
                              'n-z', or fully replicated if it fits
                              in memory on every node — 10GB fits easily)
                                        │
                              return precomputed top-K for this prefix

 offline / async pipeline (not on the read path):
   query logs ──► aggregation job (batch, e.g. hourly) ──► updated frequency counts
                                        │
                              rebuild / incrementally update trie's
                              precomputed top-K lists
                                        │
                              push updated trie snapshot to serving nodes
```

## Deep Dives

**Precomputing top-K per node.** The naive approach — store all completions under a prefix and
sort at query time — is too slow for a sub-50ms budget once a prefix has thousands of possible
completions. Precomputing and caching the top-K (K=10, say) directly on each trie node at
build time means a read is just a tree walk to the node plus returning an already-sorted list
— no sorting or scoring happens on the request path at all. The cost is shifted entirely to
the (much less frequent) update/rebuild process.

**Keeping rankings fresh without rebuilding constantly.** Rebuilding the entire trie from
scratch on every query-frequency change doesn't scale — updates happen far less often than
reads, so the system is built around a batch/incremental update cycle instead: query logs are
aggregated (commonly hourly, sometimes with a faster path for detecting genuinely trending
queries — a query whose frequency spikes sharply in a short window gets flagged and can be
injected ahead of the normal update cycle rather than waiting for the next scheduled rebuild).
This is a deliberate consistency tradeoff: autocomplete rankings are allowed to be up to an
hour or so stale in the common case, in exchange for the read path never doing real-time
aggregation work.

**Sharding the trie.** At 10 GB the whole trie likely fits in memory on a single reasonably-
sized node, so the simplest approach is full replication — every serving node holds the
complete trie, and a plain load balancer routes to any of them, since there's no need to
partition by prefix at this data size. If the dataset were larger (multi-language, much bigger
query vocabulary, per-user personalization baked directly into the trie rather than layered on
top), sharding by prefix range (all 'a'-prefixed queries on one shard, 'b' on another) is the
natural split, though it complicates single-character-prefix queries which would need to hit
many shards for the earliest keystrokes.

**Personalization layering.** A fully generic, global top-K per prefix is a reasonable
baseline, but real products blend in per-user signal (your own search history, your location)
on top of the global ranking rather than baking personalization into the shared trie itself —
personalizing the trie per-user would multiply its size by user count, which doesn't scale.
Instead, the global top-K comes back from the trie lookup, and a thin personalization layer
re-ranks or injects a small number of user-specific suggestions (recent searches by this user
matching the prefix) at request time, which is cheap because it only touches one user's small
history, not the global structure.

**Query normalization.** Case-folding, trimming, and handling near-duplicate queries ("pizza
near me" vs "Pizza near me") needs to happen before frequency counting and before trie lookup,
or the same logical query fragments its counts across variants and never accumulates enough
weight to rank well — this is a data-quality concern that matters as much as any of the
architectural choices above.

## Bottlenecks & Scaling

The read path is designed to be almost entirely CPU-cache-friendly in-memory lookups with no
disk I/O and no real-time computation, which is what makes the tight latency budget achievable
at very high QPS. The actual scaling lever, if read QPS outgrows what replicated in-memory
tries on a reasonable node count can serve, is straightforward horizontal replication (this
workload has no write contention on the read path to coordinate around) rather than anything
more elaborate — the interesting complexity in this system is entirely in keeping the
precomputed structure both fast to read and reasonably fresh, not in scaling raw request
throughput.
