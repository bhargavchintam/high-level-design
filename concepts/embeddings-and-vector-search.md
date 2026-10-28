# Embeddings and Vector Search

An embedding is a dense vector representation of an object — text, image, user, product — such
that geometric proximity in the vector space encodes semantic similarity. Two sentences with
similar meaning map to nearby points even with no shared words; two products a user tends to buy
together map to nearby points even across catalog categories. This is the opposite of sparse
representations like one-hot or bag-of-words, where similarity requires exact token overlap and
dimensionality scales with vocabulary size. Dense embeddings (128-1536 dimensions is typical)
turn "is this similar to that" into a nearest-neighbor problem in a fixed-size vector space.

## Training embeddings

The dominant approach is contrastive learning: pull embeddings of "positive" pairs together and
push "negative" pairs apart. A positive pair might be a search query and the document clicked
for it, or two augmented views of the same image. The loss (commonly InfoNCE) needs negatives to
push against, and the cheap source is in-batch negatives — for a batch of N positive pairs, every
other item doubles as a negative for each anchor, giving N-1 negatives per example for free.
Larger batches mean more negatives per step and generally better embeddings, so contrastive
training is often bottlenecked on batch size more than model size.

Two-tower architectures are the standard shape for retrieval: a query encoder and an item
encoder, trained jointly so their outputs land in the same vector space and can be compared
directly. The split matters operationally — item embeddings depend only on item content, so they
can be computed once, offline, and loaded into an index, while the query embedding depends on
the live request and is computed at serving time, but as a single cheap forward pass through one
tower. This asymmetry is what makes large-scale retrieval tractable: the expensive item-side
computation never runs at request time.

## Why exact nearest-neighbor doesn't scale

Given a query vector, exact k-NN means computing distance to every vector in the corpus and
taking the top k — O(N) per query, survivable at a million vectors but not at a billion under a
tight latency budget. Approximate nearest-neighbor (ANN) search trades a small, tunable amount
of recall for orders of magnitude less work per query by avoiding a full scan.

- **HNSW (Hierarchical Navigable Small World)**: a multi-layer graph where each vector connects
  to its approximate neighbors; upper layers have long-range links for fast coarse navigation,
  lower layers have short-range links for fine-grained search. A query walks the graph greedily
  from the top layer down, converging on the nearest region in roughly logarithmic hops. Strong
  recall/latency tradeoff, but the graph plus full vectors are memory-hungry to keep in RAM.
- **IVF (inverted file index)**: cluster the corpus offline (k-means), and at query time search
  only the handful of clusters whose centroids are closest to the query instead of the whole
  corpus. Cheaper to build and hold in memory than HNSW, but recall depends heavily on `nprobe`
  (how many clusters get searched) and degrades for points near a cluster boundary.
- **Product quantization (PQ)**: a compression technique, not a search structure — split each
  vector into subvectors and quantize each to the nearest of a small set of learned centroids,
  so a 768-dim float32 vector shrinks to a handful of bytes. Cuts memory by an order of magnitude
  or more at the cost of some recall. Commonly layered on IVF as IVF-PQ: IVF narrows the search
  to a few clusters, PQ makes scanning within them cheap to store and compute.

None of these dominates on all three axes of recall, latency, and memory — that's the
fundamental tradeoff triangle of ANN search. HNSW wins on recall-per-latency but costs the most
memory per vector; IVF is cheaper to build and smaller but leans on `nprobe` tuning to hold
recall steady; PQ buys a large memory reduction at a tunable recall cost. Picking an approach
means picking where a workload can afford to sit on that triangle — a latency-critical,
RAM-constrained candidate-generation stage tolerates more approximation than a final reranking
stage over a few hundred candidates.

## Vector databases

Purpose-built vector databases (Pinecone, Milvus, Weaviate, Qdrant) build around ANN indexes as
the primary access pattern, handling sharding, index building, and filtering-plus-ANN queries
(e.g. nearest neighbors among items where category = X) as first-class features, and generally
give the best raw performance for pure vector workloads. The alternative is adding a vector
index to a store you already run — pgvector on Postgres, Elasticsearch/OpenSearch vector fields —
sacrificing some specialized performance to avoid operating a new system and keep embeddings next
to the relational data they describe. The question is usually whether the workload is
vector-search-primary (favor a dedicated system) or vector-search-as-a-feature (favor bolting it
onto what's already there).

## Use cases

- Semantic search / retrieval: match queries to documents by meaning, not keyword overlap.
- Recommendation candidate generation: embed users and items in a shared space, retrieve nearest
  items to a user vector before a heavier reranking stage — see [../ml-system-design/search-ranking.md](../ml-system-design/search-ranking.md)
  and [../ml-system-design/ad-click-prediction.md](../ml-system-design/ad-click-prediction.md) for how retrieval feeds a full ranking pipeline.
- Deduplication / near-duplicate detection: near-identical items land close together even with
  superficial differences in text or pixels.
- RAG (retrieval-augmented generation): embed a corpus, retrieve the top-k chunks relevant to a
  prompt, feed them into an LLM's context instead of relying on parametric memory alone — see
  [llm-inference-optimization.md](llm-inference-optimization.md) for what happens to that context once it hits the model.

## Tradeoffs

Item embeddings computed offline drift out of sync with a live catalog or corpus — a
description changes, a document gets edited, a user's behavior shifts — and the stored
embedding doesn't reflect it until the next reindex. That forces a choice between reindexing
cadence (cost) and staleness (relevance), the same freshness-vs-simplicity tradeoff behind
cache invalidation (see [caching.md](caching.md)). The exact-vs-approximate tradeoff is the other constant:
ANN indexes give up a small, measurable amount of recall for latency and memory improvements
that are usually multiple orders of magnitude, and nearly every production vector search system
makes that trade deliberately rather than run exact k-NN — the recall loss is tunable, and the
alternative is often simply too slow to serve.
