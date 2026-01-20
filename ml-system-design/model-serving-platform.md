# Multi-Model Serving Platform

## Requirements

### Functional
- Host many independently-owned models (hundreds, from many teams) behind a shared serving
  infrastructure, rather than each team operating its own serving fleet.
- Support multiple concurrent versions of the same model (a shadow/canary version alongside the
  current production version) and route traffic between them by configurable policy.
- Support online A/B testing at the model level — a defined percentage of traffic sees model
  version B, the rest sees A, with outcome metrics attributable back to the version that served
  the request.
- Support both real-time (single-request, latency-sensitive) and batch (large offline scoring
  job) inference through the same model artifacts, without requiring two separate deployment
  packages per model.

### Non-functional
- Per-model p99 serving latency must be isolable — one team's misbehaving model (memory leak,
  runaway latency) must not degrade another team's model sharing the same physical fleet.
- New model version rollout must support safe, gradual traffic shift (canary) with automatic
  rollback on regression, rather than an all-or-nothing deploy.
- GPU/accelerator utilization across the shared fleet is a primary cost metric — the platform
  exists specifically to avoid every team provisioning (and under-utilizing) its own dedicated
  hardware.
- Model rollout (new version live and serving traffic) should complete in minutes, not hours —
  slow rollout directly slows down every team's iteration speed, which is the platform's main
  value proposition to its internal customers.

## Capacity Estimation

Assume the platform hosts 200 distinct models across teams, aggregate real-time serving load of
100K requests/sec across all of them, heavily skewed (a small number of high-traffic models
dominate — realistically, the top 20 models account for ~80% of request volume, a long tail of
180 models share the remaining 20%).

Compute footprint: assume an average model requires ~0.5 GPU-equivalent of dedicated capacity to
meet its own latency SLA in isolation (200 models × 0.5 = 100 GPU-equivalents if each got
dedicated hardware) — but with request-level multiplexing across models on shared nodes (see
multi-model serving deep dive), realistic utilization gains bring this down to roughly 40-60
GPU-equivalents actually provisioned, a ~2x hardware efficiency gain that is the platform's core
economic justification for existing at all versus per-team dedicated fleets.

Batch scoring load: assume 50 of the 200 models also run periodic batch scoring jobs (e.g.
nightly scoring of all users for a churn model) — a representative job scores 50M entities at
~5K entities/sec/worker → ~2.8 hours on a single worker, parallelized across ~20 workers to
finish inside a 15-minute batch window; batch capacity is provisioned as a separate,
independently-autoscaled pool from real-time capacity since the two have entirely different load
shapes (batch is a large burst then idle, real-time is continuous).

Canary/A-B traffic: with 200 models each rolling out changes at a conservative average of once a
week, that's ~28 rollouts/day across the platform, each requiring canary traffic (typically
1-5% of that model's traffic) held at reduced volume for an evaluation window (often 30-60
minutes) before promotion — this is a scheduling/orchestration load on the control plane, not a
significant data-plane load, since canary traffic volume itself is small.

## API Design

```
POST /v1/models/{model_name}/versions
     { artifact_uri, framework, resource_requirements }
     -> 201 { version_id, status: "deploying" }

POST /v1/models/{model_name}/predict
     { inputs, version?: "latest" | specific_version_id }
     -> 200 { outputs, version_served, latency_ms }

PUT  /v1/models/{model_name}/traffic-policy
     { rules: [{version_id, weight_pct}], canary?: {version_id, pct, eval_window_min,
       auto_rollback_on: {metric, threshold}} }
     -> 200

POST /v1/models/{model_name}/batch-jobs
     { input_dataset_uri, version, output_uri }
     -> 202 { job_id }
```

`predict` deliberately returns `version_served` in every response — this is what makes online
A/B attribution possible downstream (a logged prediction can be joined back to an eventual
outcome event and correctly attributed to the model version that actually produced it, not just
the model name).

## Data Model / Feature Schema

```
model_registry (source of truth for every model + version)
  model_name, version_id, artifact_uri, framework (torch/tf/onnx/...),
  resource_requirements (cpu/gpu/memory), created_at, owner_team, status

deployment_state (current placement of each version across the fleet)
  version_id -> [node_ids serving it], current_traffic_weight_pct, health_status

traffic_policy (per model_name, routing configuration)
  model_name -> { rules: [version_id, weight], canary_config }

prediction_log (sampled, for monitoring + A/B attribution)
  request_id, model_name, version_served, inputs_hash, output_summary,
  latency_ms, ts

model_metrics (aggregated, per model_name x version_id, rolling windows)
  qps, p50/p99 latency, error_rate, (optionally) business metric if outcome
  is joinable, e.g. click-through rate for a ranking model
```

