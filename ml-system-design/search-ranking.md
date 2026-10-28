# Multi-Stage Search Ranking

## Requirements

### Functional
- Given a query (and context: user, device, locale, time), return a ranked list of results
  from a corpus far too large to score exhaustively per query.
- Implement ranking as a funnel: retrieval (candidate generation over the full corpus) -> L1
  ranking (cheap, wide) -> L2 ranking (expensive, narrow) -> business-logic re-ranking — each
  stage trading off scoring cost against candidate-set size.
- Support both lexical/keyword matching and semantic/intent matching — a query and the best
  matching document don't always share vocabulary.
- Surface very recently published or updated documents (news, listings, posts) even though the
  primary index refresh cycle is too slow to include them yet.

### Non-functional
- End-to-end latency budget in the low hundreds of milliseconds (p99), tightly allocated across
  every funnel stage — no single stage can eat the whole budget.
- Freshness: time-sensitive documents need to be searchable within seconds to minutes of
  publication, not on the index's normal refresh cycle, which is far slower for the bulk corpus.
- Scale to a corpus of billions of documents, growing continuously, without linear growth in
  per-query latency — this is the entire reason the system is a funnel and not a single ranking
  pass.
- Ranking quality has to be learned from behavioral data (clicks) at scale, and that data is
  itself biased by what was already ranked highly — see the position bias deep dive.

## Capacity Estimation

Assume 6B queries/day globally → 6B / 86,400 ≈ 70,000 queries/sec average, peak (3x diurnal/
regional factor) ≈ 210,000/sec.

Corpus: 10B documents. Scoring all 10B per query with any real model is off the table — even at
a wildly optimistic 10M cheap-scores/sec per node, one query would take a full second on a
single node before any parallelism, and that's before the actually-expensive L2 model runs at
all. This is the numerical justification for narrowing aggressively before any learned model
runs.

Funnel narrowing per query: retrieval takes 10B documents down to ~5,000 candidates (inverted-
index + ANN lookup, no learned scoring yet); L1 ranking takes 5,000 -> ~300 with a cheap model;
L2 ranking takes 300 -> ~50 with an expensive model; business-logic re-ranking takes 50 -> the
~10-20 actually returned to the page.

Scoring throughput per stage: L1 = 70,000 qps × 5,000 candidates ≈ 350M scores/sec average —
this has to be a genuinely cheap model (linear or shallow GBDT) to be affordable at all. L2 =
70,000 qps × 300 candidates = 21M scores/sec average, ~63M/sec peak — still large, but two
orders of magnitude down from L1, which is what makes it affordable to run something as
expensive as a cross-encoder or a large LTR ensemble at this stage.

Latency budget (p99, total ~250ms): query understanding ~10ms, hybrid retrieval ~40ms, L1
ranking ~30ms, L2 ranking ~100ms, business-logic re-rank plus network/serialization overhead
~70ms. L2 gets the largest single slice despite scoring the fewest candidates, because it's
running the most expensive model in the pipeline (see bottlenecks).

Index storage: inverted index — 10B docs × ~200 unique indexed terms/doc × ~4 bytes/compressed
posting ≈ 8 TB, sharded across nodes (see [concepts/database-sharding.md](../concepts/database-sharding.md)). Dense vector index —
10B docs × 256-dim embeddings × fp16 (2 bytes) ≈ 5.1 TB, also sharded; see
[concepts/embeddings-and-vector-search.md](../concepts/embeddings-and-vector-search.md) for ANN index structure.

Training data: 6B queries/day × ~10 results shown/query (logged as impressions) = 60B logged
impression rows/day, ~150 bytes/row ≈ 9 TB/day raw click-log volume before position-bias
correction and downsampling.

## API Design

```
GET  /v1/search?q=...&user_id=...&count=20&surface=web
     -> 200 { results: [{doc_id, title, snippet, score}], query_id }

POST /v1/events/click   { query_id, doc_id, position, ts } -> 202
     (fire-and-forget; feeds the click-log pipeline used for both training and freshness)

GET  /v1/index/freshness/{doc_id}   -> indexed_at, in_fast_path (internal/debug)
```

