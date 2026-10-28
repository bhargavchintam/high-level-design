# Real-Time ML Feature Pipeline

## Requirements

### Functional
- Consume a live event stream (page views, clicks, transactions, app events) and compute
  derived features (rolling counts, aggregates, ratios) in near-real time.
- Materialize computed features into two destinations with different access patterns: an
  online feature store for low-latency serving lookups, and an offline feature store for
  training-set construction.
- Support online/incremental model updates — a model that adjusts its weights as new labeled
  events arrive, not only via periodic full retrains.
- Serve feature vectors by entity ID (user, device, item) to downstream scoring services with
  a tight latency budget, and serve point-in-time-correct feature snapshots to offline training
  jobs.

### Non-functional
- Feature freshness SLA on the order of seconds (a fraud or ranking model scoring "clicks in
  the last 10 minutes" is useless if that aggregate is an hour stale) — this rules out a
  batch-only (hourly/daily) pipeline as the primary path.
- Training-serving skew must be structurally prevented, not just tested for: the exact same
  feature-computation logic must produce the exact same value whether it runs in the streaming
  path or is replayed offline over historical data.
- Aggregation state needs effectively-exactly-once correctness under replay and node failure —
  a duplicate-counted event silently inflates a rolling count feature and both trains and serves
  a wrong signal, with no error thrown anywhere.
- Raw ingest throughput in the low millions of events/sec range, sustained, with the stream
  processing layer scaling independently of both the online store and the offline sink.

## Capacity Estimation

Assume 2M events/sec sustained ingest (clickstream + transaction events across a large
consumer platform), average event size 500 bytes → 2M × 500B = 1 GB/sec raw ingest into
Kafka (see [concepts/message-queues.md](../concepts/message-queues.md) for the broker-side sizing of this — this pipeline is a
consumer of exactly that kind of cluster).

**Stream processing parallelism.** A single stream-processing task (a Flink subtask) handling
a keyed aggregation tops out around 50-100K events/sec depending on aggregation complexity and
state-backend I/O. To absorb 2M events/sec with headroom, that's 2M / 50K ≈ 40 parallel
subtasks minimum, sized up to ~100-150 in practice to leave slack for skewed key distribution
and windowed-state checkpointing overhead — this maps directly to Kafka partition count on the
source topic, since Flink parallelism for a keyed stream is bounded by the number of source
partitions.

**Feature-write throughput to the online store.** Not every raw event produces a feature
write — assume each event triggers updates to ~5 rolling features (a click updates
`clicks_10m`, `clicks_1h`, `clicks_24h` for both the user and the item, roughly) → 2M × 5 = 10M
feature-writes/sec at peak. At a realistic 50-100K writes/sec per online-store shard (a
Redis-class in-memory KV node under small-write load), that's 10M / 75K ≈ 133 shards minimum —
this is the number that actually sizes the online feature store cluster, not entity count.

**State size per key.** A single entity's streaming aggregation state (a handful of sliding
window counters plus a sketch structure for approximate distincts) runs 200 bytes-2 KB
depending on window count and cardinality-estimation structures (HyperLogLog, Count-Min
Sketch). At 500M active entities being tracked concurrently, 500M × 1 KB ≈ 500 GB of live
operator state across the Flink cluster — large enough that this state lives in an embedded
disk-backed state backend (RocksDB) per task, not in-heap, with periodic checkpoints to durable
storage for recovery.

**Offline store volume.** Every materialized feature write is also appended to the offline
store for training. At 10M feature-writes/sec × ~50 bytes/row (entity_id, feature_name, value,
event_time) × 86,400 sec/day ≈ 43 TB/day raw, compacted down substantially once written as
columnar Parquet with per-entity delta encoding — still on the order of several TB/day at rest,
which is the number that drives partitioning strategy (by event date) on the data lake.

## API Design

