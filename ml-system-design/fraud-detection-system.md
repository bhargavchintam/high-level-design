# Real-Time Fraud Detection System

## Requirements

### Functional
- Score every payment transaction in real time and return a decision: approve, decline, or
  route to manual review — sitting inline in the payment-authorization path, not as an
  after-the-fact batch flag.
- Incorporate graph/relationship features (shared device, shared card, shared account, shared
  IP across transactions) to catch coordinated fraud rings that look individually unremarkable
  but are structurally connected.
- Close the loop from confirmed fraud outcomes (chargebacks, manual review confirmations) back
  into retraining, so the model adapts as fraud patterns evolve — a static model trained once
  degrades against an adversary that actively adapts to it.
- Return reason codes alongside the decision, not just a score — both for downstream review-
  queue triage and for regulatory/audit requirements around adverse action explanations.

### Non-functional
- Tight end-to-end latency budget: the score sits inline in payment authorization, so the whole
  pipeline (feature lookup + graph lookup + inference + rules) must fit in roughly 50-100ms p99,
  on top of the payment processor's own authorization round trip — a hard ceiling, not a target,
  since blowing it means declining or timing out a transaction that might have been legitimate.
- Extreme class imbalance: confirmed fraud is typically 0.1-1% of transaction volume, so a model
  that always predicts "not fraud" scores >99% accuracy while being completely useless — this
  invalidates naive accuracy/F1-style evaluation and naive training as-is (deep dive below).
- Asymmetric cost function: a false decline (blocking a legitimate transaction) costs a
  frustrated customer, lost revenue, and real churn risk, while a missed fraud costs the
  transaction amount plus chargeback fees directly — not symmetric, not even the same order of
  magnitude across merchant categories, so the decision threshold is tuned against actual
  business cost, not a generic classification metric.
- Delayed and noisy ground truth: a confirmed chargeback typically arrives 30-90+ days after the
  transaction, so the freshest labeled training data available at any time is already over a
  month stale relative to current fraud patterns.

## Capacity Estimation

Assume a payments platform processing 5,000 transactions/sec average, with a 6x holiday-season
peak → 30,000 transactions/sec peak. Every one of these needs a synchronous score before the
transaction can be authorized — this is not a background job, so throughput sizing has to be
done at peak, not average.

**Latency budget breakdown**, targeting a 100ms end-to-end ceiling: feature store lookup
(recent transaction aggregates for this card/account/device) ≈ 15ms; graph feature lookup
(precomputed ring/connectivity metrics, not a live graph traversal — see deep dive) ≈ 20ms;
model inference (a GBDT ensemble, not a giant neural net, specifically because GBDTs are cheap
and fast at inference) ≈ 15ms; rules engine pass (hard-coded deterministic blocks/allows) ≈
5ms; leaves ≈ 45ms of network/serialization/queueing headroom against the 100ms ceiling — tight
enough that any one of these stages regressing by 2x risks blowing the whole budget.

**Fraud volume and label volume.** At a 0.3% fraud rate, 5,000 tx/sec × 86,400 sec/day = 432M
transactions/day → ~1.3M fraudulent transactions/day requiring eventual confirmation. Given the
30-90 day confirmation lag, at any point in time there's a rolling window of roughly 30-90 days
× 1.3M/day ≈ 39M-117M transactions whose true label is still unresolved — this pending-label
pool is larger than the confirmed-fraud training set itself, which is the crux of the delayed-
label problem (deep dive below).

**Feature store throughput.** Each transaction triggers lookups against ~10-20 real-time
aggregate features (transaction count/amount in the last 1h/24h/7d, per card/account/device) →
30,000 tx/sec × 15 feature reads ≈ 450K reads/sec at peak against the online feature store —
sized the same way as the streaming aggregation pipeline in
ml-system-design/real-time-ml-pipeline.md, since this is the same class of infrastructure
(rolling counters computed off a live event stream) applied to a different domain.

**Graph size.** A graph node per unique card/device/account/IP, edges for co-occurrence in a
transaction. At 432M transactions/day generating a handful of edges each, and retaining a
rolling window of active entities (say 90 days), the graph runs on the order of hundreds of
millions of nodes and low billions of edges — far too large for a per-request live traversal at
a 20ms budget, which is why graph features are precomputed (see deep dive).

## API Design

