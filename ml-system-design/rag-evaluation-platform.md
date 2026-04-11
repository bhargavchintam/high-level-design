# RAG Evaluation Platform

## Requirements

### Functional
- Evaluate retrieval quality (recall@k, MRR, precision) and generation quality (faithfulness,
  answer correctness, relevance) for a RAG system (see rag-system.md) independently of each
  other, against curated golden query sets — not only as a single end-to-end pass/fail signal.
- Score generation quality dimensions that resist exact-match metrics (faithfulness, coherence,
  helpfulness) using LLM-as-judge, with the judge periodically calibrated against human-labeled
  examples so its verdicts stay trustworthy over time.
- Gate deploys: any change to the retrieval index, embedding model, prompt template, or
  generation model must run against the golden set before promotion, and must be blocked if it
  regresses quality beyond a configured threshold.
- Track regression over time (per-version score history), so a slow quality decay accumulated
  across many individually-small changes is visible on a trend line, not just single-change
  regressions caught at gate time.

### Non-functional
- Evaluation must run fast enough to gate CI/CD (minutes, not hours) for the fast-tier golden
  subset used on every change; a larger, slower full-tier evaluation runs on a less frequent
  cadence (e.g. nightly) for deeper coverage than the fast tier can afford per run.
- LLM-as-judge cost must be bounded and predictable — judge calls are themselves LLM inference and
  can dominate the evaluation platform's own cost if unmanaged, especially as golden-set size and
  deploy frequency both grow over a project's life.
- Evaluation results must be reproducible enough to trust as a gate — the same system version
  scored against the same golden set should produce a consistent verdict run over run, not one
  that flips on judge nondeterminism alone.
- Golden-set quality (label accuracy, coverage of known failure modes and edge cases) is the hard
  ceiling on what this platform can catch — the platform needs an explicit maintenance workflow
  for the golden set itself, not just for the scoring pipeline, since a stale or narrow golden set
  produces a gate that passes with false confidence.

## Capacity Estimation

Golden set: 5,000 curated queries, each with human-labeled relevant passages (for retrieval
metrics) and a reference answer or rubric (for generation metrics). A fast-tier subset of 500
queries, sampled to cover the platform's known failure categories, runs on every deploy candidate.

**CI-gating volume.** Assume 50 deploy candidates/day (prompt edits, retrieval config changes,
model version bumps) × 500 fast-tier queries = **25,000 query evals/day** for gating alone. A
full-tier nightly run adds 5,000 queries × 1 run/day = 5,000/day. Total: **30,000 query/response
pairs/day** requiring both retrieval scoring (cheap, vector math against labeled passages) and
generation scoring (LLM-as-judge, itself an inference call).

**Judge cost.** Assume ~2K input tokens per judge call (query + retrieved context + candidate
answer + rubric) and ~200 output tokens (verdict + reasoning). 30,000 calls/day × 2.2K tokens ≈
**66M tokens/day** spent purely on evaluation overhead. At a blended cost comparable to a mid-size
hosted judge model (~$3-5/1M input tokens), that's roughly $200-350/day — small in absolute terms,
but worth tracking explicitly since it scales with both deploy frequency and golden-set size, and
both trend upward as a project matures, unlike most infrastructure costs which trend down with
optimization.

**Fast-tier CI latency.** 500 queries, retrieved and scored for retrieval metrics in parallel
(~50ms/query, compute-only) plus judge calls batched at concurrency ~50: at ~3s/judge call, 500 /
50 = 10 batches × 3s ≈ **30s** judge-scoring wall-clock — comfortably inside a "minutes, not
hours" CI gate, with headroom for the retrieval-scoring pass and result aggregation around it.

**Golden-set maintenance load.** Assume ~5% of the golden set (250 queries) is reviewed or
refreshed per month as new failure modes are discovered in production and stale queries are
retired — a standing human-review workload, not a one-time construction cost, and the actual
long-run bottleneck on this platform (see Bottlenecks & Scaling).

## API Design

```
POST /v1/eval-runs
     { system_version, golden_set_tier: fast|full, changed_components: [...] }
     -> 202 { run_id, status: "running" }

GET  /v1/eval-runs/{run_id}
     -> { status, retrieval_metrics: {recall_at_5, mrr},
          generation_metrics: {faithfulness, correctness, relevance},
          per_query_results: [...], gate_decision: pass|warn|fail }

POST /v1/golden-set/queries
     { query, relevant_passage_ids: [...], reference_answer, rubric, category, difficulty }
     -> 201

GET  /v1/eval-runs/history?component=retrieval_index&window=90d
     -> [{run_id, version, score, ts}, ...]      # regression tracking over time
```

