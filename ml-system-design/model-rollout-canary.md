# Model Rollout and Canary Release Platform

## Requirements

### Functional
- Move a new model version from registry to full production traffic through a sequence of
  increasing-exposure stages (shadow, canary, staged ramp, full), with each stage's promotion
  gated on measured metrics rather than a fixed timer.
- Support model "shapes" beyond simple request/response scoring — batch/offline scoring jobs and
  embedding/retrieval models each need a canary comparison method appropriate to how they're
  actually consumed, not just an HTTP traffic-split mechanism.
- Automatically roll back to the previous known-good version when a gated metric breaches its
  threshold at any stage, without waiting on a human to notice a dashboard.
- Support champion/challenger comparison as a distinct, longer-running mode from canary —
  evaluating a challenger against the current champion over a longer window and larger sample than
  any single canary stage is sized for, before the challenger is even nominated to enter the
  staged rollout pipeline at all.

### Non-functional
- Rollback must complete within a bounded, short time — target under 60 seconds from breach
  detection to traffic fully reverted — since the entire premise of staged rollout is bounding
  blast radius, and a slow rollback undermines that guarantee regardless of how well-designed the
  staging schedule is.
- Promotion decisions must be statistically sound at every stage, not just "no visible dashboard
  alarm" — an early stage running at 1% of traffic has materially less statistical power than a
  later stage at 25%, and the gating logic has to account for that difference rather than applying
  one fixed threshold uniformly across every traffic level.
- The release pipeline's own state must survive a control-plane restart mid-rollout — a release
  halfway through a staged ramp cannot be allowed to silently stall, or worse, resume from an
  inconsistent stage after recovery.
- The platform must support many concurrent releases across many models without cross-release
  interference — one model's canary evaluation window must not be affected by another model's
  simultaneous rollout sharing the same infrastructure.

## Capacity Estimation

Assume the platform manages releases for the same 200-model fleet as model-serving-platform.md,
each rolling out changes at a conservative average of once/week → 200/7 ≈ **29 releases/day**
started.

**Stage schedule and duration.** A representative schedule: shadow (100% traffic mirrored, zero
serving impact) for 2 hours → canary at 1% for 30 min → canary at 5% for 30 min → ramp at 25% for
1 hour → ramp to 100%. If every gate passes cleanly, total pipeline duration is roughly 4-5 hours;
adding buffer for gate re-evaluation on ambiguous metrics, call it **~6 hours average time-to-
full-rollout**.

**Concurrent in-flight releases.** 29 releases/day started, ~6hr average duration → concurrent
in-flight ≈ 29 × 6/24 ≈ 7.25, rounding up for variance to roughly **10-15 concurrent releases** at
any given time. This is a small absolute number — the platform's real challenge is per-release
evaluation correctness and isolation between concurrently-running releases, not raw throughput.

**Gate evaluation frequency.** Each active stage is re-evaluated on a fixed interval (e.g. every 2
minutes) against accumulating traffic → at 15 concurrent releases × 1 evaluation/2min = **7.5
evaluations/min** platform-wide — trivial compute load; the real cost sits in each evaluation's
metric-aggregation query against recent traffic, not the evaluation loop itself.

**Canary sample size.** A canary at 1% of a model serving 1M predictions/day sees ~10,000
predictions/day, or roughly 208 predictions in a 30-minute stage window. For a business metric
with an effect size of interest around 1-2%, a sample this small has limited statistical power to
confidently detect a real regression inside that window — this is precisely why low-baseline-
volume models need longer dwell times or higher traffic percentages at early stages than
high-volume models to reach comparable statistical power (see deep dive).

**Champion/challenger comparison.** A longer-running held-out comparison at a modest fixed split
(e.g. 10/90) runs before a challenger is even nominated for the staged pipeline. For a 1M
predictions/day model, 10% = 100,000 predictions/day, accumulated over 3-7 days before the
challenger is trusted enough to enter canary → **300K-700K compared predictions** per
champion/challenger evaluation, an order of magnitude more data than any individual canary stage
gets, by design.

## API Design

```
POST /v1/releases
     { model_name, challenger_version, champion_version,
       stage_schedule: [...], promotion_criteria: [...] }
     -> 201 { release_id, status: "shadow" }

GET  /v1/releases/{release_id}
     -> { status, current_stage, stage_history: [...], traffic_pct, gate_metrics: {...} }

POST /v1/releases/{release_id}/promote     (manual override — skip ahead, or force past a warn-level gate)
     -> 200

POST /v1/releases/{release_id}/rollback    { reason }
     -> 200 { reverted_to_version, revert_completed_at }

POST /v1/champion-challenger
     { model_name, challenger_version, split_pct, duration_days }
     -> 201 { comparison_id }

GET  /v1/champion-challenger/{comparison_id}
     -> { agreement_rate, metric_deltas: {...}, recommendation: promote_to_canary|reject }
```

