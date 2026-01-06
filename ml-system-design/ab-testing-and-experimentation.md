# A/B Testing and Experimentation Platform

## Requirements

### Functional
- Deterministically assign a user to a variant (control/treatment, or one of N arms) for any
  experiment they're eligible for, based on configured allocation and targeting rules.
- Collect metric events (clicks, conversions, session length, revenue, crashes) tagged with
  which experiment/variant was active, and pipeline them into an analysis system.
- Compute statistical significance and guardrail-metric health continuously as an experiment
  accumulates data, not just at a single fixed cutoff.
- Support gradual ML model rollout — shadow mode, then a small canary, then a ramp to full
  traffic — as a specialization of the same assignment/monitoring infra used for product
  experiments, not a separate system.

### Non-functional
- Assignment must be deterministic and sticky: the same user must land in the same variant for
  a given experiment across sessions — an experiment where users flip variants mid-session
  invalidates every metric it produces.
- Assignment latency must be near-zero — most calling code checks a flag on every render, so
  assignment is usually resolved client-side or at the edge from a cached config, not via a
  network round trip per check.
- The platform must support thousands of concurrent, overlapping experiments across the
  product without them silently interacting or stepping on each other's traffic.
- The platform must guard against the peeking problem: teams watch dashboards continuously and
  are tempted to stop as soon as p < 0.05 flashes, which inflates the true false-positive rate
  far above the nominal 5% if not corrected for statistically.

## Capacity Estimation

Assume 100M DAU. Each client fetches an experiment config bundle ~3 times/day (app open /
session start) rather than calling out per-flag-check: 100M × 3 = 300M fetches/day → 300M /
86,400 ≈ 3,470/sec average, ~10,000/sec peak (3x diurnal factor) — the only network-bound
"assignment" traffic. Actual flag evaluation (hash + allocation check) then happens locally
against the cached config: 100M users × ~10 feature/page loads/day × ~5 experiments/load ≈ 5B
local evaluations/day, which is why assignment has to be a stateless pure function (see deep
dive) rather than a lookup-service call — a round trip per check at this volume is a non-starter.

Metric events: assume ~50 loggable events/user/day (impressions, clicks, conversions) tagged
with active experiment context → 100M × 50 = 5B events/day → 5B / 86,400 ≈ 58,000/sec average,
~170,000/sec peak. This is the dominant load on the platform and sizes the ingestion pipeline
(see case-studies/distributed-message-queue.md for the general shape of this problem).

Assignment log: a write happens once per (user, experiment) pair, not per request. Assume ~20
concurrently running experiments a given user is enrolled in at once, with ~5% daily churn as
experiments launch/conclude → 100M × 20 × 5% ≈ 100M new/changed rows/day at ~100 bytes ≈ 10
GB/day — small next to the metrics stream, sharded (see concepts/database-sharding.md) by
user_id.

Raw event storage: 5B events/day × ~200 bytes ≈ 1 TB/day before aggregation. Aggregated
per-experiment/per-variant/per-metric/per-day rollups shrink this by orders of magnitude — 3,000
concurrent experiments × 15 metrics × ~3 variants ≈ 135,000 aggregate rows recomputed per cycle,
trivial next to the raw stream.

## API Design

```
GET  /v1/assignments?user_id=...&surface=home_feed
     -> 200 { assignments: [{experiment_id, variant, in_experiment}], config_version }
     # bulk fetch of every active experiment relevant to a surface; cached client-side
     # (see concepts/caching.md), not called per-flag-check

POST /v1/events        { user_id, event_type, metric_value, ts, active_experiments }
     -> 202  (fire-and-forget into the metrics ingestion pipeline)

GET  /v1/experiments/{id}/results
     -> { variant_stats: [...], confidence_sequence, guardrails: [...], srm_check }

POST /v1/experiments/{id}/rollout   { stage: shadow|canary|ramp|full, traffic_pct }
     -> ML model rollout; internally just an experiment_config update, same code path
```

`/v1/assignments` is a bulk-per-surface fetch rather than a per-experiment lookup — a single
page might evaluate a dozen flags, and a dozen network calls would blow any reasonable latency
budget. The SDK caches the response and resolves individual flag checks locally against it.