## Data Model

```
inverted_index (sharded by term, see concepts/database-sharding.md)
  term -> postings [(doc_id, term_freq, positions), ...]     # BM25-style lexical scoring

vector_index (sharded ANN, see concepts/embeddings-and-vector-search.md)
  doc_id -> float16[256]     # dense embedding, dual-encoder/two-tower trained

document_store (keyed by doc_id)
  doc_id -> title, body, metadata, published_at, static_quality_score

fast_path_index (small, in-memory, short-lived)
  doc_id -> embedding + minimal features     # docs too new for the main index refresh cycle

click_log (offline, append-only)
  query_id, doc_id, position, clicked (0/1), ts, query_features, doc_features_snapshot

training_examples (offline, LTR training set, grouped by query_id)
  query_id, doc_id, position, propensity-corrected label, [query/doc feature snapshot]
```

## High-Level Architecture

```
 training path:
 click_log ──► position-bias correction (inverse propensity scoring /
               randomized-slot data, see deep dive) ──► training_examples
                                    │
                     ┌──────────────┴───────────────┐
                     │                               │
          LTR training (pointwise/                two-tower / dual-encoder
          pairwise/listwise,                       training (query tower +
          e.g. LambdaMART or a                     doc tower, contrastive
          gradient-boosted/deep                    loss) ──► doc embeddings
          ranker) ──► L1/L2 ranking                 computed in batch over
          models ──► model registry                full corpus ──► pushed
                                                     to vector_index

 serving path:
 query ──► query understanding (spell-correct, intent/entity parse)
                    │
                    ├──► hybrid retrieval: inverted_index (lexical, BM25) +
                    │    vector_index (ANN, semantic) + fast_path_index (recency)
                    │    merged ──► ~5,000 candidates
                    │
                    ├──► L1 ranking: cheap wide model over 5,000 -> ~300
                    │
                    ├──► L2 ranking: expensive narrow model (cross-encoder /
                    │    large LTR ensemble) over 300 -> ~50
                    │
                    └──► business-logic re-rank (dedupe, diversity, freshness
                         boost, policy filters) ──► top-N returned to client
```

## Deep Dives

**Why the multi-stage funnel exists.** Running an expensive, high-accuracy ranking model (a
cross-encoder, a large gradient-boosted ensemble with hundreds of features) directly over a
10B-document corpus per query is not a latency problem that more hardware fixes — at 70,000 qps
it's simply not affordable at any budget, and it wouldn't fit the latency envelope even with
unlimited hardware, since per-document scoring cost times corpus size dwarfs the per-query time
budget regardless of parallelism. The funnel exists to spend cheap compute at large scale
(retrieval, L1) to aggressively cut the candidate set, so that expensive compute (L2) only ever
runs on a few hundred candidates — the same retrieval-then-rank tradeoff
[ml-system-design/recommendation-system.md](recommendation-system.md) makes with its two-tower/ranking split, applied here
to a much larger corpus and an explicit third funnel stage.

**Hybrid retrieval.** Lexical retrieval (an inverted index scored with something like BM25 —
term frequency weighted by inverse document frequency, no learned model involved) is fast, exact
on vocabulary matches, and completely blind to synonymy or paraphrase: a query for "cheap
flights" won't match a document that only says "affordable airfare" no matter how relevant it
is. Embedding-based ANN retrieval (dense query/doc embeddings, nearest-neighbor search — see
[concepts/embeddings-and-vector-search.md](../concepts/embeddings-and-vector-search.md)) captures semantic similarity across vocabulary gaps
but can miss precise keyword/entity matches that lexical retrieval nails trivially (an exact
product SKU, a proper noun), since embeddings compress meaning and can blur exact-match signal.
Running both and merging/deduplicating the candidate sets covers each approach's blind spot with
the other's strength, which is why production retrieval is almost never purely one or the other.