`/champion-challenger` is a separate resource from `/releases`, not a stage within it — a
comparison can run, conclude, and recommend rejection without ever creating a release at all,
which is the intended outcome most of the time a challenger doesn't clear the bar.

## Data Model

```
release (one per rollout attempt for a model)
  release_id, model_name, champion_version, challenger_version,
  stage_schedule: [{stage, traffic_pct, min_dwell_min, promotion_criteria}],
  current_stage_index, status: shadow|canary|ramp|full|rolled_back|failed, created_at

stage_execution (one per stage attempt within a release)
  release_id, stage, entered_at, exited_at, traffic_pct, n_samples,
  gate_result: pass|fail|extend,
  gate_metrics: {latency_p99_delta, error_rate_delta, business_metric_delta, confidence}

rollback_event (append-only, one per rollback triggered)
  release_id, triggered_at, triggered_by: auto|manual,
  breached_metric, reverted_to_version, completed_at

champion_challenger_run (long-running comparisons, separate lifecycle from release)
  comparison_id, model_name, champion_version, challenger_version, split_pct,
  start_ts, end_ts, n_compared, agreement_rate, metric_deltas, recommendation
```

`stage_execution` is append-only per attempt, not a single mutable row per stage — a stage that
gets an `extend` result and is re-evaluated at the next interval produces a new row, so the full
history of "how many times did this stage need extending before it passed or failed" is preserved
for post-incident review rather than overwritten.

## High-Level Architecture

```
 Model registry: new challenger_version registered
                              │
                              ▼
              Champion/challenger evaluation (optional, long-running,
              held-out split — see deep dive)
                              │
                  recommendation: promote_to_canary
                              │
                              ▼
                    Release created ──► Release orchestrator
                    (state machine, drives release through stage_schedule)
                              │
        ┌─────────────────────┼──────────────────────────────┐
        ▼                     ▼                                ▼
   shadow stage         canary stage(s)                  ramp stage(s)
   traffic mirrored     traffic split via the same        same gate loop,
   to challenger,        mechanism as                      increasing traffic_pct
   output logged,        ab-testing-and-experimentation.md  and min_dwell_min per
   not served (shadow    (1% → 5%, e.g.)                    stage_schedule
   infra shared with                │
   ml-monitoring-and-      gate evaluator: statistical test against
   drift.md)                promotion_criteria, confidence-adjusted
        │                   for this stage's sample size
        ▼                            │
   behavioral parity        ┌────pass────┴────fail/extend────┐
   check (latency,          ▼                                 ▼
   error rate,          advance to next stage           auto-rollback (fail) or
   prediction-                                            re-evaluate next interval
   distribution                                            (extend)
   similarity vs
   champion) ──► gate

 on reaching full: challenger becomes champion; old champion version's
 serving infra retained warm (not torn down) as the fast-rollback target
 through the retention window

 on any gate fail: rollback path ──► traffic weight reverted to champion
 atomically ──► rollback_event logged ──► release marked rolled_back,
 owning team alerted
```

## Deep Dives

**Staged rollout as a state machine.** A release is modeled as a strict sequential state machine —
`shadow → canary(1%) → canary(5%) → ramp(25%) → ramp(100%)` — with explicit, persisted entry and
exit criteria per stage, rather than a free-form traffic-percentage knob a human or a script nudges
upward. This matters for two structural reasons. First, control-plane-restart safety: because
`current_stage_index` and `stage_execution` history are persisted, a control-plane process
crashing mid-rollout resumes by reading exactly where the release left off, rather than either
losing track of an in-flight release or (worse) re-entering a stage it had already passed and
re-running a promotion decision against stale accumulated metrics. Second, unambiguous
observability: "what stage is this release actually in" is a directly queryable fact
(`release.status`, `release.current_stage_index`), not something inferred by cross-referencing
current traffic weight against a deploy log — which matters directly during an incident, where the
first question is almost always "what's live right now and how did it get there."

**Statistical power per stage and confidence-adjusted gating.** Applying the same fixed pass/fail
threshold at a 1%-traffic stage and a 25%-traffic stage is a subtle but common bug: the two stages
accumulate wildly different sample sizes over the same dwell time, and a metric delta that would be
a confident, real regression at 25% traffic can be statistically indistinguishable from noise at
1% traffic given how few samples that stage sees (208 predictions in the capacity estimate above,
for a middling-volume model). The gate evaluator therefore checks confidence alongside effect size
at every stage, and `min_dwell_min` in the stage schedule functions as a minimum-sample-size
precondition, not a fixed wait timer — a low-baseline-volume model needs a proportionally longer
dwell time at each early stage to reach the same statistical power a high-volume model reaches in
minutes, and the gate evaluator's `extend` result exists specifically to lengthen a stage
automatically when accumulated confidence is still too low to decide, rather than forcing a
decision at an arbitrary elapsed-time cutoff regardless of how little data has accumulated.

