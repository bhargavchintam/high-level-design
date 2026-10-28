# Real-Time Embedding Service

## Requirements

### Functional
- Generate dense vector embeddings from raw input (text, image, or query) on demand — both
  real-time query-time embedding (a user's search query, arriving one at a time on a latency
  budget) and large-batch corpus embedding (embedding an entire catalog or document set offline).
- Serve as the embedding-generation front end for downstream retrieval: this service produces
  vectors, it does not perform nearest-neighbor search itself — see [vector-search-system.md](vector-search-system.md) for
  the ANN index those vectors are written into, and [rag-system.md](rag-system.md) for chunk-embedding retrieval
  built on top of both.
- Cache embeddings for repeated or near-duplicate inputs to avoid redundant model inference —
  embedding is comparatively expensive per call relative to a cache lookup, and real query/content
  distributions are heavily skewed toward repeats.
- Support multiple concurrent embedding model versions with a defined migration path when the
  model is upgraded, since a new model's vectors are not comparable to the old model's vectors —
  this is an embedding-space version boundary the system must enforce structurally, not a detail
  callers can be trusted to track themselves.

### Non-functional
- Real-time query-path embedding latency p99 < 20-30ms per request — this sits in front of a
  retrieval call's own latency budget ([vector-search-system.md](vector-search-system.md)'s 20ms search target), so embedding
  generation and retrieval together have to fit inside a caller's overall SLA, not each get their
  own independent budget.
- Batch corpus embedding throughput must be able to re-embed the full corpus within an
  operationally acceptable window (hours, not days) whenever the embedding model version is
  upgraded — this bounds how often the model can practically be changed at all.
- Cache hit rate is a primary cost and latency lever: real-world input distributions recur heavily,
  so cache effectiveness materially changes both required GPU fleet size and observed p99 latency.
- Embedding-space consistency must be structurally enforced: a caller must never compare a
  v1 vector against a v2 vector, even though both are numerically valid same-dimension float
  arrays — this has to be a property of the cache/index keys, not a rule callers are trusted to
  follow.

## Capacity Estimation

**Real-time query load.** This service backs [rag-system.md](rag-system.md)'s chunk/query retrieval (~175 QPS peak)
plus recommendation and ad-hoc search query embedding (~5,000 QPS peak) plus assorted internal
callers → **~8,000 QPS peak** aggregate real-time embedding requests.

**Cache effect on effective load.** Assuming a ~40% cache hit rate on real-time traffic (skewed
query distribution — a small set of queries/content recurs constantly): effective QPS actually
reaching the embedding model = 8,000 × (1 - 0.4) = **4,800 QPS**. This is the number that sizes the
real-time GPU fleet, not the raw 8,000 — the cache is doing the equivalent work of a meaningful
fraction of the fleet.

