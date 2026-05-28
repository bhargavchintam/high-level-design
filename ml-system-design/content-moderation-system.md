# Multimodal Content Moderation System

## Requirements

### Functional
- Classify every piece of uploaded content — text, image, video, and audio — against a policy
  taxonomy (harassment, graphic violence, spam, legal-risk categories, misinformation, etc.),
  producing per-category confidence scores.
- Route content to an enforcement action (allow, remove, demote in distribution, add a warning
  label) automatically when confidence is high enough, or to a human review queue when it isn't,
  weighted by severity.
- Support escalation: a reviewer can flag a case as ambiguous or high-stakes and route it to a
  more senior review tier or a policy specialist rather than resolving it at the first tier.

### Non-functional
- Two distinct latency budgets: a hard, low-latency budget (tens of milliseconds) for
  pre-publish checks that block posting outright for severe, legal-risk categories, versus a
  much looser async budget (seconds to minutes) for a deeper multimodal scan after content is
  already live.
- Upload volume is massive and skewed heavily toward text and images, with video the smallest
  by item count but by far the largest by compute and storage cost per item.
- Content is genuinely multimodal — a post's risk often lives in the combination of an image
  and its caption, not in either signal alone — which the classification architecture has to
  account for without making every upload wait for the slowest modality.
- Human reviewer throughput is a hard, close-to-fixed capacity constraint: unlike compute, which
  scales by spending more money, the items a finite reviewer headcount can triage per day has a
  real ceiling the system must actively protect rather than assume flexes with volume.

## Capacity Estimation

Assume a platform with 480M content items uploaded/day, split roughly: 300M text posts/day,
150M images/day, 20M videos/day, 10M audio/other/day.

Pre-publish classifier QPS: every item needs at least the fast hard-block check before it's
allowed to post → 480M / 86,400 ≈ 5,560/sec average, ~16,700/sec peak (3x diurnal factor) —
the hot-path number the low-latency classifier fleet has to be sized for.

Video compute cost: exhaustive frame-by-frame scoring is not viable. Sampling ~1 frame every 2
seconds of an average 60-second video → 30 frames/video × 20M videos/day = 600M frames/day
scored by the image classifier → 600M / 86,400 ≈ 6,940 frames/sec average — comparable in
volume to the entire text+image pre-publish load despite video being <5% of upload count by
item, which is why frame sampling rather than full-resolution frame-by-frame scanning is the
difference between a tractable and an intractable compute bill (see deep dive).

Reviewer throughput: assume a review workforce of 2,000 reviewers, each sustainably reviewing 1
item every ~90 seconds across an 8-hour shift (accounting for breaks and slower, more ambiguous
cases) → ~40 items/hour × 8 ≈ 320 items/reviewer/day → 2,000 × 320 = 640,000 items/day of
total review capacity.