## Data Model

```
experiment_config
  experiment_id (PK)
  name, status: draft|running|paused|completed
  variants: [{name, allocation_pct}]        # sums to <=100; remainder is "not in experiment"
  targeting_rules: {locale, platform, ...}  # optional filters evaluated before hashing
  layer_id                                   # mutual-exclusion group, see deep dive
  guardrail_metrics: [metric_name, ...]
  start_ts, owner

assignment_log (sharded, keyed by user_id + experiment_id)
  (user_id, experiment_id) -> variant, assigned_at
  # write-once per pair; read only on the rare cache-miss path

metrics_events (append-only stream, e.g. Kafka topic)
  user_id, event_type, metric_value, ts, experiment_context: [(experiment_id, variant), ...]

aggregated_results (per experiment x variant x metric x day)
  experiment_id, variant, metric_name, date, n, sum, sum_sq, guardrail_breach: bool

model_rollout_config (extends experiment_config for ML rollout)
  model_id, stage: shadow|canary|ramp|full, traffic_pct, auto_rollback_enabled, model_version
```

`experiment_context` is attached to events at logging time — the client already knows its
variant from the cached assignment bundle — rather than joined downstream, avoiding a costly
join between a raw event stream and a separately-stored assignment table at query time.
`model_rollout_config.model_version` ties back to the model registry that tracks training runs
(see ml-system-design/ml-experiment-tracking-platform.md); this platform only owns which version
is live and at what traffic percentage, not how it was trained.

## High-Level Architecture

```
 assignment / serving path:
 client SDK ──fetch config (few times/day)──► Config Service ──► experiment_config store
       │
       ├─ locally: hash(user_id, experiment_id) -> bucket -> variant
       │  (deterministic, no network call per check — see deep dive)
       │
       └─ resolved variant used to render UI / gate feature ──► logged into event payload

 metrics pipeline path:
 client/server ──log event (w/ experiment_context)──► Ingestion (Kafka-style topic,
                                                         ~170K events/sec peak)
                                                              │
                                          Stream aggregator (per experiment x variant x
                                          metric rollups) ──► aggregated_results
                                                              │
                                ┌─────────────────────────────┼──────────────────────────┐
                                ▼                              ▼                          ▼
                         Stats engine                  Guardrail monitor           SRM checker
                    (mSPRT/sequential sig., CIs)   (latency/crash/revenue,      (observed vs.
                                │                    every exp., every cycle)   configured split)
                                └──────────────────────────────┼──────────────────────────┘
                                                                ▼
                                        Dashboard / auto-rollback trigger (writes back into
                                        experiment_config: pause / reduce traffic_pct)

 ML model rollout (same infra, different config shape):
 new model ──shadow: scored on 100% traffic, output logged, NOT used to serve──►
             (validates end-to-end before any user impact)
           ──canary: model_rollout_config.stage=canary, traffic_pct=1-5%, assignment via
             the same hash-bucketing mechanism──► guardrail monitor watches the same
             latency/error/business metrics──►
           ──ramp: traffic_pct increased in steps, each gated on guardrails clean──►
           ──full: old model deprecated; a breach at any stage triggers the same
                    auto-rollback path used for product experiments
```

## Deep Dives

**Deterministic hash-based assignment.** Assignment has to be a pure function of
`(user_id, experiment_id)` — typically `hash(user_id + experiment_id) mod 100`, compared against
the experiment's allocation percentage to pick a variant — rather than a stateful lookup table
populated at first-encounter time. A pure function needs no coordination: it produces the same
result on every device and in every datacenter, computed locally from a cached config bundle,
with no dependency on a central service being reachable or consistent. This is the same
underlying idea as consistent hashing (see concepts/consistent-hashing.md) — a deterministic
hash space partitioned into ranges, with a key mapped into it — except the "nodes" being mapped
to are variants instead of servers, and the goal is stable assignment rather than balanced load.
Salting with experiment_id (not just user_id) matters: it keeps a user's bucket independent
across experiments, so being in treatment for experiment A doesn't correlate with their bucket
in unrelated experiment B.

