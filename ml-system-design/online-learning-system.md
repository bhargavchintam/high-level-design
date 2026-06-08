# Online Learning System

## Requirements

### Functional
- Update a production model's parameters continuously from a stream of labeled events, rather
  than only via periodic batch retraining, so the model reflects behavior from minutes ago
  instead of the last nightly or weekly retrain.
- Correctly attribute delayed and noisy labels back to the feature state that existed at
  prediction time — most labels (conversion, chargeback confirmation, churn) arrive seconds to
  weeks after the prediction they're labeling, and joining a label to the wrong feature snapshot
  silently corrupts the training signal.
- Detect and correct feedback loops where the model's own past predictions bias the traffic
  distribution it now learns from (e.g. a ranking model that only surfaces items it already
  scored highly, then "confirms" that ranking from the resulting clicks on a self-selected
  sample).
- Guardrail every online update before it reaches production serving weights — a bad batch of
  events (bot traffic, an upstream logging bug, an adversarial input pattern) must not be able to
  silently degrade a model that's actively serving live traffic.

### Non-functional
- Update-to-serving latency: a labeled event should be reflected in production predictions within
  minutes — distinctly tighter than a batch retrain cadence (hours to a day), but not literally
  per-event, since a per-event weight update lacks the statistical stability a mini-batch provides
  (see Capacity Estimation).
- Bounded blast radius per update: online-updated model behavior must not be able to regress
  unboundedly between guardrail checks — gradient clipping and a hard per-update guardrail gate
  are load-bearing requirements, not tuning conveniences.
- Reproducibility is structurally weaker than batch training (state is continuously mutating), but
  every deployed weight snapshot must remain traceable to the exact ordered sequence of updates
  that produced it, so an incident can be root-caused to a specific update rather than "sometime
  this afternoon."
- Update pipeline throughput scales with label arrival rate, independent of prediction-serving
  QPS — labels and predictions are decoupled streams with different volumes and different
  latencies, and provisioning one as a function of the other under- or over-builds the pipeline.

## Capacity Estimation

Assume the online-updated model backs a fraud-scoring or feed-ranking product at a scale
comparable to fraud-detection-system.md and recommendation-system.md: 300M predictions/day.
Not every prediction resolves to a label inside a window short enough to be useful for online
learning — assume 20% resolve within minutes to a few hours (the online-learning-eligible slice;
long-latency labels like 30-day churn still go through the batch retrain path) → **60M labeled
events/day** feeding the online learner.

60M / 86,400 ≈ 694 labeled events/sec average, ~2,100/sec peak (3x diurnal factor).

**Mini-batch cadence.** Updating weights on every single event produces high-variance gradient
steps (classic online SGD noise) and, at 694 events/sec, would mean thousands of tiny update
events per second — no guardrail-evaluation pipeline can keep up with a promotion decision at
that rate. Production online learners batch a short window (1-5 seconds, or a fixed event count)
before applying one update. At a 5-second window: ~3,470 events/update average, ~10,500/update at
peak — large enough to average out per-event noise, small enough to still land well inside the
"minutes, not hours" freshness requirement.

**Update frequency.** A 5-second batch window yields 86,400 / 5 = **17,280 candidate updates/day**
at steady state, each a small optimizer step (FTRL or Adam on a shallow/linear model, or a
low-rank adapter update layered on a larger frozen base model — see deep dive on why full
fine-tuning is rarely the default choice here).

**Guardrail evaluation cost.** Each candidate update is shadow-scored against a held-out recent
traffic slice (the last 10 minutes, ≈ 694/sec × 600s ≈ 416K predictions) before it's allowed to
replace the serving version. This shadow-eval pass, run 17,280 times/day, is the dominant compute
cost of the whole pipeline — not the gradient step itself — which is precisely why the model kept
under continuous online update is deliberately kept small or low-rank: a shadow-eval budget that
has to complete in well under 5 seconds, 17,280 times a day, cannot be spent on a heavyweight
model.

**State size.** The online learner keeps an in-memory replay buffer of recent (feature, label)
pairs spanning the current batch window plus a lookback long enough to absorb typical label
delay. At ~1KB/example and a 10-minute retained window at 694/sec: 694 × 600 × 1KB ≈ **416 MB**
per learner shard — small enough to live entirely in memory, which matters because the buffer is
read on every update cycle, not just written to.

## API Design