```
# feature retrieval (serving path, called synchronously by a scoring service)
GET /v1/features?entity_id=...&feature_names=clicks_10m,clicks_1h,avg_txn_amt_24h
    -> 200 { entity_id, features: { clicks_10m: 14, clicks_1h: 82, avg_txn_amt_24h: 41.20 },
             as_of: <timestamp>, staleness_ms: 340 }
    latency target: p99 < 10ms — this sits inline in a caller's own request path

# internal streaming write path (not client-facing)
stream_processor.emit(entity_id, feature_name, value, event_time)
    -> dual write: online_store.set(entity_id, feature_name, value)   (overwrite in place)
                   offline_store.append(entity_id, feature_name, value, event_time)  (immutable)

# offline training-set retrieval (batch, called by training jobs)
POST /v1/offline-features/point-in-time-join
     { entity_ids: [...], feature_names: [...], timestamps: [...] }
     -> a training dataframe: feature values as they existed at each given timestamp,
        not as they exist now
```

The `staleness_ms` field on the serving response is deliberate, not decorative — a caller with
a tight freshness requirement (fraud scoring) needs to know how stale the feature actually is,
not just trust that "the API responded" means "the value is fresh."

## Data Model

```
online_feature_store (key-value, e.g. Redis, keyed by entity_id — mutable, overwritten in place)
  entity_id -> { clicks_10m: 14, clicks_1h: 82, clicks_24h: 310,
                 avg_txn_amt_24h: 41.20, last_updated_ts }
  # analogous in spirit to how case-studies/uber.md keeps only a driver's current position —
  # no history, just the latest computed value per key, sized for lookup speed not durability

offline_feature_store (append-only, columnar, e.g. Parquet on a data lake, partitioned by date)
  entity_id, feature_name, value, event_time, ingestion_time
  # event_time is what point-in-time joins key off of; ingestion_time is kept separately to
  # detect and handle late-arriving events (see watermarks, deep dive below)

feature_registry (metadata, low-volume)
  feature_name -> { computation_logic_ref, owning_team, online_ttl, offline_retention_days }
  # single source of truth for "what does this feature name mean" — both the streaming job
  # and the offline batch-replay job read their transformation logic from the same reference
```

The online store holds only current state — no history — because serving only ever needs "the
feature vector right now." The offline store is the opposite: pure history, immutable, indexed
by event time, because training needs to reconstruct "what did this feature vector look like at
every past point a label was generated."

## High-Level Architecture

```
 event source
 producers ──► Kafka (event stream, partitioned by entity_id) ─┐
                                                                  │
                              ┌───────────────────────────────────┘
                              ▼
                    Stream processor (Flink-style)
                    - keyed windowed aggregation (event-time windows,
                      watermarks for late data)
                    - one shared feature-computation library, invoked
                      both here (streaming) and in the offline replay job
                              │
              ┌───────────────┴───────────────┐
              ▼                                 ▼
   SERVING PATH                        TRAINING PATH
   online feature store                 offline feature store
   (Redis-class KV, overwrite            (Parquet/columnar on data
   in place, entity_id -> vector)         lake, append-only, event-
              │                           time indexed)
              ▼                                 │
   scoring service ──GET /features──►            ▼
   (fraud/ranking/rec model,                point-in-time join engine
   inline in a request path)               (joins historical feature
              │                            snapshots to historical
              ▼                            labels AS OF label time,
   model inference ──► response            not as of query time)
                                                  │
   online/incremental update loop:                ▼
   labeled outcome events ──► streamed        training set
   back into an incremental learner                │
   (e.g. FTRL logistic regression)                 ▼
   updated per-event, deployed             batch training job
   continuously alongside the              (periodic full retrain,
   periodically-retrained batch model      heavier model architecture)
```

## Deep Dives

**Training-serving skew and point-in-time correctness.** The single most common way this
architecture silently breaks: the online path computes `clicks_1h` with one code path
(streaming operator logic) and someone reimplements the "same" feature in a batch SQL query for
the offline training pipeline — the two are never bit-for-bit identical (different window
boundary handling, different null/missing-event semantics), and the model ends up trained on
features that don't match what it sees in production. The fix is architectural, not procedural:
a single feature-computation library, callable both from the live streaming operator and from
an offline batch-replay job that re-runs the exact same logic over historical Kafka data
(rather than a hand-written SQL approximation of it). The second, subtler failure mode is
future leakage in the offline join — naively joining "current feature value" to a historical
label instead of "feature value as it existed at label time" leaks information the model would
never have had in production, inflating offline metrics that then don't materialize online.
This is exactly the point-in-time join problem covered in [ml-system-design/recommendation-system.md](recommendation-system.md)'s
feature-store section, generalized to any streaming-fed model, not just ranking.

