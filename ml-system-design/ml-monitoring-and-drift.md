# ML Monitoring and Drift Detection

## Requirements

### Functional
- Continuously monitor every production model's input feature distributions and prediction
  outputs for statistically significant deviation from what the model was trained on (data
  drift) or from its recent historical baseline (a shift in the distribution itself).
- Monitor model performance against ground-truth labels wherever labels eventually become
  available (even delayed — e.g. "did the user actually churn 30 days later"), to detect concept
  drift — the relationship between features and outcome changing, not just the feature
  distribution shifting.
- Support shadow deployment (a candidate model scores live traffic without its output being
  used) as a pre-production validation step, feeding the same monitoring pipeline used for
  production models.
- Alert the owning team when drift or performance degradation crosses a configured threshold,
  with enough context (which features, how much, since when) to triage without starting from
  zero.

### Non-functional
- Detection latency matters and is feature/label-dependent: input-distribution drift should be
  detectable within hours of the underlying shift (it doesn't require waiting on labels);
  outcome-based performance degradation is bounded by label latency itself (immediate for some
  use cases, weeks for others like churn) and the monitoring system shouldn't pretend it can
  detect faster than labels actually arrive.
- False positive rate has to be kept low enough that alerts remain actionable — a monitoring
  system that fires constantly on noise gets its alerts ignored, which is a silent failure mode
  worse than not monitoring at all.
- Monitoring overhead (compute, storage for logged predictions/features) must scale sublinearly
  with the number of monitored models — a platform monitoring hundreds of models cannot afford
  per-model bespoke pipelines.
- Monitoring must not add meaningfully to serving-path latency — it observes production traffic,
  it does not sit in the critical path of a prediction being returned to the caller.

## Capacity Estimation

Assume the platform monitors 200 production models (the same fleet as
model-serving-platform.md), aggregate 100K predictions/sec across all of them.

Prediction logging: monitoring can't log every single prediction at this volume without its own
storage pipeline becoming a major cost center — a 1% sample of 100K/sec = 1,000/sec sampled
predictions logged with full feature vectors, which at ~2KB/logged prediction (features +
prediction + metadata) ≈ 2 MB/sec ≈ ~173 GB/day, ~63 TB/year — manageable at this sampling rate,
versus ~6.3 PB/year if logging every prediction unsampled, which is the concrete reason sampling
(rather than exhaustive logging) is the default.

Drift computation: for each of 200 models, computing a distributional distance metric (PSI,
KL-divergence, or a two-sample statistical test) between a recent window (e.g. last 24h of
sampled predictions) and a reference baseline (the training data distribution), per feature. At
an average of ~150 monitored features/model × 200 models = 30,000 feature-distribution
comparisons per computation cycle. Run hourly, that's 30,000 comparisons/hour ≈ 8.3/sec — cheap
in absolute compute terms; the actual engineering cost here is statistical rigor (getting
thresholds right) rather than raw throughput.

Label-join pipeline: for models with delayed ground truth (e.g. a churn model with a 30-day
outcome window), the monitoring pipeline has to join sampled predictions made 30 days ago against
labels arriving today — a rolling join across a 30-day window of logged predictions, scaled per
model by that model's own label latency. At 1,000 sampled predictions/sec platform-wide and a
30-day join window, the pipeline needs to hold roughly 1,000 × 86,400 × 30 ≈ 2.6B logged
prediction records in a queryable, joinable state at any given time for the subset of models with
long label latency — a meaningful but not extreme scale for a stream-processing/warehouse layer
already built for this pattern.

Shadow traffic overhead: a model in shadow mode processes a full copy of live traffic (not
sampled, since the whole point is pre-production validation against real production input
diversity) — for a candidate replacing one of the platform's higher-traffic models, this can
mean provisioning a full second inference fleet purely for shadow scoring, which is why shadow
deployment is usually time-boxed (days, not a permanent steady state) rather than run
indefinitely for every model version.

## API Design

```
POST /v1/monitoring/register-model
     { model_name, version_id, feature_schema, baseline_distribution_ref }
     -> 201

GET  /v1/monitoring/{model_name}/drift-report?window=24h
     -> 200 { features: [{name, drift_score, metric: "psi"|"kl_div", threshold,
              status: "ok"|"warning"|"breach"}], overall_status }

GET  /v1/monitoring/{model_name}/performance?since=...
     -> 200 { metric: "auc"|"rmse"|..., value, sample_size, label_coverage_pct }

POST /v1/monitoring/{model_name}/shadow
     { candidate_version_id, duration_hours }
     -> 202 { shadow_run_id }

POST /v1/monitoring/alerts/subscribe
     { model_name, metric, threshold, notify_channel }
     -> 201
```

`label_coverage_pct` in the performance response is deliberate — a performance metric computed
on 5% label coverage (most predictions still awaiting ground truth) needs to be visibly
lower-confidence than one computed on 95% coverage, and surfacing coverage alongside the metric
prevents a team from over-trusting a performance number that's actually based on a small,
possibly biased (early-arriving labels are not always representative) sample.

## Data Model / Feature Schema

```
prediction_samples (sampled, joined against labels when available)
  request_id, model_name, version_served, feature_vector, prediction,
  predicted_at, label (nullable until observed), label_observed_at

baseline_distributions (per model version, snapshot of training-time
feature distributions)
  model_name, version_id, feature_name, distribution_summary
  (histogram bins or quantile sketch, not raw training data — see deep dive)

drift_reports (computed periodically, one per model per window)
  model_name, version_id, window_start, window_end,
  feature_name, drift_score, metric_type, status

performance_metrics (computed as labels arrive)
  model_name, version_id, window, metric_name, value,
  sample_size, label_coverage_pct

shadow_runs (tracks in-flight shadow evaluations)
  shadow_run_id, model_name, baseline_version_id, candidate_version_id,
  start_ts, duration_hours, comparison_summary
```

`baseline_distributions` stores a compact summary (histogram or quantile sketch) rather than raw
training data specifically because drift computation needs to run cheaply and repeatedly against
this baseline for every monitored window — recomputing a distribution summary from raw training
data on every drift check would be needlessly expensive when the baseline itself doesn't change
between a model version's deployments.

## High-Level Architecture

```
 monitoring data path:
 Model-serving fleet (from model-serving-platform.md) ──sampled predictions──►
                                                          Monitoring ingestion
                                                          (async, off request path)
                                                                    │
                                              ┌─────────────────────┴─────────────────┐
                                              ▼                                       ▼
                                    prediction_samples store              feature distribution
                                    (feature vectors + predictions,        aggregator (rolling
                                    joined with labels as they arrive)     windows, hourly)
                                              │                                       │
                                              ▼                                       ▼
                                    Label-join pipeline                    Drift scorer (compares
                                    (matches delayed ground-truth           rolling window against
                                    events back to the original             baseline_distributions,
                                    prediction by request_id)                per feature)
                                              │                                       │
                                              ▼                                       ▼
                                    performance_metrics                    drift_reports
                                              │                                       │
                                              └─────────────────┬─────────────────────┘
                                                                 ▼
                                                       Alerting engine (thresholds,
                                                       dedup, routes to owning team)

 shadow deployment path:
 live request ──► production model (response returned to caller)
      │
      └─(mirrored copy)─► shadow model (candidate version, response
                            discarded, logged for comparison against
                            the production model's actual output)
                                      │
                                      ▼
                          comparison job: prediction agreement rate,
                          latency delta, error rate — surfaced before
                          the candidate is trusted for canary traffic
                          (see model-serving-platform.md)
```

## Deep Dives

**Data drift vs. concept drift.** These are frequently conflated but require different
detection and different responses. Data drift is a shift in the input feature distribution
itself — e.g. a new user acquisition channel changes the mix of device types seen in traffic —
detectable purely by comparing feature distributions over time, with no need for labels at all,
which is why it's the faster-to-detect signal in the architecture above. Concept drift is a
shift in the actual relationship between features and the target — the same feature values now
correlate with a different outcome than they did at training time (e.g. a fraud model's features
stay similarly distributed, but fraudsters change tactics, so the same feature pattern now means
something different) — this is only detectable against ground-truth labels, and is therefore
gated by label latency. A model can suffer concept drift with zero detectable data drift, which
is precisely why performance monitoring against delayed labels is a separate pipeline from
feature-distribution monitoring, not a redundant one.

**Drift metrics: PSI and KL-divergence.** Population Stability Index (PSI) buckets a feature's
values into bins (from the baseline distribution) and measures how much the proportion of
observations in each bin has shifted in the current window — a simple, interpretable metric
with widely-used rule-of-thumb thresholds (PSI < 0.1 stable, 0.1-0.25 moderate shift worth
watching, > 0.25 significant shift). KL-divergence gives a more information-theoretic measure of
distributional difference but is less immediately interpretable to a non-specialist triaging an
alert, and is asymmetric (KL(P||Q) != KL(Q||P)), which matters when deciding which distribution
is the reference. In practice, PSI (or a symmetric variant like Jensen-Shannon divergence) is
more common for operational alerting specifically because on-call engineers need to reason about
the number quickly, while KL-divergence sees more use in research/offline analysis where that
interpretability tradeoff matters less.

**Threshold tuning and alert fatigue.** A drift score against a fixed threshold, checked hourly
across 30,000 feature-model combinations, will produce false-positive alerts purely from
statistical noise if thresholds are set naively — some features naturally have higher baseline
variance than others, and a single global threshold either misses real drift in low-variance
features or spams alerts on high-variance ones. Practical systems tune thresholds per feature
(informed by that feature's own historical volatility, not a platform-wide constant), require
sustained deviation across multiple consecutive windows before alerting (filtering single-window
noise), and deduplicate/group related alerts (ten correlated features all drifting together from
one underlying cause should page the on-call once, with all ten listed, not ten separate pages).
The cost of getting this wrong in either direction is real: too sensitive, and alerts get
ignored; too conservative, and a real degradation goes unnoticed until it shows up as a business
metric regression days or weeks later.

**Label latency and its effect on monitoring design.** A model whose outcome label is available
within seconds (e.g. "was this transaction actually fraudulent," confirmed near-instantly by a
downstream check) can have near-real-time performance monitoring. A model whose label takes
weeks (e.g. 30-day churn) fundamentally cannot detect a performance regression faster than that
label latency permits, no matter how sophisticated the monitoring pipeline is — the best the
system can do for long-label-latency models is lean more heavily on data-drift and
prediction-distribution monitoring (which need no labels) as an early-warning proxy, while
accepting that true performance confirmation lags by the label window. This is a structural
constraint the monitoring system's SLAs have to reflect honestly per model, rather than
promising a uniform detection latency across a fleet of models with very different label
latencies.

**Shadow deployment as pre-production monitoring.** Shadow deployment (introduced in
model-serving-platform.md as a rollout-safety mechanism) feeds the same monitoring
infrastructure described here — the candidate model's shadow predictions are compared against
the production model's actual predictions on identical live inputs, surfacing prediction
agreement rate, latency delta, and error rate before the candidate takes any real traffic. This
catches a class of problem that offline evaluation on a static test set often misses: behavior on
the actual, current, possibly-already-drifted production input distribution, including edge
cases and input patterns that didn't exist (or weren't yet common) when the offline eval set was
assembled. The tradeoff is cost (a full duplicate inference fleet for the shadow model, per the
capacity estimate above) and the fact that shadow mode alone can't validate outcome quality for
long-label-latency use cases within a short shadow window — it validates behavioral consistency
and operational health, not ground-truth accuracy, for anything where labels take longer to
arrive than the shadow period runs.

## Bottlenecks & Scaling

Prediction sampling rate is the main lever trading monitoring fidelity against storage/compute
cost, and different models may reasonably warrant different sampling rates (a high-stakes model
might justify a higher sample rate despite the cost, a low-stakes high-volume model might sample
much lower) rather than one platform-wide constant. The label-join pipeline is the component most
sensitive to the platform's worst-case label latency across its model fleet — it has to keep a
rolling window of logged predictions open for as long as the slowest-labeled model needs,
which sets the pipeline's storage/state retention requirement independent of prediction volume
itself. Drift computation scales with feature count × model count, which grows roughly linearly
with platform adoption — sublinear-cost techniques (quantile sketches instead of full
distributions, incremental rather than full-window recomputation) matter more as the platform
crosses into the thousands of monitored models rather than the hundreds assumed here. Finally,
alerting quality (low false-positive rate while still catching real degradations) is the
component most resistant to pure infrastructure scaling — it's fundamentally a statistics
tuning problem per feature/model, and is usually the long-term maintenance burden of a monitoring
platform long after the pipeline architecture itself has stabilized.