**Fast rollback mechanics.** Rollback has to be "revert traffic weight to the prior version," an
operation against already-running infrastructure, not "redeploy the old version," which would
reintroduce the multi-minute node-boot-and-weight-load latency covered in
gpu-fleet-autoscaler.md — directly on the critical path of an incident response, which is exactly
where that latency is least tolerable. This is why the previous champion version's serving
infrastructure is deliberately kept warm, not torn down, through at least one full rollout cycle
after a new version is promoted: the moment it's decommissioned, the next rollback (whether for
this same model's next release or a regression discovered later) loses the fast-path option and
falls back to a full redeploy. The retention window for that warm standby capacity is a direct
cost/safety tradeoff and is discussed further in Bottlenecks & Scaling.

**Champion/challenger vs. canary.** These answer different questions and are frequently conflated.
Champion/challenger asks "is this challenger meaningfully better than the current champion" —
a comparative-quality question, answered with a longer-running, larger, held-out sample precisely
because detecting a genuine quality improvement (as opposed to an operational regression) usually
requires more statistical power than a short canary window can provide. Canary asks a narrower,
different question: "is deploying this specific build artifact of the challenger operationally
safe" — evaluated against latency, error rate, and other operational health thresholds during the
actual rollout, not against comparative model quality. The two are sequential, not redundant: a
challenger that wins a champion/challenger comparison decisively can still fail canary, because
canary catches a different failure class entirely — a regression introduced in the time between
the comparison snapshot and the actual deploy artifact, a resource-configuration issue the
comparison's offline-style evaluation never exercised, or a latency regression from a change that
doesn't affect prediction quality at all. A release skipping champion/challenger and going straight
to canary is a legitimate choice for a low-stakes model; skipping canary because champion/challenger
already looked good is not, since it discards the operational-safety question canary is the only
stage that actually answers.

**Canary for non-request/response model types.** A batch scoring job has no live request traffic
to split, so its canary instead runs the challenger version against a fixed recent input sample in
parallel with the champion's regularly scheduled run, diffing output distributions before the
challenger is promoted to take over the next scheduled production run — the "traffic percentage"
concept simply doesn't apply, and forcing one onto a batch job produces a canary stage that never
actually exercises anything. An embedding or retrieval model's canary is different again: since
embedding output correctness isn't observable from the embedding vector itself (a vector is just
numbers; there's no "error rate" on a vector), its canary compares downstream retrieval quality —
recall@k against a small labeled set, using the same evaluation approach as
rag-evaluation-platform.md — rather than a request-level latency/error metric. Both cases share
the same underlying principle as request/response canary (compare challenger against champion on
a metric that actually reflects what the model is for, at increasing confidence before full
cutover) while requiring a genuinely different comparison mechanism, which is why
`promotion_criteria` is a per-release configuration rather than a fixed metric set baked into the
orchestrator.

## Bottlenecks & Scaling

Gate evaluation quality — avoiding both false-pass (a real regression slips through a
low-powered early stage) and false-hold (a good release gets stuck extending indefinitely on noisy
metrics) — is the component most resistant to pure infrastructure scaling, and is fundamentally a
per-model statistics-tuning problem, structurally similar to the alert-threshold-tuning bottleneck
described in ml-monitoring-and-drift.md. The release orchestrator's state persistence and recovery
correctness matters more than its raw throughput at this concurrency (10-15 releases in flight at
once) — a control-plane bug that loses or corrupts a single release's stage state can leave a
model stuck mid-rollout, or worse, leave it serving an unvetted challenger version at full traffic
with no record of how it got there, which is a correctness incident regardless of how rarely it
happens. Keeping a warm standby of the prior champion version for fast rollback carries a real,
ongoing infrastructure cost — duplicate serving capacity for the retention window — that scales
with the number of concurrent releases across the fleet, and needs an explicit retention policy
(how long past full promotion is a fast-rollback target kept warm) rather than indefinite
retention, which would otherwise silently double the platform's steady-state serving footprint.
Finally, champion/challenger evaluation windows for genuinely low-traffic models can stretch the
pre-canary vetting stage from days into weeks purely to accumulate enough compared predictions for
a confident recommendation — this is a structural limitation on how fast a low-volume model can
safely iterate relative to a high-volume one, not a tuning problem the platform can optimize away,
and teams owning low-volume models plan release cadence around it rather than expecting parity with
a high-traffic model's iteration speed.