`changed_components` on `/eval-runs` is deliberate — knowing whether a run is gating a retrieval
change, a prompt change, or a generation-model swap lets the regression-history view (and a human
triaging a failed gate) isolate which layer of the RAG stack actually moved, instead of only
seeing an aggregate score drop with no attribution.

## Data Model

```
golden_query (one per curated eval example)
  query_id, query_text, relevant_passage_ids: [...]     (for recall@k / MRR)
  reference_answer, rubric                              (criteria used by the judge)
  category, difficulty, tier: fast|full, last_reviewed_at

eval_run (one per gating invocation)
  run_id, system_version, changed_components: [...], tier,
  status, gate_decision, started_at, completed_at

query_result (one row per query within a run)
  run_id, query_id, retrieved_passage_ids: [...],
  retrieval_recall_at_k, retrieval_mrr,
  generated_answer, judge_score: {faithfulness, correctness, relevance}, judge_reasoning

judge_calibration (periodic, tracks judge trustworthiness over time)
  judge_model_version, calibration_run_ts, human_agreement_pct, sample_size
```

`judge_calibration` is a first-class table, not an afterthought — a platform whose central gating
signal comes from an LLM judge needs to continuously answer "how much do we still trust this
judge," and that answer changes whenever the judge model itself is updated (including silently, by
a hosted-model provider), which is exactly the failure mode this table exists to catch.

## High-Level Architecture

```
 CI/CD trigger: deploy candidate (new prompt / retrieval config / model version)
                              │
                              ▼
                    Eval orchestrator
                              │
              pulls golden_query set (fast or full tier)
                              │
              ┌───────────────┴────────────────┐
              ▼                                  ▼
   for each query: run through candidate RAG pipeline (rag-system.md)
              │                                  │
              ▼                                  ▼
   Retrieval scorer                    Generation scorer
   (deterministic — recall@k,           - deterministic checks (citation
   MRR against relevant_passage_ids)      present, format valid)
              │                          - LLM-as-judge (faithfulness vs.
              │                            retrieved context, correctness
              │                            vs. reference_answer/rubric)
              └───────────────┬──────────────────┘
                               ▼
              aggregate per-run metrics ── compare vs. configured
              thresholds ──► gate_decision: pass | warn | fail
                               │
                               ▼
              write eval_run / query_result, append to
              regression-tracking history
                               │
                fail ──► block deploy, notify owning team
                pass ──► allow promotion

 (async, off the gating path)
 Judge calibration job: samples query_results periodically ──► routed to
 human reviewers ──► compares judge verdict vs. human verdict ──►
 judge_calibration (drives judge_model_version trust and re-selection)
```

## Deep Dives

**Golden set construction and maintenance.** A golden set assembled once and never revisited is
the single most common way this kind of platform quietly loses value: it evaluates against
yesterday's failure modes while the production system has moved on to today's. Sourcing draws from
three distinct places that each catch a different class of problem — sampled production query
logs (representative of real usage, catches distribution shift), deliberately authored
adversarial/edge-case queries (catches known-hard patterns like ambiguous phrasing or
multi-document synthesis, which real traffic under-represents relative to how often it actually
causes failures), and regression cases promoted from confirmed production incidents (guarantees a
past failure never silently reoccurs). Tiering exists purely for cost: the fast subset is chosen
to maximize category coverage per query rather than raw count, so 500 queries catch a
representative slice of the full 5,000-query set's failure categories at a tenth of the judge-call
cost. A golden set that only grows without ever retiring stale or redundant queries eventually
blows the CI latency budget the fast tier exists to protect — curation is a continuous editorial
process, not a monotonically growing archive.

**Retrieval metrics.** Recall@k (does at least one labeled-relevant passage appear in the top-k
retrieved) and MRR (how highly the first relevant passage ranks, rewarding it appearing earlier)
are computed directly against `golden_query.relevant_passage_ids`, independent of anything the
generation stage does with those passages — this mirrors the retrieval-quality-as-its-own-metric
principle from rag-system.md, applied here as an automated, versioned gate rather than a one-off
manual check. The genuine subtlety is what counts as "relevant": many real queries have more than
one passage that could correctly ground an answer (several sections of a document say materially
the same thing), and a golden set that labels only a single passage as relevant per query will
score a retrieval change as a regression when it actually swapped one valid source for another
equally valid one. Golden queries with multiple acceptable relevant passages need to record all of
them, not the single passage a labeler happened to pick first.