```
POST /v1/score
     { transaction_id, card_id, account_id, device_id, ip, amount, merchant_id, ts }
     -> 200 {
          transaction_id,
          fraud_score: 0.83,
          decision: "review",              # approve | decline | review
          reason_codes: ["velocity_high_1h", "device_new_high_amount",
                          "graph_ring_flag"],
          model_version, latency_ms: 62
        }
     latency target: p99 < 100ms — synchronous, inline in payment authorization

POST /v1/feedback
     { transaction_id, outcome: "confirmed_fraud" | "confirmed_legitimate",
       source: "chargeback" | "manual_review", confirmed_at }
     -> 202  (feeds the label pipeline; does not affect the original decision retroactively)
```

`reason_codes` is not optional decoration — several jurisdictions require an adverse-action
explanation for automated declines, and the review queue triages faster off structured reasons
than off a bare score.

## Data Model

```
transaction_store (append-only, source of truth for every scored transaction)
  transaction_id, card_id, account_id, device_id, ip, amount, merchant_id, ts,
  fraud_score, decision, reason_codes, model_version

real_time_aggregate_features (online KV store, keyed by card_id / account_id / device_id)
  card_id -> { tx_count_1h, tx_count_24h, tx_amount_sum_24h, distinct_merchants_24h,
               velocity_flag, last_updated_ts }
  # same streaming-aggregation pattern as any feature store — see
  # ml-system-design/real-time-ml-pipeline.md — computed off the live transaction stream

entity_graph (precomputed, incrementally updated, keyed by entity node)
  node (card_id | device_id | account_id | ip) -> {
    connected_component_id, component_size, degree,
    ring_risk_score                    # precomputed off-path, refreshed on a short cycle
  }
  edges: co-occurrence in a transaction (card-device, device-ip, account-card, ...)

label_store (append-only, arrives on a long delay relative to the transaction)
  transaction_id, label: fraud | legitimate, label_source: chargeback | manual_review,
  confirmed_at, delay_days             # delay_days is tracked explicitly — it's a feature
                                        # of the training pipeline's own health, not noise
```

The graph is intentionally not queried live per transaction — `ring_risk_score` and
`component_size` are columns computed by an offline/streaming graph job and read as ordinary
KV lookups at scoring time, turning an expensive graph traversal into a cheap point read.

## High-Level Architecture

```
 TRAINING PATH

 transaction_store ──┐
                       │
 label_store (chargebacks, delayed 30-90d;      ┌─► class-imbalance handling
   manual review confirmations, faster) ──join──┤    (class weighting / undersampling
                       │                         │    of the majority legitimate class)
                       ▼                         │
              labeled training set ──────────────┘
                       │
              GBDT training job (XGBoost/LightGBM-class model,
              chosen for inference speed + native handling of
              tabular/mixed feature types + explainability via
              feature importance / SHAP for reason codes)
                       │
                       ▼
              model registry ──► threshold calibrated against
                                  false-decline vs. missed-fraud
                                  cost, not accuracy/F1


 SERVING PATH  (inline, synchronous, in the payment-authorization request)

 payment request ──► Scoring service
                          │
          ┌───────────────┼────────────────────┐
          ▼                ▼                    ▼
   feature store    graph feature store    rules engine
   lookup (recent    lookup (precomputed    (hard deterministic
   velocity/amount   ring_risk_score,       blocks: known-bad card/
   aggregates)        component_size)        device blocklist, sanctions
          │                │                  list, hard velocity caps)
          └───────┬────────┘                    │
                   ▼                              │
            GBDT model inference                  │
                   │                              │
                   └──────────► decision merge ◄──┘
                                (rules can force-decline
                                 regardless of model score;
                                 model score drives the
                                 approve/review/decline
                                 split in the gray zone)
                                       │
                                       ▼
                        response: score + decision + reason_codes
                                       │
                                       ▼
                        transaction_store (logged for eventual
                        labeling, feeding back into the training path)
```

## Deep Dives