```
POST /v1/online-learner/{model_name}/events
     { prediction_id, features, label, label_ts }
     -> 202  (queued into the current mini-batch window; not applied synchronously)

POST /v1/online-learner/{model_name}/apply-update      (internal, batch-scheduler triggered)
     -> 200 { update_id, base_version, gradient_norm,
              guardrail_status: pass | hold | reject }

GET  /v1/online-learner/{model_name}/status
     -> { current_version, last_update_ts, updates_applied_today,
          updates_rejected_today, staleness_ms }

POST /v1/online-learner/{model_name}/pause
     -> 200  (freezes weights at current version — manual, or automatic after
              repeated consecutive guardrail rejects, see deep dive)

POST /v1/online-learner/{model_name}/rollback   { target_version }
     -> 200
```

`/events` returns 202 rather than applying anything synchronously — the caller is contributing
one data point to a future batched update, not requesting an update, and conflating those two
would let a single client-side retry storm trigger update churn.

## Data Model

```
event_buffer (in-memory, per learner shard, windowed — not durably persisted past the
              retention lookback)
  prediction_id, features, label, label_ts, prediction_ts,
  weight              (importance weight — down-weights very late labels, see deep dive)

update_log (append-only, one row per candidate update, applied or rejected)
  update_id, model_name, base_version, resulting_version (null if rejected),
  window_start, window_end, n_examples, gradient_norm,
  guardrail_status, shadow_eval_metrics: {auc_delta, calibration_delta, ...}

model_version_store (one row per weight snapshot ever produced, forms a version chain)
  version_id, model_name, parent_version_id, created_at,
  update_id (nullable — the base batch-trained model has no update_id), weights_ref

feedback_loop_monitor (rolling, per model, computed each update cycle)
  model_name, window, exposure_gini (concentration of predictions across the
  item/action space), exploration_pct, alert_status
```

`model_version_store.parent_version_id` makes every serving weight snapshot a node in a version
chain back to the last full batch-trained model — this is what makes rollback well-defined (walk
back to any prior version_id, not just "undo the last update") and what makes an incident
debuggable as a specific, ordered sequence of updates rather than an opaque blob of continuously
drifting state.

## High-Level Architecture

```
 label ingestion:
 outcome confirmed ──► Label join service (matches label back to the original
                        prediction_id + feature snapshot — see real-time-ml-pipeline.md
                        for the underlying feature/label join mechanics this reuses)
                                    │
                                    ▼
                        Event buffer (windowed, per model shard, ~10min retained)
                                    │
                        batch scheduler fires every N seconds (e.g. 5s)
                                    ▼
                        Online learner: mini-batch gradient step against current
                        weights (FTRL / Adam / low-rank adapter update)
                                    │
                                    ▼
                        Candidate weights (held, not yet serving)
                                    │
                                    ▼
                        Guardrail evaluator
                          - gradient norm within configured bound?
                          - shadow-eval candidate vs. current serving version on
                            held-out recent traffic (~416K predictions) — metric
                            regression past threshold?
                          - feedback-loop monitor: exposure concentration
                            (exploration_pct) within bounds?
                                    │
                    ┌───────pass───┴───reject/hold───────┐
                    ▼                                     ▼
        promote to serving                     keep prior version serving;
        (writes model_version_store,           log rejection to update_log;
        traffic cutover mechanics per          N consecutive rejects trips
        model-serving-platform.md)             the automatic pause switch
```

## Deep Dives

**Mini-batch cadence as a bias-variance knob.** The batch window is the single most consequential
tuning parameter in this system. A very short window (near-per-event updates) tracks the freshest
possible signal but applies gradient steps computed from a small, noisy sample — high update
variance, and a real risk of the model chasing statistical noise rather than a genuine shift.
A long window (minutes) averages out noise into a stable gradient estimate but reintroduces the
staleness this whole system exists to eliminate, converging back toward a slow-batch-retrain
freshness profile in the limit. The 1-5 second range used in the capacity estimate is a practical
middle point: large enough (thousands of examples) for the gradient estimate to be reasonably
stable, small enough that a genuine behavior shift (a new fraud pattern, a viral item) shows up in
serving weights within single-digit minutes end to end, including the guardrail gate.

**Delayed and noisy label handling.** A label observed 5 seconds after its prediction and one
observed 4 hours after its prediction are not equally trustworthy inputs to the same mini-batch:
the longer the gap, the more the feature snapshot at prediction time may have drifted from the
world the label is now describing, and the more likely the example belongs to a rarer,
harder-to-model tail of behavior (fast-resolving labels are frequently the easy cases). Production
online learners apply an importance weight that decays with label latency, and set the buffer's
retention window to the label-latency distribution's practical cutoff (not its worst case) — a
label arriving after the buffer has evicted its matching prediction is simply dropped from the
online path and picked up instead by the next full batch retrain, which has no such retention
constraint. This is a deliberate scope boundary: online learning optimizes for freshness on the
labels that can plausibly resolve fast; anything with genuinely long label latency (30-day churn,
disputed-chargeback resolution) is explicitly the batch pipeline's job, not this one's.