**LLM-as-judge design.** A rubric-based prompt (explicit criteria the judge checks against, not an
open-ended "is this a good answer?") produces materially more consistent verdicts across similar
queries than an unconstrained judging prompt, because it narrows what the judge is actually
scoring rather than leaving "good" to the judge's own unstated notion of quality on each call.
Faithfulness scoring is reference-free (checked only against the retrieved context: is every claim
in the answer actually supported by what was retrieved), while correctness scoring is
reference-based (checked against `reference_answer`) — these catch different failure modes and are
reported as separate scores rather than blended into one number, since an answer can be fully
faithful to a wrong or incomplete retrieval and still fail correctness, or can be broadly correct
while including an unsupported claim the retrieved context never backs. The judge model is
deliberately chosen to differ from (typically larger or a different family than) the model being
evaluated, to avoid self-preference bias — a model scoring its own outputs as a judge has a
measurable tendency to rate its own generation style more favorably, independent of actual
quality. Judge output is constrained to a structured schema (a fixed verdict field plus a
reasoning field), not free text, so `query_result.judge_score` is reliably parseable rather than
requiring its own downstream extraction step that can itself fail silently.

**Judge calibration and reliability.** The judge is a model, and models drift — both the
evaluated system and the judge model itself can change (a hosted judge model gets silently updated
by its provider, or its behavior shifts across a version bump the platform didn't explicitly
opt into). `judge_calibration` runs on its own periodic cadence: a sample of `query_result` rows
is routed to human reviewers who independently score the same query/answer pairs, and
`human_agreement_pct` tracks how often the judge's verdict matches the human's. A sustained drop
in agreement is the signal that the judge itself, not the system under test, needs attention —
distinguishing "the RAG system regressed" from "the judge stopped being reliable" is exactly what
this feedback loop exists to make possible, since without it a judge that silently degraded would
present as either a system regression that isn't real or a system improvement that isn't real,
indistinguishably. For gates where a wrong pass/fail decision is expensive, ensemble judging
(multiple judge calls, majority vote or averaged score) trades additional judge-call cost for
reduced variance in the verdict — a reasonable tradeoff for the full-tier nightly run, usually not
worth the added cost and latency for the fast-tier gate running 50 times/day.

**Regression gating in CI/CD.** A gate defined purely as an absolute floor ("faithfulness must
exceed 0.9") is simple but brittle to golden-set composition changes — adding harder queries to
the fast tier can drop the aggregate score below the floor with no actual regression in the system
under test. A gate defined purely as relative-to-previous-run is more robust to that, but can let a
system drift downward through a long sequence of individually-tiny regressions, each one passing
its own relative check. Production gates typically combine both: a relative-regression check
against the last known-good version (catches sudden drops from a specific change) plus a slower-
moving absolute floor re-baselined periodically (catches gradual drift the relative check alone
would let through one small step at a time). A `warn` tier — flagged for review, not blocking
deploy — exists specifically for changes that trade one metric for another in a way that might be
intentional (a retrieval change that trades some recall for materially better latency, say) and
that a hard binary gate would either wrongly block or wrongly wave through.

## Bottlenecks & Scaling

Judge-call cost and latency are the binding constraint on how large or how frequent an evaluation
run can be, which is the entire reason the fast/full tiering exists rather than running the full
golden set on every deploy candidate. Golden-set curation is a human-labor bottleneck that does
not scale with deploy frequency the way automated scoring does — a team can ship ten deploy
candidates a day against a golden set that only gets meaningfully reviewed once a month, and in
practice curation throughput, not compute, is usually the actual ceiling on how much real eval
coverage a team sustains over time. Judge nondeterminism and drift directly threaten the
reproducibility requirement at scale and need the dedicated `judge_calibration` monitoring loop
rather than an assumption that judge behavior is stable once validated at launch. Finally, this
platform's usefulness is structurally coupled to the production RAG system it evaluates — new
corpora, new query patterns, or a new user-facing surface in rag-system.md all mean the golden set
has to evolve to track them, or the eval scores keep measuring a version of the problem the
production system has already moved past; that coupling is a standing maintenance dependency
between the two systems, not a one-time integration done once at platform launch.