**Class-imbalance handling.** Training a model naively on raw 0.3%-positive data biases it
toward always predicting the majority class, since minimizing plain log-loss or accuracy over
such a skewed distribution is satisfied almost entirely by getting the easy 99.7% right and
barely penalizes missing the rare positives. The standard mitigations are class weighting
(upweighting the fraud class in the loss function so misclassifying it costs proportionally
more), resampling (undersampling the legitimate class or oversampling/SMOTE-style synthesizing
the fraud class so the training distribution is less extreme), or reframing entirely as
anomaly/outlier detection when positive examples are too sparse for supervised learning to work
well at all. None of these are free: aggressive undersampling throws away legitimate-transaction
signal the model could have learned from, and oversampling risks overfitting to duplicated or
synthetic minority examples. Just as important as the training-side fix is evaluation: accuracy
and even plain F1 are close to meaningless at this imbalance, so evaluation uses precision-recall
curves and, critically, the decision threshold is tuned directly against the false-decline vs.
missed-fraud cost asymmetry from the requirements — the threshold that maximizes F1 is very
rarely the threshold that minimizes actual business cost, because F1 treats both error types as
equally expensive and this domain explicitly does not.

**Graph features for fraud rings.** A single fraudulent transaction often looks unremarkable in
isolation — the giveaway is structural: five "different" accounts sharing one device fingerprint,
or a cluster of cards transacting through the same narrow set of IPs, the shape a ring takes to
spread risk across seemingly-independent identities. Computing this at request time via a live
graph traversal (BFS/connected-components on demand) cannot fit inside a 20ms sub-budget once the
graph reaches hundreds of millions of nodes, so production systems precompute graph metrics
(connected-component size, degree, a heuristic ring-risk score) on a separate streaming or batch
job that incrementally updates the graph as new edges land, and the scoring path only does a
cheap point lookup of an already-computed value. The tradeoff is freshness: a ring assembled in
the last few minutes may not yet be reflected in `component_size` if the graph-update job runs on
a 5-15 minute cycle — the same freshness-vs-latency tradeoff as online-store staleness in
ml-system-design/real-time-ml-pipeline.md, applied to graph structure instead of scalar
aggregates.

**The delayed-label problem.** Chargeback-confirmed fraud labels lag the transaction by 30-90+
days, so the model always trains on a stale slice of what "current" fraud looks like — fraud
patterns evolve to adapt to whatever the current model blocks, so a model trained purely on
chargeback-confirmed labels is structurally behind the adversary. The common mitigation is a
faster, noisier proxy label: transactions confirmed by the manual review team, available within
hours or days rather than months, used as an earlier lower-volume label source blended with the
slower but more complete chargeback labels once those arrive — two label sources at two
different latencies feeding one training pipeline, with `delay_days` tracked explicitly so it's
possible to reason about how much of the recent training window is actually populated versus
still pending.

**Rules-plus-ML ensembling for explainability and audit.** The architecture never lets the ML
model be the sole decision-maker for hard cases: a rules engine layered alongside (or ahead of)
the model handles deterministic, auditable blocks — known-bad device/card blocklists, sanctions-
list matches, hard velocity caps — enforced with certainty rather than a probabilistic score,
partly for security (a model score can drift or be attacked) and partly because some
jurisdictions require declines to carry specific, disclosable reasons, which a black-box score
alone doesn't satisfy. The model's real job is the gray zone the rules don't confidently allow
or block — where a GBDT is favored over a deep model specifically because feature-importance/
SHAP-style attribution maps cleanly onto the `reason_codes` the API has to return, a much harder
property to get out of a large neural network cheaply at serving time.

## Bottlenecks & Scaling

Graph feature computation at low latency and scale is the hardest single piece of this budget —
it's the one feature source that can't simply be sharded by entity key the way scalar rolling
aggregates can, since connected-component computation is inherently a whole-graph property that
a naive per-node shard boundary would fragment; production systems handle this with incremental
graph-update jobs (updating only the affected component when a new edge lands, rather than
recomputing global connectivity from scratch) and accept the resulting few-minutes staleness
window rather than pay for real-time whole-graph recomputation. The online feature store and
rules engine scale the way any high-QPS inline lookup service does — sharded, replicated, with a
hard timeout that fails to a conservative default (route to manual review, not silent approval)
since failing open on a fraud check is a far worse failure mode than falling back to a slower
path. At the training-pipeline level, the delayed-label gap is a persistent structural staleness
the system never fully closes — more transaction volume doesn't fix it, since volume and label
delay are independent axes — so the real scaling lever is faster proxy-label pipelines rather
than forcing chargeback data to arrive sooner, which is bounded by card-network dispute
timelines outside the system's control.
