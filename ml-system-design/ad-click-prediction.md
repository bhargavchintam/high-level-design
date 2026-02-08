# Ad Click-Through Rate (CTR) Prediction

## Requirements

### Functional
- For every ad request (a user loading a page/app surface with an ad slot), predict the
  probability of click (pCTR) for each candidate ad competing for that slot.
- pCTR feeds the auction directly: eCPM = bid × pCTR (roughly), so ranking quality here is
  ranking quality of the entire ads marketplace, not just a UX nicety.
- Serve fresh online features (recent user behavior, recent ad performance) at request time,
  not just features computed in last night's batch job.
- Retrain the model on a regular cadence (hours, not weeks) as click patterns, campaigns, and
  creatives turn over continuously — a stale CTR model misprices the entire auction.

### Non-functional
- Hard latency budget: CTR scoring must complete in <10ms p99, inside a much larger ~100-150ms
  end-to-end ad-serving budget shared with targeting eligibility, the auction itself, and
  creative rendering decisions — CTR scoring is one stage among several, not the whole request.
- 100k+ QPS sustained ad-request volume, each fanning out to scores for many candidate ads —
  the actual (user, ad) scoring throughput is one to two orders of magnitude higher than the
  request rate itself.
- Feature cardinality is enormous and effectively unbounded: user_id × ad_id crosses alone are
  billions of distinct values, so anything resembling a fixed vocabulary/dictionary breaks —
  see the feature hashing deep dive.
- Calibration matters as much as ranking quality: pCTR isn't just used to sort candidates, it's
  multiplied directly into the price the advertiser pays, so a systematically over- or
  under-confident model directly costs or overcharges revenue.

## Capacity Estimation

Assume 8.64B ad requests/day globally → 8.64B / 86,400 = 100,000 requests/sec average, peak
(2.5x diurnal/regional factor) ≈ 250,000/sec — this is the number behind the "100k+ QPS"
requirement.

Each ad request has already been narrowed by eligibility/targeting filters to ~200 candidate
ads before CTR scoring runs (fan-out per request). Total scoring volume = 100,000 req/sec × 200
candidates ≈ 20M (user, ad) scores/sec average, ~50M/sec at peak — this, not the request rate,
is what actually sizes the serving fleet.

Latency: scoring 200 candidates within the 10ms budget only works as one batched forward pass,
not 200 sequential model calls — sequential calls at even 1ms each blow the budget 20x over on
their own. A single batched inference call for 200 candidates on a quantized model typically
runs 2-4ms on CPU/GPU, leaving headroom for feature-store round trips. Fleet sizing: if one
scoring instance sustains ~100k scores/sec (batched, quantized, a few hundred features), average
load (20M/sec) needs ~200 instances; peak (50M/sec) needs ~500, provisioned with multi-AZ
headroom.

Feature store: assume 500M monthly active users × ~200 online features × 8 bytes ≈ 800 GB;
50M active ads/campaigns × ~100 features × 4 bytes ≈ 20 GB — both sharded across an in-memory
KV tier (see concepts/database-sharding.md), since no single-node store serves 20M+ reads/sec.
Embedding tables for hashed high-cardinality IDs add to this: at 2^26 (~67M) buckets, embedding
dim 12, fp32, one hashed feature family is 67M × 12 × 4 bytes ≈ 3.2 GB — a handful of crossed
families lands in the tens-of-GB range, small enough to keep resident on each scoring host
instead of behind a network hop.

Training data: click rate ~1.5% → ~130M clicks/day out of roughly 1.7B logged impressions/day
(impressions actually shown post-auction — not the full 20M/sec of candidates scored, since
most scored candidates lose the auction and are never logged as impressions). At ~200 bytes/
logged row, raw daily log volume ≈ 340 GB/day before negative subsampling, which is applied
before training since raw logs are dominated by non-click rows.

## API Design

