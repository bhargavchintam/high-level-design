# ML Experiment Tracking Platform

## Requirements

### Functional
- Log training runs: hyperparameters, metrics over time (a scalar per step/timestamp, e.g.
  loss and accuracy every N steps), code version (git commit hash), and environment
  (dependency lockfile hash, container image digest).
- Track full lineage from input to output: dataset version -> feature version -> training run
  -> model artifact, so any model in the registry can be traced back to exactly what produced
  it.
- Maintain a model registry with staged versions (`None -> Staging -> Production -> Archived`)
  per model name, decoupled from the run that produced the underlying artifact — registration
  and promotion are separate actions from logging.
- Let downstream serving and deployment systems consume the registry as the single source of
  truth for "what is the currently-approved production artifact for model X" (see
  ml-system-design/real-time-ml-pipeline.md and concepts/model-serving-patterns.md for the
  consuming side).
- Support comparing runs side by side (metrics, params, diffs) and reproducing a specific past
  run exactly, months later, on demand.

### Non-functional
- Training-loop instrumentation must be effectively non-blocking — a GPU-bound job burning
  $30-100/hr in accelerator time cannot stall its training step on a synchronous network call
  to log one metric value, every step, potentially thousands of times per run.
- The ingestion path must absorb very high-frequency, high-cardinality metric logging across
  many concurrent jobs without becoming the bottleneck on any individual job's wall-clock time.
- Artifact storage must handle large binary objects (multi-GB model checkpoints) cheaply and
  reliably at scale — a blob-storage problem, not a database problem — and needs resumable
  upload, since a dropped connection on a 20 GB checkpoint upload cannot mean starting over.
- Metadata queries across millions of historical runs (search/filter by hyperparameter value,
  by metric threshold, by tag, by owner) must stay fast — this is the surface most engineers
  interact with daily, and a slow run browser directly taxes org-wide iteration speed.
- Reproducibility is a hard correctness requirement for runs promoted to production, not a
  nice-to-have — an unreproducible production model is an audit and incident-response
  liability, not just an inconvenience.

## Capacity Estimation

Assume a mid-to-large ML org: 2,000 concurrent training jobs org-wide at peak, each logging a
metric point roughly every 2 seconds (logging every optimizer step at typical 5-20 steps/sec
rates would be far higher volume, so most loops log every N steps or on a wall-clock timer).

**Metric-write throughput.** 2,000 jobs × (1 point / 2 sec) ≈ 1,000 metric-writes/sec average.
Each logging call typically submits a batch of related scalars (loss, learning rate, gradient
norm, eval metrics) rather than one value — at ~15 scalars/point → 1,000 × 15 = 15,000
metric-values/sec steady state, bursting 3-5x when a large cohort of jobs hits an eval
checkpoint simultaneously (a real correlated-burst pattern, since eval intervals are often
round step numbers shared across similarly-configured jobs). This write shape — high
frequency, append-only, monotonically increasing key per series — is a time-series workload,
not a transactional one.

**Params, checkpoints, retained storage.** Params log once per run, not per step — at ~50
params/run and ~500 new runs/day, that's 25,000 param writes/day, trivial next to the metrics
path. Checkpoints average 2 GB and are written every 30 min for the ~300 of 2,000 concurrent
jobs that are long-running (the rest checkpoint once, at the end): 300 × (1 / 1,800 sec) × 2 GB
≈ 333 MB/sec sustained object-storage write throughput at peak — the number that sizes upload
bandwidth and, combined with dedup (deep dive below), drives the storage bill. At 500 runs/day
× 3 GB average final checkpoint × 730 days (2-year retention) ≈ 1.1 PB raw retained — dedup
reduces this meaningfully in practice, but 1.1 PB raw is what forces cold-tiering of artifacts
belonging to old, non-Production runs rather than keeping everything on hot storage (the same
lifecycle tradeoff as case-studies/google-drive-dropbox.md's cold-tiering, applied to
checkpoints instead of user files).

**Metadata query load.** ~365,000 runs over 2 years, each with ~50 params and ~30 summary
metrics indexed for filtering, is ~30M indexed (run, key, value) rows — comfortably sized for a
relational store with the right indexes (concepts/indexing.md), and explicitly not a workload
to serve by scanning the metrics time-series or artifact blob store directly.

## API Design

