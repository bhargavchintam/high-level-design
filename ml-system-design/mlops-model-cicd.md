# MLOps Model CI/CD Pipeline

## Requirements

### Functional
- Automatically build, evaluate, and progressively deploy a new model artifact — triggered by a
  training run completing or a code/data change kicking off retraining — through to full
  production traffic, without manual per-step intervention for the common case.
- Gate every promotion behind both an offline evaluation (holdout metrics vs. the current
  production champion) and an online evaluation (live-traffic shadow and/or canary comparison)
  before any candidate reaches 100% of production traffic — no artifact skips straight from
  "trained" to "fully live."
- Support automatic rollback: if a promoted model's online guardrail metrics regress past a
  configured threshold within the monitoring window, revert to the previous production version
  without waiting for a human to notice and page.
- Encode promotion criteria, evaluation datasets, and rollout policy as declarative, versioned
  per-model configuration, so what "safe to promote" means for a given model is reviewable config,
  not tribal knowledge in a runbook.

### Non-functional
- Time from "candidate artifact registered" to "fully promoted or rejected" should be bounded
  (hours, not days) for models that iterate frequently, without cutting corners on the evaluation
  rigor that makes the gates meaningful in the first place.
- False-promote rate (a genuinely worse model passes every gate and reaches full production
  traffic) must be low — this is a correctness property of how the gates are designed and sized,
  not just a property of the pipeline's operational reliability.
- Every promotion or rollback decision must be auditable: traceable to the exact metric values,
  thresholds, and config version active at decision time, for post-incident review and compliance.
- The pipeline must run safely, unattended, and continuously across hundreds of independently
  releasing models — the same control-plane scaling concern [model-serving-platform.md](model-serving-platform.md) raises
  about rollout velocity across many teams, but here it's the automated decision-making itself
  that has to scale, not just the traffic-shifting mechanism.

## Capacity Estimation

**Rollout volume.** Reusing [model-serving-platform.md](model-serving-platform.md)'s estimate of ~28 rollouts/day across 200
hosted models: each one is a full pipeline run through offline eval, shadow, canary, and (if all
gates pass) promotion.

**Offline eval compute cost.** Assume a holdout set of 2M labeled examples per gate. At a batch
(non-latency-constrained) scoring throughput of ~20,000 examples/sec/GPU for a moderate-size
ranking/classification model, one gate run takes 2,000,000 / 20,000 = 100 seconds of GPU time. At
28 gates/day: 28 × 100 sec ≈ 2,800 sec ≈ **47 minutes/day of aggregate GPU-time** — trivial next to
training compute, but this step sits on the critical path of every promotion, so its *wall-clock*
latency (queueing to get GPU time, not the 100 seconds of actual compute) is what the pipeline's
hours-not-days SLA actually has to budget for.

**Online canary concurrency.** At a 30-60 minute canary evaluation window (per
[model-serving-platform.md](model-serving-platform.md)) and ~28 rollouts/day spread across a work day, average concurrent
canaries ≈ 28 × 45min / 1,440min ≈ **0.9 concurrent canaries** on average — but release activity
clusters heavily (many teams shipping Monday morning after a weekend), so the orchestration layer
has to handle bursts of 10-20 simultaneous canaries, not assume the average holds at all times.

**Statistical power, worked example.** To detect a 1 percentage-point absolute regression in a
guardrail conversion metric with a baseline rate of 10%, at 80% power and 95% confidence, a
standard two-proportion sample-size calculation calls for roughly 15,000-17,000 samples *per arm*
(canary and baseline) — a back-of-envelope figure from the standard formula
n ≈ 16 × p(1-p) / MDE², here 16 × 0.1 × 0.9 / 0.01² ≈ 14,400, rounded up for a two-sided test. For
a model receiving 5% canary traffic out of, say, 200 req/sec total, that's 10 req/sec into the
canary arm → 14,400 / 10 ≈ **1,440 seconds ≈ 24 minutes** to reach the needed sample size — which
is why the canary window is a statistical requirement tied to the model's own traffic volume, not
an arbitrary fixed calendar duration applied uniformly to every model regardless of how much
traffic it actually receives.