```
POST /v1/predict/ctr
  { user_id, context: {device, geo, surface, ts},
    candidates: [{ad_id, campaign_id, creative_id, bid}, ...] }
  -> 200 { predictions: [{ad_id, pctr, calibrated_pctr}], model_version }   (<10ms p99)

POST /v1/events/impression   { request_id, ad_id, rank, won_auction, price, ts } -> 202
POST /v1/events/click        { impression_id, ad_id, ts }                       -> 202

GET  /v1/models/{model_version}/status   -> rollout state, shadow-traffic metrics
```

Impression and click events are fire-and-forget (202, async) — they feed the offline
label-join pipeline, not the synchronous request path, and must never block ad serving.

## Data Model

```
feature_store (online, sharded KV, see concepts/database-sharding.md)
  user_features: user_id -> { recent_ctr_1h/24h, category_affinity[...], device, geo }
  ad_features:   ad_id   -> { campaign_id, advertiser_id, historical_ctr_7d, creative_features }
  cross_features: computed at request time via feature hashing (user_id × ad_id,
                  user_id × category, ad_id × hour_of_day, ...) — not precomputed/stored,
                  the cross space is too large to materialize

embedding_tables (model-owned, resident on scoring hosts)
  hash(feature_value) mod N_buckets -> float16[embedding_dim]

impression_log (offline, append-only)
  impression_id, user_id, ad_id, request_ts, feature_snapshot, rank_shown, price

click_log (offline, arrives with delay relative to impression_log)
  impression_id, click_ts     # absence after the label window closes => negative label

training_examples (offline, generated by delayed join, label window e.g. 24-72h)
  feature_snapshot (as-of impression time) + label (clicked within window: 0/1)
```

## High-Level Architecture

```
 training path:
 impression_log ──┐
                   ├──► delayed-label join (wait window, e.g. 24-72h) ──► training_examples
 click_log ────────┘         (see deep dive: delayed & missing labels)
                                          │
                              feature pipeline (batch + streaming aggregates,
                              writes online feature store)
                                          │
                              model training (LR -> GBDT -> deep model,
                              see deep dive: model evolution)
                                          │
                              calibration fit (Platt scaling / isotonic,
                              on a held-out slice) ──► model + calibration map
                                          │
                                    model registry ──► shadow traffic ──► rollout

 serving path:
 ad request ──► eligibility/targeting filter (candidate generation, ~200 candidates)
                              │
                              ├──► feature store lookup (user + 200 ad features, batched)
                              │
                              ├──► feature hashing (crosses computed on the fly)
                              │
                              ├──► CTR model: one batched forward pass over 200
                              │    candidates (quantized, <10ms p99)
                              │
                              ├──► calibration layer (raw score -> calibrated pCTR)
                              │
                              └──► auction: rank by bid × calibrated_pCTR ──► winner(s)
                                   returned within the ~100-150ms total ad-serving budget
```

## Deep Dives

**Feature hashing and feature crosses.** Raw categorical IDs — user_id, ad_id, campaign_id —
carry almost no signal individually at billions-of-values scale, but their *crosses* (this
specific user × this specific ad, or category affinity × ad category) are where most of the
predictive power in CTR models lives, because they capture interaction effects a linear
combination of individual features can't express. The problem is that crossing two already-huge
ID spaces produces a combinatorially larger one — user_id × ad_id alone is potentially billions
× millions — and an explicit dictionary per distinct value is infeasible, growing unboundedly as
new users/ads appear. The fix is the hashing trick: hash each raw feature value into a
fixed-size bucket space (say 2^24-2^26 buckets) and look up an embedding for the bucket instead
of the raw value. Collisions happen but are tolerable — the model learns around them — and
embedding table size becomes bounded by bucket count, not by the true, unbounded cardinality of
the underlying ID space.