Review-routing volume: assume ~2% of all uploads are routed to human review (the confidence
band the automated classifier can't resolve, plus anything above a severity threshold) → 480M
× 2% ≈ 9.6M items/day requiring review — roughly 15x the 640K/day reviewer capacity. This gap
is structural, not a temporary staffing shortfall, and is the reason the review queue has to be
aggressively prioritized (see deep dive) rather than processed FIFO: at this ratio, FIFO means
the median item waits days, by which point most of the harm has already happened.

Storage: video dominates — 20M videos/day × ~50 MB average compressed size ≈ 1 PB/day raw
ingest, versus 150M images/day × ~500 KB ≈ 75 TB/day and 300M text posts/day at negligible size
by comparison. Model scores and review metadata are tiny relative to the raw content and are
stored separately, keyed by content_id, so re-scoring or re-reviewing never requires re-fetching
the original blob unless a human reviewer opens the case.

## API Design

```
POST /v1/moderate/sync         { content_id, modality, text? / blob_ref, uploader_context }
     -> 200 { verdict: allow|block, categories: [...], confidence, latency_ms }
     # pre-publish path — hard-block categories only, tight latency budget

POST /v1/moderate/async        { content_id, modality, blob_ref, context }
     -> 202 { accepted: true }
     # deeper multimodal scan; result delivered via callback or polled

GET  /v1/moderate/{content_id}/result
     -> { categories: [{name, score, model_version}], action, routed_to_review: bool }

GET  /v1/review-queue/next?reviewer_id=...
     -> next highest-priority item for this reviewer (not FIFO — see deep dive)

POST /v1/review/{content_id}/decision   { verdict, category, reviewer_id, notes }
     -> 200  # triggers enforcement action + feeds back into training data
```

Splitting `sync` and `async` at the API level, rather than one endpoint with a variable response
time, makes the latency contract explicit to callers — the client blocking on publish only ever
waits on the sync path, and the async path's result is consumed as a follow-up enforcement
action against already-live content rather than something the upload flow waits on.

## Data Model

```
content_store
  content_id (PK), modality, blob_ref / text, uploader_id, uploaded_at,
  status: live|removed|demoted|pending_review

model_scores
  content_id, category_id, modality_model_score, fused_score, model_version, scored_at
  # per-modality scores plus a fused multimodal score, see deep dive

review_queue
  content_id, priority_score (fn of expected_reach x severity_tier x score_uncertainty),
  assigned_reviewer_id, status: queued|in_review|resolved, enqueued_at

policy_taxonomy
  category_id, name, severity_tier, enforcement_action_by_confidence_band,
  training_label_definition, requires_pre_publish_block: bool

review_decisions (feeds back into training)
  content_id, reviewer_id, final_verdict, category, decided_at
  # becomes a labeled training example for the next model iteration
```

`priority_score` is recomputed as `expected_reach` changes — a piece of content that starts
gaining rapid engagement while sitting in the queue gets promoted — which is why the review
queue is a live priority structure rather than a static ordering assigned once at enqueue time.
`content_store` is sharded by content_id (see concepts/database-sharding.md) since access is
almost always a point lookup by id, never a range scan.

## High-Level Architecture

```
 training path:
 human review decisions ──┐
 (labeled ground truth)   │
                          ▼
 raw content samples ──► per-modality classifier training (text model, image model,
 (+ external labeled       video/audio model trained separately — independently
  policy datasets)         deployable and debuggable per modality)
                                        │
                          fusion layer: combines per-modality scores + light
                          cross-modal features (does caption contradict image?)
                          into a fused risk score ──► model registry

 serving / enforcement path:
 upload ──► fast sync classifier (hard-block categories only,
            small model, tight latency budget) ──► block outright? ──yes──► rejected
                       │ no (or not a hard-block category)
                       ▼
            content goes live ──► queued for async deeper scan (per-modality models +
                                    fusion layer, frame-sampled for video — consumer
                                    pattern per case-studies/distributed-message-queue.md)
                                              │
                              confidence / severity routing:
                       ┌──────────────────────┼───────────────────────┐
                       ▼                       ▼                       ▼
                 high-confidence          uncertain /              low risk
                 + high severity          mid-confidence            → allow,
                 → auto-remove/demote     → prioritized human       no action
                 (no human needed)          review queue
                                                    │
                                          reviewer decision ──► enforcement
                                          action applied ──► fed back as
                                          labeled training data
```

## Deep Dives

**Multimodal classification architecture.** Most production systems start with separate
per-modality models (a text classifier, an image classifier, a video/audio classifier) plus a
lightweight fusion layer that combines their output scores, rather than a single model trained
jointly end-to-end on raw multimodal input. The reason is operational, not accuracy: per-modality
models can be retrained, evaluated, and rolled back independently — a regression in the image
model doesn't force a redeploy of the text model — and each has its own data pipeline, labeling
process, and a much simpler debugging story when a specific category starts misfiring. A single
joint model can in principle capture richer cross-modal interactions (a sarcastic caption
undercutting a benign image), but it couples every modality's release cycle together and makes
root-causing a regression far harder, which is why the fusion-of-independent-models pattern
dominates in practice; a joint model, if used at all, tends to be a later refinement layered on
top rather than the initial architecture (see also concepts/model-serving-patterns.md for
versioning and independent-deploy patterns generally).

**Tiered latency design.** Running the full multimodal pipeline (per-modality models plus
fusion, frame-sampled video) against every upload before allowing it to post is incompatible
with a tens-of-milliseconds publish latency budget, and most content doesn't need that level of
scrutiny before going live. The system splits into a small, fast, high-precision model that
gates publish for a narrow set of severe/legal-risk categories only — where even brief exposure
is unacceptable — and a slower, more thorough async scan that runs against everything after it's
already live, covering the much broader taxonomy. This mirrors the sync-score / async-deeper-
analysis split in ml-system-design/fraud-detection-system.md: a cheap model gates the
latency-critical path, a heavier model runs after the fact against everything. The tradeoff is a
brief exposure window for lower-severity violations, accepted so no upload is held hostage to
the slowest, heaviest model in the pipeline.

**Human-in-the-loop design.** Three mechanisms keep the review queue tractable given fixed
reviewer capacity and unbounded demand. Confidence-threshold routing: only content in the
genuinely uncertain score band, or above a severity threshold regardless of confidence, reaches
a human — anything the model is confidently right about, in either direction, is auto-actioned.
Queue prioritization by expected reach/virality rather than FIFO: an item with a thousand views
waiting behind an item about to go viral is the wrong order to review in, so `priority_score`
actively reorders the queue as reach signals change, not just at enqueue time. Active learning
for the labeling pipeline: rather than randomly sampling content for reviewers to label as new
training data, the system preferentially samples the cases the current model is most uncertain
about, since those examples move the decision boundary the most per labeling dollar spent —
random sampling mostly relabels cases the model already gets right.

**Adversarial evasion.** Unlike a stable prediction task, content moderation faces an adversary
that actively iterates against the deployed filter — slightly modified images, coded-language
substitutions, borderline framing designed to sit just under the auto-action threshold. A
classifier that performed well at launch degrades over time purely because bad actors probe and
adapt to it, not because the underlying data distribution drifted on its own. The classifier
can't be treated as a static artifact retrained on a slow, infrequent cadence the way a more
stable task might be — it needs a continuous retraining loop fed by fresh reviewer decisions
(`review_decisions` in the data model) and regular adversarial red-teaming to find and patch
evasion patterns before they scale.

## Bottlenecks & Scaling

Human review capacity is the ultimate constraining resource in the entire system, and it does
not scale the way everything upstream of it does. Compute for classifiers is a budget problem —
more GPUs, more throughput, roughly linearly. Reviewer throughput is bounded by real headcount,
hiring and training lead time, and the toll of the work itself, none of which flex on the
timescale of a traffic spike. Every design choice upstream — the confidence threshold deciding
what reaches a human, the fusion model's precision, the priority function — exists specifically
to protect that fixed ceiling, the opposite of most scaling problems in this repo where the
bottleneck can eventually be solved by throwing hardware at it (compare to the ranking-stage GPU
fleet in ml-system-design/recommendation-system.md, which scales with spend; this one doesn't).

Video is the second bottleneck, on the compute and storage side. At 1 PB/day raw ingest and
600M scored frames/day even under aggressive sampling, exhaustive frame-by-frame scanning would
multiply both numbers by roughly the frames-per-second of the source video — completely
intractable at this volume. Frame sampling (score a handful of representative frames rather than
every frame) trades a small amount of recall — a violation appearing only in an unsampled frame
is missed by the automated pass — for a compute bill that's actually payable, and is one of the
few places in the system where the tradeoff is explicitly "cheaper and slightly less complete"
rather than "cheaper and equally correct."