**Event time vs. processing time and watermarks.** Windowed aggregation ("clicks per user in
the last 10 minutes") has to answer: 10 minutes according to what clock? Processing time (when
the event arrives at the stream processor) is simple but wrong under any network delay or
consumer lag — a burst of late-arriving events gets attributed to whatever window happens to be
open when they arrive, not the window they actually occurred in. Event time (the timestamp on
the event itself) is correct but requires the system to decide when a window is "done," since
events can arrive out of order or late indefinitely in principle. Watermarks solve this: a
watermark is a heuristic assertion ("no more events with timestamp earlier than T will arrive")
that lets the processor close and emit a window once the watermark passes the window's end,
trading a bounded amount of lateness-tolerance for the ability to ever finalize a windowed
result at all. Events that arrive after the watermark has passed their window are either
dropped or routed to a side output for a slower correction path — a real tradeoff between
freshness and completeness, not a solved problem.

**Online/incremental learning.** A model retrained on a fixed batch schedule (say, nightly)
lags the freshness SLA this pipeline is built to hit — a fraud pattern that emerges mid-day
isn't reflected in the model until the next retrain. Incremental learning algorithms (FTRL —
Follow-The-Regularized-Leader — is the canonical choice for online logistic regression) update
model weights per-event or per-mini-batch as labeled outcomes stream in, keeping the model
continuously current. The tradeoff is stability: a model updated on every single labeled event
has much higher variance in its weights (one bad batch of mislabeled or adversarial events can
swing it) than a model retrained periodically over a large, deduplicated, quality-controlled
batch. Production systems commonly run both: a heavier periodically-retrained batch model as
the stable baseline, with a lightweight incremental model either used for fast-moving auxiliary
signals or blended with the batch model's output, rather than fully replacing the batch-trained
model's stability with the online model's freshness.

**Dual online/offline store architecture.** Maintaining two physically separate stores for the
same logical features (rather than one store serving both needs) is deliberate, not
duplicated effort: the online store is optimized for point-lookup latency (single-digit
milliseconds, key-value, no history) and the offline store is optimized for large-scale
historical scan and join (columnar, immutable, indexed by event time) — these are conflicting
storage engine requirements, and no single store is good at both. The hard part isn't running
two stores, it's keeping them semantically consistent — the dual write from the stream
processor (online: overwrite; offline: append) needs to be atomic-enough that a crash between
the two writes doesn't leave the stores permanently disagreeing, which is usually handled by
making the offline write the durable source of truth (replayable from Kafka) and treating the
online store as a lossy, rebuildable cache of the latest value.

## Bottlenecks & Scaling

Hot keys in the streaming aggregation are the most common production incident in this
architecture: a viral post, a bot account hammering an endpoint, or a single high-volume
merchant account concentrates a disproportionate share of events onto one partition key,
overloading the single Flink subtask responsible for that key's state and creating backpressure
that stalls the whole pipeline behind it — the same hot-partition failure mode covered in
[concepts/database-sharding.md](../concepts/database-sharding.md) and [concepts/message-queues.md](../concepts/message-queues.md), showing up here in a stream
processor instead of a broker or a shard. Mitigations mirror those domains: key salting for
known-hot entities, or a two-stage aggregation (local pre-aggregation across a few sub-keys,
merged in a second stage) to spread the load. Flink operator state grows monotonically with
the number of actively-tracked entities and the number/width of windows per entity — state
size is the reason RocksDB-backed state (spills to local disk, not pure in-heap) is the default
for any deployment past a few hundred GB of live state, and checkpoint time (durably snapshotting
that state) scales with it too, so state size indirectly caps how large a Flink job can grow
before checkpoint duration itself becomes an availability risk. On the serving side, online-store
write throughput — not read throughput — is usually the binding constraint, since every one of
millions of events/sec fans out into several feature writes; this is the reason the online store
is sharded aggressively (as sized in the capacity section) well ahead of when read QPS alone
would justify it.
