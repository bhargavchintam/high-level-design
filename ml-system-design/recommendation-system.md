# Large-Scale Recommendation System

## Requirements

### Functional
- Given a user (and context: device, time, surface — home feed, "related items", search
  re-ranking), return a ranked list of items the user is likely to engage with.
- Support multiple surfaces from one platform: home feed, related-item rails, notification
  candidates — each with different latency budgets and different definitions of "engagement."
- Incorporate real-time signals (an item clicked 30 seconds ago should influence the next
  request) alongside long-horizon signals (a user's stable topical interests).
- Support new items (cold start) and new users (cold start) without requiring weeks of
  interaction history before they can be recommended or recommended to.

### Non-functional
- End-to-end serving latency p99 < 150ms for the full candidate-generation + ranking pipeline;
  budget breakdown roughly 20ms retrieval, 80ms ranking, 50ms overhead/network — this is what
  bounds the model complexity allowed at each stage.
- Freshness: engagement signals (clicks, likes) should influence candidate retrieval within
  minutes, not hours — a purely daily-batch pipeline is stale for anything session-aware.
- Training-serving consistency: features computed at serving time must match what the model saw
  in training, or ranking quality degrades in ways that are hard to detect from offline metrics
  alone (see feature store deep dive).
- High availability with graceful degradation — if the ranking model or a feature lookup times
  out, fall back to a cheaper heuristic (e.g., popularity-ordered candidates) rather than
  failing the request.

## Capacity Estimation

Assume 50M DAU, each requesting recommendations ~6 times/day (home feed load + refreshes +
module reloads) → 300M requests/day. Average QPS = 300M / 86,400 ≈ 3,470/sec, peak ≈ 10,400/sec
(3x factor for diurnal + regional skew).

Catalog: 20M recommendable items (products, videos, posts). Item embeddings at dim 128, fp16 →
20M × 128 × 2 bytes ≈ 5.1 GB — small enough to hold entirely in memory in the ANN index, which
matters because retrieval latency is dominated by memory access pattern, not disk.

Candidate generation: each request retrieves ~500 candidates from the 20M-item corpus via
approximate nearest-neighbor search over the two-tower item embeddings.

Ranking: the 500 retrieved candidates are scored by a heavier model. Total scoring volume =
300M requests/day × 500 candidates = 150B scored (user, item) pairs/day → ~1.74M/sec average,
~5.2M/sec at peak. This is the number that sizes the ranking fleet: at a realistic ~50K
scores/sec per GPU for a batched DNN ranker with a few hundred input features, peak load needs
~104 GPUs in the hot path; provisioned with headroom and multi-AZ redundancy, call it ~150 GPUs.

Feature store: online features for 50M users × ~200 features × 8 bytes ≈ 80 GB; item features
for 20M items × ~300 features × 4 bytes ≈ 24 GB — both fit in a sharded in-memory KV store.

Training data volume: impression logs (item shown + label: clicked/not) at ~50 logged
impressions/request × 300M requests/day = 15B logged rows/day, ~150 bytes/row ≈ 2.25 TB/day raw
— this is downsampled (negative subsampling) before it becomes a training set, since the raw
logs are dominated by non-click negatives that carry diminishing marginal training value.

## API Design

```
GET  /v1/recommendations?surface=home_feed&user_id=...&count=20
     -> 200 { items: [{item_id, score, reason_code}], model_version }

POST /v1/events                 { user_id, item_id, event_type, ts }
     -> 202  (fire-and-forget; feeds the real-time feature pipeline)

GET  /v1/similar/{item_id}?count=20
     -> item-to-item recommendations (uses the item tower only, no user context)
```

The `surface` parameter matters more than it looks — home feed, related-items, and
notification-candidate generation are functionally separate recommendation problems (different
latency budgets, different label definitions) served through one API by routing to
surface-specific model configs internally.

## Data Model / Feature Schema

```
user_embedding_store (online, keyed by user_id)
  user_id -> float16[128]                 (from the user tower, refreshed periodically)

item_embedding_store (online + ANN index, keyed by item_id)
  item_id -> float16[128], metadata (category, created_at, popularity_bucket)

user_features (online store, keyed by user_id)
  recent_clicks_1h, recent_clicks_24h     (rolling counters, streaming-updated)
  topic_affinity_vector                   (float[50], updated hourly by batch job)
  device, locale, session_features

item_features (online store, keyed by item_id)
  age_hours, global_ctr_7d, category, creator_id
  embedding_freshness_ts                  (used to detect stale embeddings — see deep dive)

training_examples (offline, generated via point-in-time join)
  user_id, item_id, label (click/no-click), timestamp,
  [snapshot of every feature above AS OF timestamp, not as of query time]
```

The point-in-time join in `training_examples` is the single most failure-prone part of this
schema: joining features as they exist *now* instead of as they existed *at the time of the
logged impression* silently leaks future information into training and inflates offline metrics
that then fail to materialize online. See the feature-store case study for the general pattern.

## High-Level Architecture

```
 offline / training path:
 impression + engagement logs ──► stream processor (aggregates rolling features,
                                    writes to online feature store; also lands raw
                                    events in the offline warehouse)
                                              │
                        offline warehouse ──► point-in-time feature join ──► training set
                                              │
                        training job (two-tower retrieval model +
                        separate ranking model) ──► model registry
                                              │
                        item tower run in batch over full catalog ──► refreshed item
                        embeddings pushed to ANN index (periodic, e.g. every few hours)

 online / serving path:
 client ──GET /recommendations──► App/serving layer
                                         │
                                         ├─► user tower (real-time, or cached user embedding)
                                         │
                                         ├─► ANN index lookup (candidate generation,
                                         │    ~500 candidates from 20M-item corpus)
                                         │
                                         ├─► feature store lookup (online features for
                                         │    user + 500 candidates, batched)
                                         │
                                         ├─► ranking model (scores 500 candidates)
                                         │
                                         └─► business rules / diversity re-rank ──► top-N
                                              returned to client
```

## Deep Dives

**Two-tower retrieval.** Scoring all 20M items against a user directly, per request, is not
feasible inside a 20ms retrieval budget. The two-tower model factors the problem: a user tower
and an item tower are trained jointly (typically with an in-batch softmax / sampled-softmax
contrastive loss over positive engagements) so that relevance is approximated by embedding dot
product or cosine similarity. At serving time, item embeddings are precomputed offline for the
entire catalog and loaded into an ANN index; only the user tower runs online (or the user
embedding itself is cached and refreshed periodically), turning retrieval into a single ANN
query instead of 20M model evaluations. The tradeoff is that the two-tower architecture cannot
model user-item cross-features (an interaction term computed only at scoring time) — that
expressive power is deliberately deferred to the ranking stage, which sees far fewer items and
can afford it.

**Multi-stage funnel (retrieval → ranking → re-rank).** No single model is asked to be both fast
enough for 20M items and expressive enough to model fine-grained relevance. Retrieval narrows
20M → ~500 cheaply (ANN, no cross-features). Ranking narrows 500 → ~50-100 with a heavier model
that does see cross-features (user-item interaction terms, recency, session context) since it
only has to run on hundreds of candidates, not millions. A final business-rules / diversity
re-rank pass (dedupe similar items, enforce category diversity, inject a small exploration
slate) produces the final top-N — this stage is typically cheap heuristics, not another model,
because by this point the candidate pool is small and the goal shifts from relevance-scoring to
list-level constraints.

**Feature freshness and training-serving skew.** The ranking model is trained on features
computed via the offline point-in-time join; at serving time, the same feature names must be
computed by a different code path (the online feature store) under a tight latency budget. Any
divergence — a feature computed slightly differently online vs. offline, or a feature that's
fresher online than anything the model ever saw in training — degrades ranking quality in a way
that's invisible offline (offline eval uses offline-computed features) and only shows up as a
live metric regression. The standard mitigation is a shared feature-transformation library used
by both the training pipeline and the online serving path, so "recent_clicks_1h" is computed by
one piece of code regardless of which path calls it.

**Embedding staleness.** Item embeddings are refreshed in batch (every few hours, not
per-request), which means a newly published item has no embedding yet — this is the cold-start
problem for items. The common fix is a fallback path: brand-new items are injected into
candidate generation via a separate content-based or popularity-based path (not the ANN index)
until their learned embedding is available, and are up-weighted slightly in the re-rank stage to
give them enough exposure to accumulate the engagement signal the next training run needs.

**Exploration vs. exploitation.** A ranking model trained purely to maximize predicted
engagement converges toward recommending what's already popular, starving new and niche items of
the exposure needed to ever collect a training signal — a feedback loop, not a bug. Production
systems inject controlled exploration (a small random or under-explored slate mixed into the
top-N, or explicit uncertainty-aware scoring like Thompson sampling on a subset of slots) purely
to keep the training data distribution from collapsing onto what the current model already
believes.

## Bottlenecks & Scaling

The ranking stage is the dominant cost center — 150B scores/day vs. far cheaper retrieval — and
is the reason the funnel narrows aggressively before ranking rather than ranking a larger
candidate set directly. The ANN index needs to be sharded once the catalog no longer fits
comfortably in one node's memory, with results merged across shards (adds a fan-out/fan-in hop
to the retrieval budget). The online feature store is a shared dependency for every request and
needs enough replication and cache-locality (colocate hot user features near the ranking
service) that a feature-store latency spike doesn't blow the entire 150ms budget — a common
pattern is to set a hard feature-fetch timeout and serve with stale/default features rather than
block the request. Finally, the batch item-tower refresh job (recomputing embeddings for the
full catalog) scales with catalog size, not request volume, and is usually the first thing to
need its own distributed batch-processing budget independent of the online serving fleet.