**Feedback loops.** A model that only ever gets to observe outcomes on the traffic it already
chose to expose is not a neutral observer of "what works" — it's training on a training set its
own past decisions already filtered. A ranking model that stops showing item X because it once
scored it low will (almost by construction) never collect the click data that could prove it
wrong, and each online update reinforces the same self-selected exposure pattern a little further.
`feedback_loop_monitor.exposure_gini` tracks how concentrated predictions/exposure are across the
item or action space over each rolling window — a rising Gini coefficient with a falling
exploration_pct is the operational signature of a tightening feedback loop, distinct from and
often invisible to the guardrail metrics (which only look at prediction quality, not exposure
distribution). The standard mitigation is the same one used in the recommendation-system case
study: mixing in a small, deliberately random or uncertainty-weighted exploration slice of
traffic, so every online update keeps seeing outcomes outside what the model currently believes,
not just confirmations of it.

**Guardrails and the promotion gate.** No candidate update reaches serving weights without
passing through the shadow-eval gate — computed the same way a canary is evaluated in
model-serving-platform.md, except run automatically 17,280 times/day against a tight time budget
instead of a handful of times/week against a generous one. The gate checks two different failure
classes: a gradient-norm bound catches numerically pathological updates (an outlier batch, a
feature-pipeline bug producing extreme values) before they're even shadow-evaluated, cheaply;
the shadow-eval metric-regression check catches updates that are numerically fine but
statistically bad (the model got worse against held-out recent traffic). Both checks feeding into
one `guardrail_status` matters operationally — a `reject` from the gradient-norm check usually
means "something upstream is broken" and should page a human, while a `hold` from shadow-eval
regression more often means "this window's data was unrepresentative" and is fine to silently
retry on the next window. Repeated consecutive rejects (not holds) automatically trip the pause
switch, freezing weights at the last known-good version rather than letting a persistently broken
input pipeline keep grinding against a gate that will never pass.

**Catastrophic forgetting and the batch-model anchor.** A model updated purely from a recent
sliding window of online data has no mechanism forcing it to remember patterns that were common
before that window opened — enough consecutive updates on a temporarily shifted distribution
(a holiday traffic pattern, a promotional campaign) can measurably erode performance on the
steady-state behavior the model needs to handle once that period ends. The standard mitigation
isn't a bigger buffer (which reintroduces staleness) but anchoring: either regularizing each
online update toward the weights of the last full batch-trained model (an L2 penalty pulling
updated weights back toward the batch baseline), or periodically replacing the online-updated
model entirely with a fresh batch retrain that incorporates the full historical distribution, and
resuming online updates from that new baseline. In practice, most production systems run both
loops permanently: a heavyweight batch-retrained model as the stable, periodically-refreshed
baseline, and a lightweight online-updated adapter layered on top that gets reset to the baseline
on every batch retrain cycle — freshness from the online loop, stability from the batch anchor,
neither one asked to do the other's job.

## Bottlenecks & Scaling

Shadow-eval cost is the binding constraint on update frequency, not the gradient computation
itself — a shorter batch window directly multiplies how many times/day a held-out evaluation pass
has to run and complete within budget, which is why the system keeps the continuously-updated
model small or low-rank rather than scaling shadow-eval infrastructure to match an arbitrarily
tight window. Buffer retention scales with the practical label-latency cutoff, not the average
label latency — a model with a long tail of slow-resolving labels needs a proportionally larger
in-memory buffer per shard, and past some retention length it becomes cheaper to accept those
tail labels only in the batch path than to keep growing the online buffer to catch them. Guardrail
statistical power is itself a scaling concern: a smaller batch window produces a smaller shadow-eval
sample each cycle, which produces noisier metric-regression estimates, which produces both more
false rejects (unnecessarily discarding good updates) and more false accepts (promoting updates
that only look neutral due to sample noise) — tuning the window size is therefore a joint
optimization against both freshness and guardrail reliability, not freshness alone. Finally,
coordination between the online loop and the periodic batch-retrain anchor needs its own
scheduling discipline at fleet scale (hundreds of independently online-updated models each on
their own batch-retrain cadence) so that a batch-retrain cutover for one model doesn't race
against an in-flight online update for that same model and produce an inconsistent version chain.