## API Design

```
POST /v1/pipelines/{model_name}
     { eval_datasets: [...],
       promotion_thresholds: [{metric, comparison: vs_baseline | absolute, min_delta}],
       rollout_policy: {canary_pct, eval_window_min, guardrail_metrics} }
     -> 201   (registers/updates the declarative pipeline config; itself versioned)

POST /v1/pipelines/{model_name}/trigger
     { candidate_artifact_id }
     -> 202 { pipeline_run_id, status: "offline_eval" }

GET  /v1/pipelines/{model_name}/runs/{pipeline_run_id}
     -> { status: offline_eval | shadow | canary | promoting | promoted |
                   rejected | rolled_back,
          gate_results: [{gate_type, metric, candidate_value, baseline_value, passed}] }

POST /v1/pipelines/{model_name}/runs/{pipeline_run_id}/rollback
     -> 202   (manual override; the same action the pipeline triggers automatically
               on a guardrail-metric breach)
```

## Data Model

```
pipeline_config (declarative, versioned per model_name — a config change is itself
  a reviewable diff)
  model_name, version, eval_datasets, promotion_thresholds, rollout_policy

pipeline_run (one per promotion attempt)
  pipeline_run_id, model_name, candidate_artifact_id, baseline_artifact_id
  (both are references into ml-experiment-tracking-platform.md's model_registry),
  status, started_at

gate_result (one row per gate evaluated within a run)
  pipeline_run_id, gate_type: offline | shadow | online_canary,
  metric_name, candidate_value, baseline_value, passed: bool, evaluated_at

promotion_event (audit trail — every stage transition, automated or manual)
  pipeline_run_id, from_stage, to_stage,
  decided_by: automated | manual_override, ts
```

`promotion_event` feeds the exact same kind of audit trail [ml-experiment-tracking-platform.md](ml-experiment-tracking-platform.md)'s
`model_registry.stage` transitions need — this pipeline is, in effect, what *drives* those
transitions automatically instead of leaving them to a human clicking a stage-promotion button.

## High-Level Architecture

```
 candidate artifact registered
 (ml-experiment-tracking-platform.md's model_registry, stage=None)
                    │
                    ▼
        Pipeline trigger ──► Offline eval job: batch-score candidate + current
                              production baseline over the same holdout set
                                    │
                        gate pass (vs. baseline, not absolute)? ──no──►
                        reject, alert owner, artifact stays unpromoted
                                    │yes
                                    ▼
                        Shadow deploy (model-serving-platform.md's shadow
                        mechanism: candidate scores live traffic, output
                        never shown to users) ──► compare vs. baseline
                        output over a shadow window
                                    │
                        gate pass? ──no──► reject
                                    │yes
                                    ▼
                        Canary rollout (model-serving-platform.md's
                        traffic-policy canary: small % of real traffic)
                        ──► online gate: statistical comparison of
                        guardrail metrics, canary vs. baseline, sized per
                        the power calculation above
                                    │
                        regression past threshold? ──yes──► automatic
                        rollback (traffic weight -> 0, a routing-table
                        change, not a redeploy)
                                    │no
                                    ▼
                        gradual ramp to 100% ──► registry stage ->
                        Production; previous version -> Archived
```

## Deep Dives

**Offline eval gates as champion/challenger, not absolute thresholds.** Gating on an absolute
threshold ("accuracy > 0.9") loses meaning as the champion model improves over successive
iterations — once the current production model is already at 0.93, a 0.9 gate would pass a
regression. Gating the candidate against the *current production model's* score on the same
holdout set (comparison: `vs_baseline`) keeps the bar meaningful release over release. This still
doesn't catch everything: a holdout set reflects the data distribution at the time it was
collected, and offline gates by construction cannot observe a distribution shift between that
holdout set and live traffic — which is exactly why an online gate is still mandatory even after
a clean offline pass, and why [ml-monitoring-and-drift.md](ml-monitoring-and-drift.md)'s distribution-shift detection operates
as an independent, continuous check outside this pipeline entirely, not a substitute for it.