**Calibration.** A model trained with log loss to rank candidates correctly is not guaranteed to
output well-calibrated probabilities — it can be very good at *ordering* ads by likelihood of
click while still being systematically over- or under-confident in the actual numbers, especially
once negative subsampling during training skews the base rate it was fit on. This matters
enormously here specifically because pCTR isn't just a ranking signal, it's multiplied directly
into eCPM and therefore into price — a miscalibrated model doesn't just rank ads slightly wrong,
it misprices the auction, either overcharging advertisers (eroding trust) or underpricing
inventory (leaving revenue on the table). The standard fix is a calibration layer fit *after*
the main model, on a held-out slice of recent traffic: Platt scaling (fit a single logistic
function mapping raw score -> calibrated probability) for a cheap, smooth correction, or
isotonic regression for a more flexible non-parametric mapping when the miscalibration isn't a
simple monotonic distortion. This calibration map is refreshed on its own cadence, often more
frequently than the full model retrain, since calibration drift (base rate shifting) happens
faster than the underlying ranking function decays.

**Delayed and missing labels.** A click can arrive seconds after an impression or, less
commonly, hours later — the label for "was this impression clicked" isn't known at the moment
the impression is logged. Training has to join impression_log and click_log with a wait window
(e.g. 24-72 hours) before a label is considered final, which means the most recent window of
impressions is always partially unlabeled and either excluded from the current training run or
included with a provisional negative label. The latter is more common and creates a mild
positive-unlabeled problem — some impressions labeled negative because their click just hasn't
landed yet are actually latent positives, biasing the model slightly toward under-predicting
CTR for recently-run campaigns. This is generally accepted as a tolerable, bounded bias rather
than solved outright, because waiting for a fully-closed label window on every impression before
training on it would make the pipeline arbitrarily slow to react to genuinely new patterns.

**Model evolution: logistic regression -> GBDT -> deep models.** Logistic regression over
hand-crafted (often hashed and crossed) features was the original workhorse — cheap to train,
trivially fast to serve (a dot product), and easy to reason about, but it can't learn feature
interactions it wasn't explicitly given as a cross, so accuracy is capped by feature engineering
effort. GBDTs improved on this by learning nonlinear feature interactions automatically from
raw and lightly-engineered features, at the cost of higher serving latency (tree traversal
across potentially hundreds of trees) and a training pipeline less naturally incremental than
LR. Deep models (Wide & Deep, DeepFM, DCN and successors) push further: a "wide" component keeps
the memorization benefit of sparse crossed features (similar in spirit to LR) while a "deep"
tower over dense/embedded features learns higher-order interactions the wide side can't express,
and factorization-machine-style or cross-network layers make the crossing itself learned rather
than hand-specified. Each step trades latency for expressiveness — exactly why the <10ms serving
budget, not accuracy alone, constrains which of these a given system can run in the hot path.

**Hitting the <10ms budget.** Three techniques do most of the work. Batching: scoring all ~200
candidates as one batched forward pass instead of 200 sequential calls turns a latency problem
that scales with candidate count into one that's nearly flat up to a batch-size limit.
Quantization: running the model at int8 or fp16 instead of fp32 cuts compute time and memory
bandwidth, typically at a small, measured accuracy cost that's worth it against a hard latency
wall (see concepts/model-serving-patterns.md). Feature caching: recently-computed user features
are cached at the serving edge, so a user issuing several ad requests in a short window doesn't
force a fresh feature-store round trip every time — at 20M+ scores/sec, feature-store latency,
not model-inference latency, is frequently the first thing to blow the 10ms wall.

## Bottlenecks & Scaling

The feature store is the most exposed shared dependency: at 20M+ scores/sec average, every one
of those needs a feature lookup, and a feature-store latency spike affects every in-flight
request simultaneously rather than degrading gracefully — this pushes toward aggressive sharding
by user_id/ad_id (see concepts/database-sharding.md), edge caching of hot features, and a hard
timeout with a stale-or-default-feature fallback rather than blocking the request past budget.
Cold start is a persistent second-order problem on both sides: a brand-new ad has no
historical_ctr_7d yet, and a brand-new user has no behavioral features — both fall back to
coarser priors (campaign-level or category-level average CTR instead of ad-level, demographic
priors instead of user-level history) until enough impressions accumulate for the fine-grained
feature to populate, which is itself a chicken-and-egg problem the exploration budget in the
auction (deliberately serving somewhat-unproven ads to new or thin-history users) exists to
solve.