**Real-time fleet size.** Real-time batching windows are short (bounded by the latency budget, so
the server can't wait long to accumulate a batch), giving a lower per-GPU throughput than the
offline path — call it ~600 embeddings/sec/GPU. At 4,800 QPS: 4,800 / 600 = 8 GPUs minimum,
provisioned with headroom → **~12-16 GPUs**.

**Cache footprint.** ~50M distinct cached `(content_hash, model_version) -> embedding` entries, at
768-dim fp16 (1.5KB) plus small metadata ≈ 50M × ~1.6KB ≈ **~80GB** — fits comfortably in a
sharded in-memory KV cache tier, the same shape as [feature-store.md](feature-store.md)'s online store sizing.

**Batch corpus re-embedding.** Re-embedding the full 1B-item corpus (consistent with
[vector-search-system.md](vector-search-system.md)'s index size) on a model-version upgrade: at a batch throughput of ~1,000
embeddings/sec/GPU (no latency constraint, larger batches than the real-time path) across a 50-GPU
migration pool: 50 × 1,000 = 50,000/sec → 1,000,000,000 / 50,000 = 20,000 sec ≈ **~5.6 hours** for
a full-corpus re-embed — the number that bounds how disruptive an embedding-model upgrade is, and
why migrations are planned operations, not casual model swaps.

**Encoder vs. decoder cost, for scale.** Unlike [llm-inference-serving.md](llm-inference-serving.md)'s decode-bound generation
(sequential, one token at a time), an embedding model does one forward pass per input with no
autoregressive loop — which is exactly why its per-GPU throughput figures above are an order of
magnitude higher than that document's ~4,000 output-tokens/sec/replica figure for a comparable
GPU count: there's no KV cache, no per-token step, just a single batched pass per input.

## API Design

```
POST /v1/embed
     { inputs: [{type: text | image, content | uri}, ...], model_version?: "latest" | pinned }
     -> 200 { embeddings: [[float16 x dim], ...], model_version, cache_hits: int }

POST /v1/embed/batch
     { input_dataset_uri, model_version, output_uri }
     -> 202 { job_id }   (async, offline/corpus path — mirrors feature-store.md's
                          offline endpoint: not something that returns inline)

GET  /v1/embed/models
     -> [{ model_version, dim, status: active | migrating | deprecated }]
```

## Data Model

```
embedding_cache (keyed on content_hash + model_version — the versioned key is the
  enforcement mechanism that prevents cross-version comparisons)
  content_hash, model_version -> embedding[dim], cached_at, ttl

model_registry (embedding-model-specific subset of the concept in
  ml-experiment-tracking-platform.md's registry)
  model_version -> dim, status, deployed_at

migration_job (tracks a corpus re-embedding run during a version upgrade)
  job_id, from_version, to_version, corpus_uri, progress_pct, status

embedding_request_log (sampled)
  request_id, model_version, cache_hit: bool, latency_ms, ts
```

## High-Level Architecture

```
 real-time path:
 client ──POST /embed──► Cache lookup (content_hash + model_version)
                                    │
                    hit ───────────┼───────────► return cached embedding
                                    │miss
                                    ▼
                        Embedding model server (batches concurrent
                        misses arriving within a short window into one
                        forward pass)
                                    │
                                    ▼
                        Write-through cache ──► embedding returned

 batch / offline path:
 corpus source ──► Batch embedding job (distributed workers, each running
                    the encoder over a shard of the corpus, no latency
                    constraint, large batches)
                                    │
                                    ▼
                    Output written to object storage, then upserted into
                    vector-search-system.md's ANN index

 migration path (on model version upgrade):
 new model deployed alongside old ──► backfill job re-embeds the corpus
 under the new version (tracked via migration_job) ──► validate new
 vectors against a held-out relevance check ──► atomic index cutover
 (or dual-serve both versions' indexes during a transition window) ──►
 old version deprecated after a grace period
```

## Deep Dives

**Encoder serving vs. decoder serving.** Contrasted directly with [llm-inference-serving.md](llm-inference-serving.md): an
embedding model is typically encoder-only, producing one output per input in a single forward
pass, with no KV cache, no per-token streaming, and no TTFT/ITL distinction to optimize
separately. The entire latency budget is one batched forward pass, which makes batching here a
pure request-accumulation-window tradeoff (wait longer, get a bigger and more efficient batch, at
the cost of added per-request latency) rather than the continuous-batching-plus-paged-KV-cache
machinery that dominates LLM serving design — a materially simpler serving problem, which is also
why its achievable per-GPU throughput is so much higher.

**Caching strategy and skew.** Because embedding is a pure function of `(input, model_version)` —
identical input always produces an identical vector, unlike an LLM generation call — content-hash
caching is exact-match and correctness-preserving, not an approximation of the "real" answer.
Cache effectiveness then depends entirely on real-world input skew, structurally the same
hot-key problem [feature-store.md](feature-store.md) addresses for its online store, just applied here to a
compute-avoidance cache rather than a data store — a small number of very-common queries or
re-embedded documents dominate cache hit volume, and the cache tier needs the same
replication/locality care around those hot keys.

**Embedding versioning and the re-embedding migration.** The operationally hard problem: upgrading
the embedding model doesn't just mean deploying new weights, it means every previously-computed
vector across every downstream index is now in a different, numerically incomparable vector
space. Migration requires re-embedding the full corpus under the new model (the ~5.6-hour figure
above) and then either cutting the consuming index over atomically, or maintaining both versions'
indexes in parallel during a transition window — a mixed-version index returns meaningless
similarity scores for any comparison that spans the version boundary. This is closer to a breaking
schema migration than a rolling software deploy, which is why `model_version` is a first-class key
threaded through every table in this data model rather than an incidental tag.

**Near-duplicate and semantic caching.** Exact content-hash caching only captures literally
identical inputs. Query text especially has many distinct strings that are semantically
near-identical ("cheap flights to Tokyo" vs. "affordable flights to Tokyo"), which a second,
approximate cache tier can catch: embed the query, do a small ANN lookup against a recent-query
cache index, and return the cached vector if a match exceeds a high similarity threshold. This
raises effective hit rate beyond what exact matching alone achieves, at the cost of a small risk of
returning a slightly-off cached vector for an input that was similar but not identical — production
systems set that threshold conservatively specifically to bound this risk rather than chase hit
rate aggressively.

**Multi-modal and mixed-cost inputs.** Text and image embedding have very different per-input
compute costs — an image encoder forward pass is typically far more expensive than a short-text
encoder pass. A shared embedding service fronting multiple modalities has to route requests to
modality-specific model pools rather than one undifferentiated fleet, and batch-accumulation
windows and GPU provisioning need to be tuned per modality independently — treating one cost model
as representative of all input types would badly under- or over-provision whichever modality
doesn't match it.

## Bottlenecks & Scaling

Cache hit rate is the single biggest lever on required fleet size — the gap between provisioning
for 8,000 QPS raw and 4,800 QPS post-cache is directly the cache's economic value, and the cache
infrastructure itself needs the same hot-key sharding discipline as any high-read KV store
(cross-ref [feature-store.md](feature-store.md)). Batch corpus re-embedding is bounded by whatever GPU fleet is
available for the migration window, and is deliberately run on a separate capacity pool from the
real-time serving fleet (cross-ref [gpu-cluster-scheduler.md](gpu-cluster-scheduler.md) for how that separation is enforced) so
a migration job never competes with live query-embedding latency SLAs. Embedding-space version
proliferation — keeping too many model versions live simultaneously for too long — multiplies both
cache footprint and downstream index storage, which is why prompt deprecation after a validated
cutover is an operational discipline here, not just housekeeping.
