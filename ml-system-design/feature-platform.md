# Feature Engineering Platform

This is the layer upstream of feature-store.md: that document covers the online/offline *store*
and the point-in-time-correctness contract it serves reads under; this one covers the streaming
and batch *pipelines* that compute the values landing in that store, and the org-wide tooling
(definition framework, catalog, backfill, versioning) that lets hundreds of engineers produce
features without each hand-building two parallel compute paths. Read feature-store.md first if
you haven't — this document assumes and extends its online/offline split rather than re-deriving
it.

## Requirements

### Functional
- Let any team define a feature transformation once, in a shared declarative framework, and have
  it compiled automatically into both a streaming pipeline (real-time updates) and a batch
  pipeline (historical backfill and periodic recompute) — without hand-writing two independent
  implementations that can silently drift apart (the concrete mechanism behind
  feature-store.md's training-serving-skew mitigation, not just a restatement of the problem).
- Backfill historical values for a newly-defined feature across the retention window, so it's
  usable in training immediately rather than only accumulating forward from the moment it's
  defined.
- Provide a searchable feature catalog — what features exist, computed from what sources, owned
  by whom, consumed by which models — so teams can discover and reuse existing features instead
  of redundantly redefining near-duplicates.
- Support schema evolution of a feature definition (a changed aggregation window, an added
  derived field) without breaking models already pinned to the prior version.

### Non-functional
- A new or updated feature definition should take effect in both compute paths within a bounded
  window (under an hour) — slow iteration here directly slows every downstream model team's
  development velocity, not just this platform's own team.
- Backfill jobs must reprocess months to years of historical events without disrupting the
  concurrently-running streaming pipelines computing today's features from the same underlying
  event sources.
- Schema/definition changes are backward compatible by default — existing consumers pinned to a
  version keep working unchanged — the same enforced-immutability principle as
  feature-store.md's `event_timestamp`-keyed store, applied one layer up to the definition itself.
- Pipeline health (freshness-SLA violations, schema drift, backfill failures) must be independently
  alertable per feature, not just visible as an aggregate pipeline-health signal — a single
  silently-broken feature can degrade one model's quality without moving any platform-wide metric.

## Capacity Estimation

**Aggregate ingestion volume.** This platform aggregates event sources across the whole org, not
one product surface — assume ~2B raw events/day (an order of magnitude above
feature-store.md's single-source 200M/day example, consistent with many teams' event streams
feeding the same platform) → 2,000,000,000 / 86,400 ≈ **23,000 events/sec average, ~70,000/sec
peak**.

**Streaming cluster sizing.** At a representative ~15,000 events/sec/core for typical windowed
aggregation work (heavily dependent on transformation complexity — this is a planning figure, not
a hard constant), the bare-minimum compute for 70,000 events/sec peak is ~5 cores — but real
deployments run this across dozens to roughly 100 cores per major pipeline stage once
partitioning overhead, windowed-state-store I/O, and burst headroom are accounted for; the
theoretical minimum badly understates real provisioning here the same way naive QPS math
understates real fleet size elsewhere in this repo.

**Backfill throughput, worked example.** Backfilling one new feature across a full 2-year
retention window: 2B events/day × 730 days ≈ **1.46 trillion historical events**. At a realistic
batch-processing throughput of ~500,000 events/sec/executor-fleet (no real-time constraint, heavy
parallelism): 1.46×10¹² / 5×10⁵ ≈ 2.9×10⁶ sec ≈ **~34 days** for a full-history backfill on one
modestly-sized fleet. This is precisely why production backfills are scoped to a bounded lookback
(commonly 90-180 days, matching what most training sets actually consume) rather than full
history — the same math at 180 days instead of 730 drops the job to roughly 2.9×10⁶ × (180/730) ≈
**~8.5 days**, and scoping further or parallelizing across a larger fleet brings it into a
low-single-digit-day window in practice.

**Catalog/metadata load.** A few hundred feature definitions with rich metadata, queried
interactively by engineers browsing/searching — structurally the same low-volume,
high-read-value shape as ml-experiment-tracking-platform.md's run-metadata store, and never a
capacity concern at this scale.

## API Design

```
POST /v1/feature-definitions
     { name, entity_type, transformation: { type: streaming | batch | both, source,
       logic: "windowed_agg(field, window, fn)" },   # declarative DSL, not free-form code
       owner, freshness_sla }
     -> 201 { feature_id, version: 1 }
     (compiles to both a streaming job spec and a batch job spec from this one definition)

POST /v1/feature-definitions/{feature_id}/backfill
     { lookback_window }
     -> 202 { backfill_job_id }

GET  /v1/catalog/search?q=...
     -> [{ feature_id, name, owner, consumers: [model_names],
            freshness_sla, last_computed_at }]

GET  /v1/pipelines/{feature_id}/health
     -> { streaming_lag_sec, last_batch_run_status, schema_drift_detected: bool }
```

This API is entirely about defining and operating the pipelines that *produce* feature values —
it's deliberately distinct from feature-store.md's `get-features` / `get-historical-features`
read API, which is about *consuming* the values these pipelines write.

## Data Model

```
feature_definition (versioned, immutable per version — new versions are layered,
  never mutated in place)
  feature_id, version, name, entity_type, transformation_spec (DSL),
  owner, freshness_sla, created_at

compiled_pipeline (the two generated artifacts from one definition)
  feature_id, version -> streaming_job_ref, batch_job_ref

backfill_job
  backfill_job_id, feature_id, lookback_window, status,
  progress_pct, started_at

pipeline_health (continuously updated)
  feature_id, streaming_lag_sec, last_successful_batch_run_ts,
  schema_drift_flag

catalog_index (denormalized, search-optimized view over feature_definition
  plus consumer references, backing the discovery API)
```

## High-Level Architecture

```
 definition path:
 engineer ──POST /feature-definitions──► Compiler: one declarative
                                            transformation -> two execution
                                            plans (streaming + batch)
                                                    │
                        ┌───────────────────────────┴───────────────────────┐
                        ▼                                                   ▼
        Streaming job (deployed to Flink/Kafka-Streams-      Batch job (deployed to
        style cluster; windowed aggregation, incremental     Spark/Airflow-style
        updates)                                              scheduler; periodic
                        │                                     recompute + handles
                        │                                     cold-start backfill)
                        ▼                                                   ▼
        feature-store.md's ONLINE store                    feature-store.md's OFFLINE
        (this platform is upstream of that                 store (same relationship)
        store, not a replacement for it)

 backfill path (isolated from live pipelines):
 POST /backfill ──► Backfill job reads historical raw events from the
                     offline event log / data lake (NOT the live Kafka
                     topic the streaming job consumes) ──► writes into
                     the same offline store, reconciled against
                     feature-store.md's point-in-time semantics

 observability path (continuous):
 Pipeline health monitor ──watches──► streaming_lag_sec per feature vs.
 freshness_sla, schema drift, batch run success ──► alerts per feature
 (not aggregate) ──► feeds catalog_index freshness display
```

## Deep Dives

**One definition, two compiled pipelines.** This is the structural fix for
feature-store.md's training-serving-skew problem, not just a description of it: a declarative
transformation like `windowed_agg(clicks, window=1h, fn=count)` is compiled by the platform itself
into both a streaming execution plan and a batch execution plan, so the online and offline paths
are generated from one source of truth rather than independently hand-written and prone to
drifting apart on window-boundary semantics or late-arriving-event handling. The platform's
compiler — not developer discipline — is what guarantees the two paths agree, which is a
meaningfully stronger guarantee than "a shared library both paths are supposed to call."

**Backfill orchestration without disrupting live pipelines.** A backfill job reprocesses historical
raw events through the same transformation logic as the live streaming pipeline, but must not
contend for the Kafka partitions/consumer groups or warehouse read capacity the live pipeline
depends on. Backfills therefore read from an isolated path — the offline event log / data lake,
not the live topic — specifically so a large historical backfill (the multi-day jobs estimated
above) can never starve or lag the real-time freshness SLA of every other feature sharing the same
underlying streaming infrastructure.

**Schema evolution and versioning.** A feature definition change is published as a new version,
never a mutation of the existing one. Consumers pinned to version N keep receiving version N's
values unchanged (its compiled pipelines keep running untouched), while new consumers can adopt
version N+1 independently; an old version's compiled pipelines are torn down only after its last
known consumer migrates, or an explicit deprecation policy elapses. This mirrors the same
backward-compatible evolution discipline as API versioning, applied to a feature transformation
instead of an endpoint contract — and it's what makes `feature_definition` immutable-per-version
in the data model above, rather than a single row updated in place.

**Freshness and schema-drift monitoring.** Tracked per feature, not in aggregate: streaming lag
(the gap between an event's timestamp and when its effect lands in the online store) is compared
against that feature's own declared `freshness_sla`, and schema drift (the computed value's actual
type or distribution silently diverging from what the definition declares — e.g. a source field
that used to always be non-null starting to arrive null) is flagged independently. Both failure
modes are invisible to any downstream model's own quality metrics until predictions have already
degraded, which makes this pipeline-level monitoring the upstream complement to
ml-monitoring-and-drift.md's downstream, model-output-focused drift detection — a broken feature
pipeline and a genuine real-world distribution shift can look identical from a model's output
alone, and distinguishing the two requires exactly this kind of pipeline-side instrumentation
watching the input side, not just the output side.

**Feature catalog and discovery as a dedup mechanism.** At hundreds of definitions across many
teams, the real risk isn't running out of raw signal — it's redundant near-duplicate features
quietly proliferating (two teams independently defining "purchases in the last 30 days" with
slightly different edge-case handling), which doubles both compute cost and the
training-serving-skew surface area, since now there are two independently-compiled pipelines to
keep consistent instead of one. A searchable catalog with consumer tracking makes existing
features discoverable before a team writes a new one, and surfaces genuinely unused features as
deprecation candidates — as much a compute-cost control at platform scale as a hygiene practice.

## Bottlenecks & Scaling

Streaming infrastructure has to scale with raw event volume across every source feeding every
feature definition combined, independent of how many downstream models actually consume the
results — the same "producers don't know or care how many consumers exist" scaling shape as any
pub-sub ingestion layer (cross-ref concepts/message-queues.md). Backfill jobs are the component
most likely to become a shared-infrastructure bottleneck as feature count grows, since each new
feature potentially needs its own historical backfill competing for the same batch-processing
capacity as every other team's backfills and retraining jobs — the same scarce-shared-batch-
capacity pattern gpu-cluster-scheduler.md manages for GPU jobs, here applied to CPU-bound batch
compute, and worth quota-managing for the same reason. The compiler that generates both pipelines
from one definition is a single point of correctness leverage for the whole platform — a bug in
how it handles a particular transformation type propagates into every feature using that pattern —
which is why the DSL is deliberately kept smaller and more constrained than a general-purpose
scripting language, trading expressiveness for the guarantee that the two compiled paths really do
stay equivalent. Catalog and discovery search stays cheap indefinitely at this metadata scale and,
unlike everything else in this list, is never the bottleneck.
