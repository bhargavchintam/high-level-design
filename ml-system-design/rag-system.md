# Retrieval-Augmented Generation (RAG) System

## Requirements

### Functional
- Given a user query, retrieve relevant passages from a large document corpus and use them as
  grounding context for an LLM-generated answer, with citations back to source documents.
- Support incremental corpus updates — new and edited documents must become retrievable without
  reprocessing the entire corpus.
- Support multiple document types (long-form docs, PDFs, structured data extracts) normalized
  into a common chunked-and-embedded representation.
- Degrade gracefully when retrieval finds nothing relevant — answer from the model's own
  knowledge with a clear "no source found" signal, rather than fabricating a citation.

### Non-functional
- End-to-end p95 latency < 2s for retrieval + generation combined; retrieval itself should
  contribute well under 200ms of that (retrieval is not the bottleneck, generation is — but a
  slow retrieval stage steals budget from an already latency-constrained generation step).
- Freshness: newly ingested or edited documents should be retrievable within 15 minutes for
  fast-moving corpora (support tickets, product docs); a nightly batch reindex is acceptable for
  slower-moving reference corpora — freshness requirement is corpus-dependent and should be a
  configured SLA per source, not a single platform-wide number.
- Retrieval quality (recall@k of the actually-relevant passage) is the ceiling on answer
  quality — no amount of generation-side prompting fixes a retrieval miss, so retrieval
  correctness is evaluated and monitored as its own metric, independent of end-to-end answer
  quality.
- Grounding faithfulness: the generated answer should be checkable against the retrieved
  context (low hallucination rate relative to the provided sources) — this is evaluated
  separately from retrieval quality, since a system can retrieve the right passage and still
  generate an answer that isn't actually supported by it.

## Capacity Estimation

Corpus: 50M source documents, average 2,000 tokens/doc. Chunked at 512 tokens with 50-token
overlap → roughly 5 chunks/doc → **250M chunks** total.

Embeddings: dim 1024 (a mid-size embedding model), stored at fp16 → 250M × 1024 × 2 bytes ≈
512 GB raw vector data. Product-quantized for the ANN index (see vector-search-system.md for the
general technique) down to roughly 100-150 GB — this is the number that determines whether the
index fits in memory on a single large node or needs sharding across several.

Query volume: 5M queries/day → 5M / 86,400 ≈ 58 QPS average, ~175 QPS peak (3x). Each query
retrieves top-k=50 candidates from the ANN index, which are then reranked down to the top 5-8
that actually get placed in the LLM context window.

Reranking cost: 5M queries/day × 50 candidates reranked = 250M cross-encoder scoring ops/day →
~2,900/sec average, ~8,700/sec peak — cheaper than the ranking-model load in the recommendation
case study, but still enough to require a dedicated batched-inference reranking service rather
than scoring candidates one at a time inline.

Context assembly: top ~6 chunks × 512 tokens ≈ 3,000 tokens of retrieved context, plus system
prompt and query, typically lands total prompt size around 3.5-4K tokens fed into the
generation stage (see llm-inference-serving.md for what that costs downstream).

Ingestion throughput: assume 200K new/updated documents/day needing embedding → ~1M chunks/day
→ ~12 chunks/sec average embedding throughput needed for steady-state ingestion, spiky in
practice around bulk re-ingestion events (a source corpus reprocessed after a schema change),
which is why ingestion is a queue-backed batch pipeline rather than a service sized for average
load.

## API Design

```
POST /v1/query          { query, corpus_id?, top_k?: 6, filters?: {...} }
     -> 200 { answer, citations: [{doc_id, chunk_id, span, score}], retrieval_ms, generation_ms }

POST /v1/documents       { doc_id, content, metadata }
     -> 202 { status: "queued_for_ingestion" }   (async — see ingestion pipeline)

DELETE /v1/documents/{doc_id}
     -> 202   (must also remove all derived chunks/embeddings from the index — see deep dive)

GET  /v1/documents/{doc_id}/status
     -> { status: chunking | embedding | indexed | failed }
```

Returning `retrieval_ms` and `generation_ms` separately in the response isn't cosmetic — it's
the minimum observability needed to tell, per query, whether a latency regression is a retrieval
problem or a generation problem, which point at completely different parts of the system.

## Data Model / Feature Schema

```
documents (source of truth, one row per ingested document)
  doc_id, source, content_hash, version, updated_at, acl/permissions

chunks (derived, many per document)
  chunk_id, doc_id, chunk_index, text, token_count,
  span_start, span_end            (offsets back into the source document, for citation)

chunk_embeddings (vector index, one entry per chunk)
  chunk_id -> float16[1024]        (kept in sync with `chunks` — see deep dive on
                                     re-embedding invalidation)

query_log (for retrieval eval and reranker training data)
  query_id, query_text, retrieved_chunk_ids[], reranked_chunk_ids[],
  clicked/thumbs_up feedback, ts
```

`content_hash` on `documents` matters operationally: re-ingesting an unchanged document should
be a cheap no-op (hash match, skip re-chunking/re-embedding) rather than burning embedding
compute on content that hasn't changed — at 200K documents/day ingested, a naive "always
reprocess" pipeline wastes most of its throughput on unchanged content in any corpus with
partial daily updates.

## High-Level Architecture