**Shadow-then-canary as sequential risk reduction.** Shadow deployment ([model-serving-platform.md](model-serving-platform.md))
catches crashes, latency regressions, and gross miscalibration with zero user-facing risk, since
its output is computed but never returned or acted on. It cannot, by the same design, observe how
real users actually respond to the new model's outputs — click-through, conversion, any behavioral
metric that only exists once a real decision was actually made using the model's output. Canary is
the necessary next, costlier step specifically because that class of signal has no shadow-mode
substitute; the two stages are sequential specifically to filter out the cheap-to-catch failure
modes (shadow) before paying the cost of exposing any real traffic to the candidate at all
(canary).

**Statistical rigor in online gates.** A canary evaluation is a live experiment, not a health
check, and treating it as anything less than a properly powered hypothesis test risks both
false-promotes (an underpowered canary that got lucky) and false-rejects (normal variance
mistaken for regression). The worked power calculation above is why the pipeline computes a
minimum sample size per gate from the guardrail metric's baseline rate and the minimum detectable
effect that matters for that model, rather than applying one fixed calendar-time window to every
model regardless of its traffic volume — cross-ref [ab-testing-and-experimentation.md](ab-testing-and-experimentation.md) for the
underlying statistical machinery (sample-size calculation, sequential testing considerations) this
gate reuses directly rather than reimplementing.

**Automated rollback and blast-radius containment.** Because [model-serving-platform.md](model-serving-platform.md)'s
traffic-policy design keeps the previous production version warm and actively serving until the
new version is fully promoted, "rollback" is a routing-table weight change, not a redeploy — it
completes in the time it takes that config change to propagate, not the time it takes to
provision hardware or reload weights. This pipeline's actual job is deciding *when* to trigger
that primitive: continuously evaluating guardrail metrics during the canary window against the
statistically-sized threshold, and firing the rollback the instant a breach is confirmed, rather
than implementing the rollback mechanism itself, which already exists one layer down.

**Declarative pipeline config and audit trail.** Encoding promotion criteria as versioned,
reviewable config rather than runbook steps means a threshold change is a diff someone can review
before it takes effect, and every automated decision the pipeline makes can be replayed later
against the exact config version and metric values that were active at that moment. This is what
makes an unattended pipeline running continuously across hundreds of models an auditable process
rather than an opaque one — directly extending the reproducibility guarantee
[ml-experiment-tracking-platform.md](ml-experiment-tracking-platform.md)'s lineage tracking already makes for how a model was *trained*,
to cover how it was subsequently *promoted*.

## Bottlenecks & Scaling

At hundreds of independently-releasing models, the orchestration layer tracking pipeline_run state
has to handle bursty concurrent canary evaluations (release clustering, e.g. Monday mornings) as a
real queueing and scheduling problem rather than assuming the low steady-state average concurrency
computed above always holds. Offline eval compute competes for the same shared GPU capacity as
everything else in the org (cross-ref [gpu-cluster-scheduler.md](gpu-cluster-scheduler.md)) and needs its own priority/quota
class, so a burst of promotion evaluations doesn't starve training jobs, or vice versa delay every
pending promotion behind a large training run. Statistical rigor imposes a hard, non-infrastructure
constraint: a low-traffic model cannot get a fast, confident online-gate decision no matter how
well the pipeline itself is engineered, since the sample-size requirement is a property of the
traffic volume and effect size, not of pipeline throughput — the system has to surface this as an
explicit "insufficient traffic for statistical confidence in this window" state rather than
silently promoting on an underpowered result just because the calendar-time window elapsed.