```
POST /v1/runs
     { experiment_id, run_name, git_commit, env_hash, tags }
     -> 201 { run_id }

POST /v1/runs/{run_id}/params
     { params: { learning_rate: 0.001, batch_size: 256, ... } }
     -> 200   (write-once-ish; logged at or near run start)

POST /v1/runs/{run_id}/metrics
     { metrics: [{ key: "loss", value: 0.42, step: 1500, timestamp }, ...] }
     -> 202   (batched, async-friendly — client SDK buffers and flushes
               these, never calls this once per logged value)

POST /v1/runs/{run_id}/artifacts
     { path: "checkpoints/step_1500.pt", content_hash }
     -> 200 { upload_url }        (resumable/multipart upload target, not an inline body)

POST /v1/registry/{model_name}/versions
     { run_id, artifact_path }
     -> 201 { version, stage: "None" }

PATCH /v1/registry/{model_name}/versions/{version}
     { stage: "Staging" | "Production" | "Archived" }
     -> 200   (a stage transition — the action that triggers downstream
               deployment pipelines, see architecture below)

GET /v1/runs?filter=params.learning_rate<0.01 AND metrics.eval_accuracy>0.9
     -> 200 [ { run_id, params, summary_metrics, tags }, ... ]
```

The metrics endpoint being batched and returning `202` rather than a per-value synchronous
`200` mirrors the async job-based pattern in ml-system-design/feature-store.md's offline
endpoint, for the same reason: the caller (a training loop) cannot block on a round trip per
logged value.

## Data Model

```
runs (relational — queried by arbitrary filters, moderate volume)
  run_id, experiment_id, git_commit, env_hash, dataset_version, feature_version,
  status, started_at, ended_at, owner

params (relational, low volume, write-once per run)
  run_id, key, value

metrics_summary (relational — last/best value per key, denormalized for fast filtering)
  run_id, key, last_value, best_value, step_at_best

metrics_timeseries (append-only, high write volume — separate store from the above)
  run_id, key, step, value, timestamp
  # every logged point, never updated in place, keyed by (run_id, key, step) —
  # a range scan over step/timestamp for one (run_id, key) is the "loss curve" query,
  # structurally the same access pattern as one partition's log in
  # case-studies/distributed-message-queue.md, or one entity's history in
  # ml-system-design/feature-store.md's offline store

artifacts (metadata pointer table — the actual bytes live in object storage)
  artifact_id, run_id, path, content_hash (SHA-256), size_bytes, storage_uri

model_registry (relational, small — this table is what serving reads)
  model_name, version, run_id, artifact_id, stage, transitioned_at, transitioned_by

lineage_edges (graph, small relative to metrics, read on every "how was this model made" query)
  (dataset_version) -> (run_id) -> (model_version)
```

`metrics_timeseries` is a deliberately separate store from the rest, for the same reason the
online/offline feature stores are split in ml-system-design/real-time-ml-pipeline.md: metrics
are append-only and read via range scan, while runs/params/registry are read via arbitrary
filter predicates — no single storage engine is good at both.

## High-Level Architecture

```
 TRAINING PATH (client SDK embedded in the training loop)

 training loop (GPU-bound) ──log_metric()/log_param()──► SDK in-process buffer
        │                                                        │
        │ (step proceeds immediately — SDK call returns          │ background thread
        │  after an in-memory enqueue, not a network              │ batches + flushes
        │  round trip)                                            │ every few seconds
        ▼                                                        ▼
 next training step                                    Tracking API (ingest)
                                                                   │
                                    ┌───────────────────────────────┼─────────────────────┐
                                    ▼                               ▼                      ▼
                           runs/params store              metrics_timeseries      artifact upload
                           (relational)                    (append-only,           (checkpoint ->
                                                             write-optimized)        object store,
                                                                                      content-addressed,
                                                                                      resumable)
                                    └───────────────────────────────┬──────────────────────┘
                                                                     ▼
                                                    model_registry (register new version,
                                                                     initially stage=None)

 SERVING PATH (registry consumed by deployment/serving systems)

 ML engineer/CI ──PATCH stage=Staging──► model_registry ──eval gate/approval──►
 ML engineer ──PATCH stage=Production──► model_registry ──► triggers deployment pipeline
                                                              (the transition IS the trigger)
                                                                     │
                                                                     ▼
                                          deployment/serving system (concepts/model-serving-
                                          patterns.md) pulls the registered artifact by version
                                          from object storage, loads it into the serving fleet;
                                          prior Production version demoted to Archived
```

Training and serving share only the registry and the artifact store — a training job never
talks to a serving system directly, and vice versa. The registry's stage field is the entire
coupling surface, which is deliberate: it lets the two sides evolve independently as long as
they agree on what a stage transition means.

## Deep Dives