```
 ingestion path:
 source doc ──► Ingestion service ──► chunker (fixed-size or semantic chunking)
                                                  │
                                        embedding model (batched inference)
                                                  │
                                        chunk_embeddings written to
                                        vector index (upsert, keyed by chunk_id)
                                                  │
                                        chunks + documents written to
                                        metadata store (source of truth)

 query path:
 client ──POST /query──► Query service
                              │
                              ├─► embed the query (same embedding model as ingestion —
                              │    must be the identical model/version, see deep dive)
                              │
                              ├─► ANN search over vector index ──► top-k=50 candidates
                              │
                              ├─► reranker (cross-encoder, query+chunk pairs) ──► top 5-8
                              │
                              ├─► context assembly (dedupe, order, truncate to token budget)
                              │
                              └─► LLM generation (context + query) ──► streamed answer
                                   with citations mapped back to chunk_id -> doc_id
```

## Deep Dives

**Chunking strategy.** Fixed-size chunking (e.g. 512 tokens with 10% overlap) is simple and
uniform but can split a coherent idea across a chunk boundary, hurting both retrieval (neither
half looks like a strong match for a query about the whole idea) and citation quality (the
answer's supporting evidence is split across two "sources"). Semantic chunking (splitting at
natural boundaries — paragraphs, sections, or via a model that detects topic shifts) produces
more coherent chunks at the cost of variable chunk size, which complicates batching in the
embedding pipeline and can produce chunks too large for the context budget if not capped. The
overlap between adjacent chunks exists specifically to reduce the boundary-splitting problem
without going fully semantic — a query whose answer straddles a boundary still has a decent
chance of being fully contained in the overlapping region of one chunk or the other.

**Embedding model consistency.** The query embedding at serving time and the chunk embeddings
computed at ingestion time must come from the exact same model version — an embedding-model
upgrade is not a drop-in swap, because old and new embeddings are not comparable in the same
vector space. Upgrading the embedding model requires re-embedding the entire corpus (250M
chunks in this example) before cutting serving traffic over, or running old and new indexes in
parallel during a migration window — there is no way to mix embedding versions in one ANN index
and get sensible similarity scores.

**Retrieval vs. reranking split.** ANN retrieval over the full corpus (250M chunks) uses cheap
bi-encoder embeddings (query and chunk embedded independently, compared by dot product/cosine) —
fast enough to search the whole corpus but limited in relevance precision because query and
chunk never actually attend to each other. The reranking stage runs a cross-encoder (query and
chunk concatenated and passed through a model jointly) on only the top-k candidates from
retrieval — far more accurate at judging relevance because the model can directly compare query
and passage, but too expensive to run against the full corpus. This mirrors the multi-stage
funnel in the recommendation-system case study: cheap-and-broad, then expensive-and-narrow.

**Context assembly and ordering.** Simply concatenating the top-k reranked chunks isn't free of
tradeoffs: near-duplicate chunks (common when a corpus has multiple versions of similar content)
waste context budget and should be deduplicated before insertion; the order chunks are placed in
the prompt measurably affects how much the model weights each one (a well-documented
"lost in the middle" effect in long-context generation), so the highest-relevance chunk is
typically placed nearest the query rather than first in retrieval order; and a hard token budget
means the assembly step must truncate gracefully (drop lowest-scored chunks entirely, not
truncate mid-chunk, since a half-sentence of grounding is worse than no grounding from that
source).

**Deletion and index consistency.** Deleting a document must remove every derived chunk from
both the metadata store and the vector index — a stale chunk embedding surviving in the ANN
index after its source document is deleted is a correctness bug (users get answers grounded in
content that's supposed to no longer exist, and for many corpora — e.g. anything with an access
control or right-to-be-forgotten requirement — this isn't just a quality issue but a compliance
one). Because ANN index deletes are often more expensive than inserts (some index structures
don't support cheap point deletion and instead mark-and-compact), deletion latency needs its own
SLA distinct from ingestion latency, and is frequently handled as tombstone-and-filter at query
time followed by periodic compaction, rather than synchronous removal.

**Evaluation.** Retrieval quality is measured independently of generation quality: recall@k
(does the known-relevant chunk appear in the top-k retrieved) and MRR against a labeled
query/relevant-chunk eval set, tracked as its own dashboard metric so a retrieval regression is
caught before it's masked by the generation model's ability to produce a plausible-sounding
answer anyway. Generation faithfulness is evaluated separately — typically via an
LLM-as-judge pass that checks whether each claim in the generated answer is actually supported
by the cited chunks — since a system can have perfect retrieval and still generate an answer
that drifts from what the retrieved context actually says.

## Bottlenecks & Scaling

The vector index is the component most sensitive to corpus growth — at 250M+ chunks it likely
needs sharding (see vector-search-system.md), and every sharding decision trades index-build and
memory cost against recall and per-query fan-out latency. The reranking stage is the main
latency and cost lever between retrieval and generation — running a cross-encoder over top-k=50
is far more expensive per query than the bi-encoder ANN search that produced those candidates,
so k is tuned to the smallest value that doesn't measurably hurt recall of the reranked set.
Ingestion throughput needs to scale independently from query throughput — a bulk reprocessing
event (schema change, embedding model upgrade) can spike ingestion load by orders of magnitude
for a short window without affecting query traffic at all, so the two are provisioned and scaled
as separate pools rather than one shared fleet. Generation cost (see llm-inference-serving.md)
typically dominates total per-query cost once context length grows past a few thousand tokens,
which is the practical pressure keeping context-assembly token budgets tight rather than simply
stuffing in every plausibly-relevant chunk.
