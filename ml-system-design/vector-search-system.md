# Billion-Scale Vector Search System

## Requirements

### Functional
- Given a query vector, return the top-k approximate nearest neighbors from a corpus of up to a
  billion vectors, ranked by similarity (cosine or L2, depending on the embedding space).
- Support insertions and deletions into the index without requiring a full rebuild for every
  update.
- Support metadata filtering combined with vector similarity (e.g. "nearest neighbors, but only
  items in category X") — pure ANN search alone is often not the actual product requirement.
- Serve as the retrieval backbone for other systems in this repo (recommendation-system.md's
  candidate generation, rag-system.md's chunk retrieval) — i.e. this is infrastructure, not a
  standalone product surface.

### Non-functional
- Query latency p99 < 20ms at the target recall level — this is almost always on someone else's
  request-latency budget (a recommendation or RAG request), so it has to be a small, predictable
  slice of a larger SLA, not the dominant cost.
- Recall@k >= ~95% against exact (brute-force) nearest-neighbor search, at the chosen operating
  point — recall below this starts to measurably degrade whatever downstream system depends on
  retrieval quality, in ways that are easy to under-notice because ANN failures look like
  "slightly worse ranking," not a hard error.
- Memory footprint has to fit the economics of the deployment — at a billion vectors, storing
  full-precision embeddings without compression is often not viable at any reasonable node
  count, which is why quantization is a first-class design decision, not an optimization applied
  later.
- Index updates (insert/delete) must not require taking the index offline or materially
  degrading query latency/recall while they're in flight.

## Capacity Estimation

Corpus: 1B vectors, dimension 768 (a common mid-size embedding dimension).

Raw storage at fp32: 1B × 768 × 4 bytes ≈ **3.07 TB**. At fp16: ~1.54 TB. Neither comfortably
fits in memory on a single node at any reasonable cost, and query latency for ANN search depends
heavily on the working set living in memory rather than being paged from disk — this is the
core motivation for quantization.

Product quantization (PQ): split each 768-dim vector into m=96 sub-vectors of 8 dimensions each,
quantize each sub-vector to one of 256 centroids (8 bits/sub-vector) learned via k-means. Storage
per vector drops to 96 bytes (down from 3,072 bytes at fp32) — a **32x compression ratio**. Total
corpus storage: 1B × 96 bytes ≈ **96 GB** — now comfortably fits across a modest number of nodes'
memory, or even one large-memory node, versus the multi-terabyte uncompressed figure.

HNSW graph overhead (if used instead of/alongside IVF): each node maintains M=16-32 neighbor
links per layer; at M=32 and 4 bytes/link, ~32 × 4 = 128 bytes/vector of graph structure on top
of the vector data itself — for 1B vectors, another ~128 GB, roughly comparable in magnitude to
the PQ-compressed vector data itself, which is why pure HNSW at full precision on a billion
vectors is memory-expensive enough that hybrid approaches (IVF partitioning + PQ compression,
optionally with an HNSW graph only within each partition) are common at this scale rather than
one flat HNSW graph over the whole corpus.

Query volume: assume this index backs both a recommendation system (per recommendation-system.md,
~10,400 QPS peak) and a RAG system (per rag-system.md, ~175 QPS peak) sharing infrastructure —
call it ~11,000 QPS peak in aggregate, each query searching for k=50-500 candidates depending on
caller.

Sharding: at 96 GB compressed, a single well-provisioned node (e.g. 256 GB RAM) could
theoretically hold the whole index, but production deployments still shard — for query
parallelism (spreading 11,000 QPS across many nodes instead of one), for availability
(replication), and for update throughput (writes to one giant structure serialize more than
writes spread across shards). A reasonable split: 10-20 shards, each holding 50-100M vectors
(~5-10 GB compressed), replicated 2-3x for availability, giving a working footprint in the
few-hundred-GB-to-low-TB range across the cluster once replication is included.

## API Design

```
POST /v1/index/upsert         { id, vector, metadata }
     -> 202   (async — see index update deep dive; not immediately queryable in all
               index structures)

POST /v1/index/delete         { id }
     -> 202   (tombstoned, reclaimed on next compaction — see deep dive)

POST /v1/index/search
     { vector, k, filters?: {...}, ef_search? / nprobe? }
     -> 200 { results: [{id, score, metadata}], recall_estimate? }

GET  /v1/index/stats
     -> { vector_count, shard_count, avg_query_latency_ms, index_type }
```

`ef_search` (HNSW) / `nprobe` (IVF) are exposed as per-query tunables deliberately — the
recall/latency tradeoff (see deep dive) is often a caller-specific decision: a RAG system
retrieving top-8 for direct LLM grounding may want higher recall than a recommendation system's
first-stage candidate generation, which gets a second chance to fix mistakes at the ranking
stage downstream.

## Data Model / Feature Schema

```
vector_index (per shard, the core ANN structure — logically, not a relational table)
  id -> quantized_vector (PQ codes, ~96 bytes) + graph/inverted-list membership
  id -> metadata (small, filterable fields: category, timestamp, acl)

id_mapping (maps external IDs to internal index-local IDs, needed because most ANN
libraries operate on dense integer IDs internally)
  external_id -> (shard_id, internal_id)

tombstones (per shard, pending-deletion set)
  internal_id -> deleted_at   (filtered out of search results, physically removed
                                only at next compaction)

shard_metadata (control plane)
  shard_id -> { vector_count, node_assignment, replica_endpoints, index_build_version }
```

Metadata filtering (category, ACL, timestamp range) is stored alongside the vector rather than
requiring a join to an external store at query time — a filtered ANN search needs to apply the
filter during graph traversal or inverted-list scanning (see deep dive), which is only
efficient if the filter fields are colocated with the vector data itself, not fetched via a
separate round trip per candidate.

## High-Level Architecture

```
 index build / update path:
 new/updated vectors ──► Upsert service ──► quantize (apply the corpus's trained PQ
                                              codebook) ──► route to owning shard
                                              (by id hash, or by IVF cluster assignment)
                                                        │
                                                        ▼
                                    Shard: insert into in-memory index structure
                                    (HNSW graph edges added, or IVF inverted list
                                    appended) — incremental, not a full rebuild
                                                        │
                          periodic: ──► Compaction job (physically removes
                                         tombstoned vectors, rebalances/optimizes
                                         graph structure that's degraded from
                                         many incremental inserts)

 query path:
 client ──POST /search──► Query router (fans out to relevant shards — all shards,
                            if no filter narrows it; a subset, if sharding is
                            filter-aware, e.g. sharded by category)
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                 ▼
              Shard 1 search     Shard 2 search     Shard N search
              (top-k locally,    (top-k locally)    (top-k locally)
               applies filters
               + tombstone mask)
                    │                 │                 │
                    └─────────────────┴─────────────────┘
                                      │
                          Merge top-k across shards (min-heap merge of
                          per-shard top-k lists) ──► final top-k to client
```

## Deep Dives

**HNSW vs. IVF-PQ.** These represent two different points on the recall/latency/memory tradeoff
surface, and production systems often combine them rather than picking one purely.
HNSW (Hierarchical Navigable Small World graphs) builds a multi-layer graph where search
traverses from coarse (long-range links, top layer) to fine (short-range links, bottom layer),
giving excellent recall and low latency, but at higher memory cost (full-precision vectors plus
graph structure) and non-trivial build/insert cost. IVF (Inverted File index) partitions the
vector space into clusters (via k-means) and, at query time, only searches within the `nprobe`
clusters nearest the query vector rather than the whole corpus — much cheaper memory footprint,
especially combined with PQ compression of the vectors within each cluster, but recall depends
heavily on the query's true nearest neighbors actually falling within the probed clusters, which
degrades for queries near cluster boundaries. A common production pattern (used in this design)
is IVF-PQ for the coarse partitioning and compression, sharding data across nodes by IVF
cluster, and optionally layering a small HNSW graph within each cluster/shard for fast
in-cluster search — getting IVF's memory efficiency at the corpus level with HNSW's search
quality at the shard level.

**Recall vs. latency tuning.** Both major index families expose a runtime-tunable knob trading
recall for latency: HNSW's `ef_search` (how many candidates to keep in the traversal's dynamic
candidate list — higher explores more of the graph, higher recall, higher latency) and IVF's
`nprobe` (how many clusters to search — higher covers more of the space, higher recall, higher
latency). This is exposed to callers (see API design) rather than fixed platform-wide, because
the right operating point is a product decision, not a systems one — a first-stage
candidate-generation call that gets a second correctness pass downstream (ranking) can tolerate
lower recall for lower latency than a RAG system's single-shot retrieval, which has no downstream
correction step if it misses the right chunk.

**Sharding strategy.** Two broad approaches: shard by hash of ID (uniform load distribution,
simple, but every query fans out to every shard since there's no way to know in advance which
shard holds a query's nearest neighbors) or shard by IVF cluster / semantic region (a query can
sometimes be routed to only the shards covering its nearby clusters, reducing fan-out, but risks
uneven shard sizes if the underlying data distribution is skewed across semantic regions — some
topics/categories are simply far more populous than others). Billion-scale deployments
frequently use hash-based sharding for load-balancing simplicity and accept full fan-out per
query, since with a sharded merge (each shard returning its own top-k, merged centrally) the
per-shard latency is low enough that full fan-out is still fast in aggregate — it's a
parallel scatter-gather, not a serial cost.

**Deletion and compaction.** Physically removing a single vector from an HNSW graph is expensive
(it may be a load-bearing hop in many other nodes' navigation paths; removing it can require
graph repair to maintain connectivity), so deletes are implemented as tombstones — the vector is
marked deleted and filtered out of search results immediately (correctness is preserved), but
the graph/index structure isn't physically compacted until a periodic background job rebuilds
or repairs it. This trades a small amount of wasted memory and slightly degraded search
efficiency (traversal still visits tombstoned nodes en route to live ones) for avoiding expensive
synchronous graph surgery on every delete — acceptable as long as compaction runs often enough
that the tombstone fraction doesn't grow large enough to meaningfully hurt recall or latency.

**Filtered search.** A naive implementation of "ANN search, but only items matching filter X"
either does the ANN search first and then filters the top-k results (fast, but if filter X is
selective — matches a small fraction of the corpus — the true top-k-after-filter may not even be
present in the unfiltered top-k, tanking recall) or filters first and does exact search over the
filtered subset (correct, but throws away the ANN speed advantage if the filtered subset is
still large). Production systems typically push the filter into the traversal itself (skip
non-matching candidates during graph/cluster traversal rather than after it, continuing to
explore until k *matching* results are found, not just k results) — this preserves both recall
and most of the ANN speed advantage, at the cost of a less predictable, filter-selectivity
dependent query latency.

## Bottlenecks & Scaling

Memory is the primary constraint at billion-scale, which is why quantization is treated as
foundational rather than optional above — the choice of PQ sub-vector count and centroid bit
depth directly trades index size against recall, and is usually the single highest-leverage
tuning knob for the whole system's cost profile. Query fan-out (scatter-gather across all
shards) means p99 latency is bound by the slowest shard on any given query, not the average
shard — a single slow or overloaded shard degrades every query that touches it, making per-shard
latency consistency (even load distribution, enough replicas to absorb a hot shard) more
important than aggregate cluster capacity alone. Incremental inserts degrade HNSW graph quality
over time relative to a freshly-built graph from the same data (the graph structure was optimized
for the data present at build time, not for data trickling in afterward), which is why periodic
full or partial rebuilds/compaction are a standing operational job, not a one-time migration
step. Finally, because this system is infrastructure for other systems (recommendation
candidate generation, RAG retrieval), its capacity planning is fundamentally reactive to those
systems' growth — query volume here scales with the combined QPS of every downstream consumer,
which argues for provisioning with real headroom rather than tight-fitting current load.