The separation between `model_registry` (what versions exist) and `deployment_state` (where
they're currently running and at what traffic weight) matters operationally: a version can be
registered and warmed on hardware before it receives any live traffic, decoupling artifact
deployment from traffic cutover — the two are separate steps precisely so a bad rollout fails at
the traffic-shift step, not the deploy step, and rollback is "shift weight back," not
"redeploy."

## High-Level Architecture

```
 rollout / control-plane path:
 team pushes model artifact ──► Model registry (versioned, immutable artifacts)
                                          │
                                          ▼
                        Deployment controller: provisions the new version onto
                        a subset of serving nodes, health-checks it, warms it
                        (loads weights, runs a few synthetic requests)
                                          │
                                          ▼
                        Traffic policy updated: canary weight (e.g. 5%) assigned
                        to new version; controller watches model_metrics for the
                        canary version against the baseline version
                                          │
                              regression? ──yes──► auto-rollback (weight -> 0)
                                    │no
                                    ▼
                        gradual ramp (5% -> 25% -> 100%) over the eval window

 serving (data-plane) path:
 client ──POST /predict──► Router (looks up traffic_policy for model_name,
                             picks a version per the configured weights)
                                          │
                                          ▼
                        Model server process (one or more model versions
                        loaded, multiplexes requests across them — see
                        multi-model serving deep dive)
                                          │
                                          ▼
                        prediction returned; sampled request logged to
                        prediction_log for monitoring + A/B attribution
```

## Deep Dives

**Multi-model serving and request batching.** Dedicating a full node (or GPU) to every one of
200 models, sized for each model's own peak load, wastes capacity badly for the long tail of
low-traffic models — most of the time, most of that dedicated hardware sits idle. A
multi-model server instead loads several models into the same process/node and multiplexes
requests across them, batching same-model requests that arrive close together in time (batching
is what makes GPU inference efficient — a batch of 32 requests scored together is far cheaper
per-request than 32 sequential single-request calls) while still isolating resource limits per
model so one model's traffic spike doesn't starve another's latency budget. The scheduling
problem this creates — which models to co-locate on which nodes, given each one's resource
footprint and traffic pattern — is essentially a bin-packing problem, and is what the ~2x
utilization gain in the capacity estimate above is actually coming from.

**Canary deployment and automatic rollback.** A new model version is never cut over to 100% of
traffic in one step. It's first deployed alongside the current version, receiving a small
percentage of live traffic (the canary), while the platform compares its metrics (latency, error
rate, and where available a proxy business metric) against the currently-serving version over an
evaluation window. If the canary's metrics regress past a configured threshold, traffic weight
is automatically shifted back to zero — no human has to notice and intervene for a bad rollout to
be contained to a small fraction of traffic for a short window. This is the same operational
pattern as canary deploys for regular services, but the metric that matters most for a model
(prediction quality / a downstream business metric) is often slower to materialize than a
service's error rate, which is why model canary windows tend to run longer (tens of minutes to
hours) than a typical software canary.

**Online A/B testing vs. canary.** These solve related but distinct problems and are often
conflated. Canary is a rollout-safety mechanism — a temporary state on the way to 100% (or
rollback), evaluated against operational health metrics. A/B testing is a deliberate,
often-longer-running experiment comparing two (or more) model versions on a business metric to
decide which one is actually better — both versions are considered "valid production traffic,"
not one provisional and one baseline, and the traffic split is often sustained for the full
statistical-significance period rather than ramped to 100%. The platform's traffic-policy
primitive (weighted routing across versions) is shared infrastructure for both use cases, but
the surrounding orchestration (auto-rollback vs. statistical experiment analysis) differs.

**Shadow deployment.** Before a new version receives any real (user-facing) traffic at all, it
can be deployed in shadow mode — it receives a copy of live production requests, computes
predictions, and those predictions are logged and compared against the currently-serving
version's actual outputs, but the shadow version's output is never returned to the client or
allowed to affect any downstream decision. This catches correctness and latency problems (a
new version that's badly miscalibrated, or unexpectedly slow, or crashes on some slice of real
production inputs) with zero user-facing risk, before the version is trusted enough to take even
canary traffic — see ml-monitoring-and-drift.md for how shadow deployment fits into the broader
monitoring picture.

**Batch vs. real-time serving from one artifact.** A model trained and packaged once should
serve both a latency-sensitive real-time endpoint and a large offline batch-scoring job without
requiring two separately-maintained deployment packages — the platform achieves this by treating
batch scoring as the same model artifact run through a different execution path (a distributed
batch job that loads the artifact onto many workers and scores a large input dataset in
parallel, with no per-request latency constraint) rather than a different model format. The
practical benefit is that a model behaves identically in both paths — no risk of the batch
scoring pipeline and the real-time endpoint silently drifting into different behavior because
someone maintained two separate implementations.

## Bottlenecks & Scaling

The bin-packing/scheduling problem of placing many models onto shared hardware gets harder as
model count and heterogeneity (different frameworks, wildly different resource footprints, very
different traffic patterns) grow, and is usually the first component to need its own dedicated
engineering investment as a platform scales past the first few dozen hosted models — naive
round-robin placement leaves utilization gains on the table that the whole platform exists to
capture. The control plane (deployment controller, traffic policy, canary evaluation) needs to
scale with the *rate of model rollouts* across all 200+ teams, not with request volume — at
enough hosted models, rollout velocity across the platform can itself become a scaling bottleneck
if canary evaluation and promotion aren't fully automated. Prediction logging for monitoring and
A/B attribution has to be sampled rather than exhaustive once aggregate request volume gets into
the 100K/sec range, trading some statistical power in downstream analysis for a logging pipeline
that doesn't become the platform's own bottleneck. Finally, the long tail of low-traffic models
is where multi-model co-location matters most economically, while the head of high-traffic
models is usually better served by dedicated capacity even on a shared platform — sizing that
split correctly is an ongoing operational tuning problem, not a one-time architecture decision.