**Sample ratio mismatch (SRM).** If an experiment is configured as a 50/50 split, the observed
split in logged data should be statistically indistinguishable from 50/50 — a chi-squared
goodness-of-fit check run continuously. A significant SRM (say a 49.5/50.5 split that fails the
check at high confidence, because n is large enough that even a tiny deviation is detectable)
almost always indicates a bug in the assignment or logging path, not a real effect — a
bot-filtering rule that disproportionately excludes one variant, a client crash correlated with
one code path failing to log its event, or stale-cache reads that skew one arm's assignment.
SRM invalidates every downstream metric, since it means the two populations being compared
aren't actually random samples of the same group, so it's checked automatically and gates the
results dashboard before any significance number is shown.

**Sequential testing and the peeking problem.** A classical fixed-horizon t-test computes a
p-value assuming the sample size was fixed in advance and the test is run exactly once, at the
end. Checking it repeatedly as data accumulates and stopping the first time it crosses 0.05 is a
different procedure statistically — an implicit multiple-comparisons problem, and the true
false-positive rate under continuous peeking can run several times higher than the nominal 5%.
Always-valid sequential methods (mixture sequential probability ratio tests / mSPRT, or
group-sequential designs with pre-specified alpha-spending across a fixed number of looks)
construct a confidence sequence that stays valid no matter when or how often it's checked —
which is what actually lets a dashboard be trusted the moment it turns green, rather than only
at a single pre-committed end date.

**Guardrail metrics and automatic rollback.** Independent of what a given experiment is
optimizing for, a fixed set of guardrail metrics (page-load latency, crash rate, revenue,
unsubscribe rate) is checked on every experiment running on the platform. A guardrail breach —
detected with the same sequential-testing machinery so it isn't itself subject to peeking bias —
triggers an automatic reduction in traffic allocation or a full pause, without waiting for a
human to notice a dashboard. This is what makes ML model rollout safe to automate end-to-end:
the rollback trigger is generic guardrail-breach detection, not model-specific logic.

**ML model rollout as a specialization.** Shadow mode runs the new model against live traffic
and logs its output without ever using it to serve a response — validating the model end-to-end
(feature availability, latency, output distribution) against production traffic patterns offline
evaluation can't fully replicate, at zero user-facing risk. Canary then exposes a small
percentage of real traffic via the identical hash-bucketing mechanism used for product
experiments, so guardrail metrics can be observed under real exposure at low blast radius. Ramp
increases that percentage in steps, each gated on guardrails staying clean, until the model
reaches full traffic. None of these stages need bespoke infrastructure — they're a config on
top of the same experiment_config / assignment / guardrail-monitor path used for a copy-change
test, which is why gradual rollout stays cheap enough to be the default way any model reaches
production rather than a special-cased, manually-run process (see also
concepts/model-serving-patterns.md for the serving-side mechanics of shadow/canary traffic
through a model server).

## Bottlenecks & Scaling

The metrics pipeline, not assignment, is the real scaling challenge: assignment is
embarrassingly parallel and mostly resolved client-side, but every one of the ~170K events/sec
at peak has to be joined against which experiments and variants were active for that user at
that moment, aggregated per experiment/variant/metric, and made available to a stats engine with
low enough latency that guardrail breaches are caught in minutes, not hours. This is the same
general stream-aggregation shape covered in case-studies/distributed-message-queue.md and
ml-system-design/real-time-ml-pipeline.md — partition the event stream by user_id (so a single
user's events land on one partition, avoiding a cross-partition join), maintain rolling
aggregates in the stream processor rather than re-scanning raw events per query, and treat
`aggregated_results` as the only thing dashboards and the stats engine ever read.

The second scaling concern is interaction effects between overlapping experiments: with
thousands running concurrently, two experiments that both modify, say, the checkout button can
produce a combined effect that neither one's isolated analysis predicts, and can bias each
other's measured lift. Large platforms solve this with layers (sometimes called domains) —
mutually exclusive partitions of traffic per surface, where any two experiments in the same
layer can never see overlapping users, while experiments in different layers are allowed to
overlap because they're assumed, by the surface owner's design, not to interact. The `layer_id`
field on `experiment_config` enforces this at assignment time: a user's bucket for one layer's
hash space doesn't affect their bucket in another.