**Async, non-blocking instrumentation.** `log_metric()` must return in microseconds — it
enqueues into an in-process buffer (bounded ring buffer/queue) and returns immediately; a
background thread drains it on a timer or size threshold, batching many points into one
network call — the same tradeoff as a StatsD/Prometheus client batching pushes rather than
opening a connection per counter increment. On buffer overflow (flush falls behind a fast
training loop), the SDK should drop the oldest or sample rather than block the training step —
a lost loss-curve point is a chart annoyance, not a correctness problem, so best-effort
delivery is the right default for metrics. On flush failure (tracking API unreachable), the
SDK should spool to local disk and retry rather than silently discarding a run's metric
history — losing an hour of logging on a multi-day job is a real cost. This is a strictly
different reliability bar than artifact uploads: a dropped metric point is acceptable loss, a
dropped checkpoint is not, since it may be the only copy of hours of compute — so artifacts get
a durable, resumable, acknowledged path while metrics get a best-effort, fire-and-forget one.
Conflating the two tiers in one code path is the most common design mistake in homegrown
instrumentation.

**Metrics time-series storage at high write volume.** `metrics_timeseries` is written far more
than read, and every write is an append at a monotonically increasing key within a (run_id,
key) series — never an update in place. This is the log-structured write pattern from
concepts/indexing.md's LSM-tree section, applied to a broker's partition log in
case-studies/distributed-message-queue.md: sequential appends are cheap, and the dominant read
("the loss curve for run X") is a contiguous range scan over one series, not a point lookup or
join — exactly what a write-optimized, range-scan-friendly store is built for. Serving this out
of the same relational store used for `runs`/`params` would mean either billions of narrow rows
degrading indexed lookups the metadata tables depend on, or constant compaction pressure from
an access pattern the store wasn't designed for — hence a separate store, not just a separate
table.

**Artifact content-addressing and deduplication.** Every artifact is hashed (SHA-256 over its
contents) and that hash becomes its storage key — re-uploading a checkpoint with unchanged
bytes is a cheap no-op, since the client checks whether the hash already exists and skips the
transfer if so. This matters because checkpoints are routinely re-saved with only a fraction of
weights changed, or a fine-tune re-persists a base checkpoint byte-for-byte. It's the identical
mechanism to the content-addressed block storage in case-studies/google-drive-dropbox.md,
applied to checkpoints instead of documents — same hash-as-key, same reason content-defined
chunking (rather than hashing the whole multi-GB file as one unit) improves dedup ratio when
only part of a large file changes. Multi-GB files also need multipart/resumable upload as a
first-class API — a single HTTP PUT for a 20 GB checkpoint over a flaky training-cluster
network is a reliability liability; parts that retry independently, reassembled and verified
against the final content hash server-side, is what makes checkpoint upload survivable.

**Lineage and reproducibility.** Reproducing a run exactly requires capturing, at logging time,
everything it depended on: code (git commit hash — dirty/uncommitted changes should be blocked
or auto-committed to a shadow branch, since a hash for code that was actually different at run
time is worse than no hash), environment (a container image digest or fully pinned lockfile,
not a loose-range requirements.txt), and dataset version (an immutable snapshot identifier, not
"latest as of some date"). The lineage graph (`dataset_version -> run_id -> model_version`) is
what makes "what produced this Production model" answerable months later without Slack
archaeology. In practice this is the hardest part to get right — not because the tracking
platform's own schema is hard, but because reproducibility requires every upstream system (the
feature store, the data pipeline, the base image registry) to also be versioned and immutable;
the platform can faithfully record a dataset version identifier, but if the warehouse allows
that "version" to be mutated in place afterward, the recorded lineage points at something that
no longer exists as it did at training time. Reproducibility is an org-wide versioning
discipline this platform depends on and enforces at its own boundary, not something it
guarantees unilaterally.

## Bottlenecks & Scaling

The metrics-ingestion path is the highest-QPS, most write-heavy component by a wide margin —
thousands of concurrent jobs each flushing batched points every few seconds is structurally the
same shape as the location-ping ingestion problem in case-studies/uber.md (many independent
high-frequency producers, low value per individual write, aggregate volume is the challenge),
and it scales the same way: horizontal sharding of the time-series store by run_id (keeping one
run's series co-located for efficient range scans) and aggressive client-side batching before
the network, since the SDK-side buffering above is as much a server-load mitigation as a
training-loop latency one. Artifact storage cost, not the metadata store, is the dominant line
item at scale — metadata is small and cheap even at tens of millions of rows, while checkpoint
storage grows with model size, checkpoint frequency, and retention window simultaneously, which
is why content-addressed dedup and cold-storage tiering for non-Production, non-recent
artifacts are the primary cost levers, not database tuning. Metadata query performance across
millions of historical runs requires the indexed relational store from the data model rather
than scanning the artifact blob store or metrics time-series store directly — a query like
"learning_rate < 0.01 AND eval_accuracy > 0.9" is exactly what a relational store with the
right composite indexes (concepts/indexing.md) is built for and a blob or LSM-style
time-series store is not; keeping this query surface on its own right-sized store is what keeps
the run browser fast as total run count grows into the millions.