**Learning-to-rank approaches.** Pointwise LTR treats ranking as independent regression/
classification per (query, doc) pair (predict a relevance score, sort by it) — simple to train
and reason about, but it optimizes each prediction independently and never explicitly models
that what matters is the *relative order* of documents within a query, not the absolute score of
any one of them. Pairwise LTR (RankNet, LambdaRank, and gradient-boosted variants like
LambdaMART) instead trains on ordered pairs within a query — is doc A more relevant than doc B —
which directly targets ranking correctness and generally beats pointwise approaches on ranking
quality metrics (NDCG) for a comparable amount of model capacity. Listwise LTR goes further and
optimizes a loss over the entire ranked list for a query at once (e.g. a smoothed,
differentiable approximation of NDCG), which best matches what's actually being evaluated but is
more complex to train. LambdaMART (pairwise gradients computed with respect to the change in
NDCG a swap would cause, boosted over trees) remains a strong, standard choice for the L2 stage
specifically because it delivers pairwise-quality ranking without the full complexity of a
listwise loss, and tree ensembles are cheap enough to serve at the L2 candidate-count scale.

**Freshness handling.** The main index (inverted + vector) is refreshed on a cycle — batch
rebuild or incremental merge — that's too slow (minutes to hours, depending on corpus size) for
content where relevance is inherently time-sensitive: breaking news, a just-listed item, a post
from moments ago. A separate fast-path index holds only very recent documents, is small enough
to update near-instantly as documents arrive, and is queried alongside the main index at
retrieval time (see fast_path_index in the data model and the retrieval stage in the
architecture diagram). Recency itself also becomes a ranking feature at L1/L2 and a signal in
the business-logic re-rank stage (a freshness boost for time-sensitive query intents), separate
from the question of whether a document is indexed at all.

**Position bias in click logs.** Click data used naively as a relevance label is confounded by
position: a document clicked because it was ranked #1 gets credit for relevance it may not
actually have, while a genuinely relevant document ranked #8 gets systematically under-clicked
simply because fewer users scroll that far — training directly on raw clicks teaches the model
to reproduce the current ranker's biases rather than to improve on them. Two standard
corrections: inverse propensity scoring (estimate, per position, the probability a result at
that position gets examined at all, then reweight each click/non-click by the inverse of that
probability so a click at a rarely-examined position counts for more) and randomized-slot data
(periodically inject randomization into result order for a small slice of traffic, producing
click data that isn't confounded by position because position itself was randomized) — the
latter is cleaner but costs measurable relevance quality on the traffic it's run against, so it's
typically kept to a small, deliberately sacrificed slice.

## Bottlenecks & Scaling

L2 ranking is the tightest part of the latency budget despite scoring the fewest candidates,
because it deliberately runs the most expensive model in the pipeline — any regression in L2
model latency (a model size increase, a slow feature lookup) has nowhere to hide the way it
might at L1, where there's more raw budget margin per candidate. Index sharding is unavoidable
past a few billion documents (see [concepts/database-sharding.md](../concepts/database-sharding.md)) — both the inverted index and
the vector index are partitioned across nodes, which turns retrieval into a fan-out/fan-in
(query every shard, merge top-K results) and adds a tail-latency risk: the slowest shard sets
the retrieval latency for the whole query, not the average shard. Index refresh cost scales with
corpus size and update rate, not query volume, and is usually run as its own batch/streaming
pipeline independent of the serving fleet (analogous to the item-tower refresh job in
[ml-system-design/recommendation-system.md](recommendation-system.md)) — the fast-path index exists specifically because
that refresh cadence can't be sped up enough to cover freshness-sensitive content without one.
Embedding staleness compounds this: a document's vector embedding reflects the model version and
content state at last refresh, so between refreshes, semantic retrieval quality for that
document slowly drifts from what the current model would produce if re-run — generally
tolerated as a bounded, temporary degradation rather than solved by refreshing continuously,
since continuous full-corpus re-embedding at this scale is itself a large distributed compute
job (see [concepts/distributed-training.md](../concepts/distributed-training.md) for the general pattern).
