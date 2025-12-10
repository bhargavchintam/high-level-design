# Feature Store

## Requirements

### Functional
- Define a feature once (a named, versioned transformation from raw events/tables to a model
  input value) and make it usable both in offline training and online serving without
  reimplementing the transformation twice.
- Serve low-latency feature lookups for online inference (a batch of features for one or more
  entities, on the request path of a model-serving call).
- Generate point-in-time-correct training datasets — for a given labeled example at timestamp
  T, join in feature values as they existed at T, not as they exist now.
- Support both batch-computed features (aggregates recomputed periodically) and streaming
  features (updated continuously as events arrive), unified under one lookup interface for
  consumers.

### Non-functional
- Online read latency p99 < 10ms for a batch lookup of ~100-200 features across one or a few
  entity keys — this is on the hot path of every model inference call and directly adds to
  serving latency budgets described in the recommendation and ranking case studies.
- Point-in-time correctness is a hard correctness requirement, not a performance target — a
  violation doesn't crash anything, it silently inflates offline eval metrics that then fail to
  hold online (label leakage), which is worse than a visible failure because it isn't caught by
  normal monitoring.
- Training-serving skew must be minimized: the same feature, requested by the same entity at
  the same logical time, should return the same value whether computed via the offline batch
  path or the online path, modulo intentional freshness differences that are explicitly
  documented per feature.
- Freshness SLA is per-feature, not global — some features (a 30-day rolling purchase count) are
  fine refreshed hourly; others (items added to cart in the last 5 minutes) are only useful if
  updated within seconds.

## Capacity Estimation

Assume 500 registered feature definitions, serving 100M entities (users), consumed by
~30 downstream models with a combined online serving load of 50K requests/sec, each request
fetching ~100 features across 1-3 entities (e.g. one user + a handful of candidate items).

Online store read volume: 50K req/sec × 100 features ≈ 5M feature-value reads/sec. At a typical
per-feature value size of ~8 bytes (numeric) or ~50 bytes (small string/embedding-bucket), total
online read bandwidth lands in the few-hundred-MB/sec range — well within what a sharded
in-memory KV store (Redis-cluster-style) sustains, provided reads are batched per-request rather
than issued as 100 separate round trips.

Online storage footprint: 100M entities × 500 features × ~16 bytes average ≈ 800 GB — sharded
across enough nodes that each shard's working set fits comfortably in memory with room for
replication.

Streaming feature updates: assume 200M raw events/day feed streaming aggregations (clicks,
purchases, impressions) → 200M / 86,400 ≈ 2,300 events/sec average, ~7,000/sec peak, processed
by a stream aggregation layer that maintains rolling windows (1h, 24h, 7d counters) and writes
updates into the online store — this write path is a small fraction of the online store's total
load compared to reads, consistent with feature stores being read-heavy by a wide margin (reads
outnumber writes by roughly 700:1 in this estimate).

Offline storage: raw event history + computed feature snapshots retained for training data
generation, at petabyte scale over a few years of history for a platform this size — stored in a
columnar format (Parquet) in a data lake, not a row store, since offline access is
scan-and-join-heavy rather than point-lookup-heavy.

## API Design

```
POST /v1/online/get-features
     { entity_keys: [{entity_type: "user", id: "..."}, ...], feature_refs: ["fs:feature_name", ...] }
     -> 200 { values: { entity_key -> { feature_name -> value, event_timestamp } } }

POST /v1/offline/get-historical-features
     { entity_df: [{entity_id, event_timestamp, label}, ...], feature_refs: [...] }
     -> 202 { job_id }    (async — produces a point-in-time-joined training dataset,
                           typically materialized to the data lake, not returned inline)

POST /v1/feature-definitions
     { name, source, transformation, entity, ttl, owner }
     -> 201   (registers a feature; online/offline compute pipelines pick it up)
```

The offline endpoint is asynchronous and job-based, not a synchronous query — a point-in-time
join across hundreds of features and millions of labeled rows is a distributed batch job
(effectively a large multi-way join), not something that returns in a request/response cycle
like the online path.

## Data Model / Feature Schema

```
feature_definitions (registry, source of truth for every feature)
  name, entity_type, source_table/stream, transformation_logic,
  value_type, ttl, freshness_sla, owner_team

online_store (keyed by entity_id, one row/entry per entity per feature group)
  entity_id -> { feature_name: value, event_timestamp: ts }
  (event_timestamp is the timestamp the value reflects, distinct from the write time —
   this distinction is what point-in-time correctness is built on)

offline_store (columnar, partitioned by date, append-only feature log)
  entity_id, feature_name, value, event_timestamp, ingested_at

training_dataset (materialized output of a point-in-time join)
  entity_id, label, event_timestamp, [feature_name: value AS OF event_timestamp, for
  every requested feature]
```

The critical field in this schema is `event_timestamp` on every stored feature value — without
it, there is no way to answer "what was this feature's value at the time this training example
was labeled," which is the entire point of the offline store existing as an append-only log
rather than a table that's just overwritten in place on every update.

## High-Level Architecture

```
 write / feature computation path:
 raw events (clickstream, transactions) ──► Stream processor (windowed aggregations,
                                              e.g. Flink/Spark Streaming)
                                                        │
                        ┌───────────────────────────────┴──────────────────────────┐
                        ▼                                                          ▼
              Online store (KV, low-latency,                          Offline store (data lake,
              sharded by entity_id, TTL'd)                            append-only, partitioned
                                                                        by event date)
                        ▲
                        │
              Batch feature jobs (periodic, e.g. hourly/daily
              aggregates computed from the warehouse, written
              to both online and offline stores for consistency)

 online serving (read) path:
 model-serving call ──► get-features(entity_keys, feature_refs)
                              │
                              ▼
                    Online store batched lookup (single round trip,
                    fans out to relevant shards, merges response)
                              │
                              ▼
                    features returned to the model as input tensor

 offline training (read) path:
 training job ──► get-historical-features(entity_df, feature_refs)
                              │
                              ▼
                Point-in-time join engine: for each (entity_id, event_timestamp,
                label) row, look up the most recent feature value AS OF that
                timestamp (not the latest value) from the offline store
                              │
                              ▼
                Materialized training dataset, land in data lake for the
                training job to consume
```

## Deep Dives

**Point-in-time correctness.** The canonical failure mode this store exists to prevent: given a
training example "user X clicked item Y at time T," the naive approach joins in whatever the
feature values are *right now* (at training-job-run time, which could be weeks after T). If a
feature like "user's total purchase count" has grown since T, the model trains on a value it
never would have seen at serving time for that example — a subtle form of label leakage, since
the feature now indirectly encodes information correlated with the outcome that occurred after
T. The offline store's append-only, timestamped design directly enables the fix: the join engine
looks up, for each training row, the feature value whose `event_timestamp` is the latest one
that is still `<= T`, not the latest value that exists in the table. This is why the offline
store cannot simply be "the online store, but bigger" — the online store intentionally overwrites
old values (it only needs to answer "what's the value now"), while the offline store must retain
history specifically to answer "what was the value at T" for arbitrary past T.

**Training-serving skew.** Even with point-in-time correctness solved, skew creeps in when the
online and offline computation paths for the *same* feature diverge — e.g. a rolling
"clicks in last 1 hour" feature computed by a streaming job online but recomputed by a
differently-windowed batch job offline (different window boundaries, different handling of
late-arriving events). The standard mitigation is defining the transformation once, in one
shared library or declarative spec, and having both the streaming and batch compute paths
execute that same definition rather than two independently-maintained implementations that
happen to compute "the same" feature. Skew is detected in practice by periodically comparing
online-served values against offline-computed values for the same entity/timestamp and alerting
on divergence past a threshold, since it's otherwise invisible until a model's live performance
quietly diverges from its offline eval.

**Freshness vs. cost tradeoff.** Streaming-computed features are more expensive to maintain
(continuous compute, stateful stream processing infrastructure) than batch-computed ones
(periodic job, simpler infra, cheaper). Not every feature needs streaming freshness — a feature
like "account age in days" barely changes and is fine recomputed daily in a batch job, while
"items viewed in this session" is nearly useless if not near-real-time. The feature store
exposes freshness as a per-feature-definition property specifically so this tradeoff is made
deliberately per feature rather than defaulting either to "everything streaming" (expensive,
often for no quality benefit) or "everything batch" (breaks any feature that actually needs
recency).

**Entity key design and fan-out.** A serving request scoring 500 ranking candidates (as in the
recommendation-system case study) needs features for the user once and features for all 500
candidate items — the online store's API is designed around batched multi-entity lookups
precisely so this is one fanned-out round trip to the relevant shards, not 500 sequential
lookups, which would blow the 10ms latency budget by orders of magnitude. This pushes a real
design constraint onto the online store's sharding scheme: it needs to shard in a way that a
typical batch lookup (one user + N items) doesn't have to fan out to every shard in the cluster,
which usually means sharding item features and user features by their own entity ID independent
of each other, letting the caller's batched request fan out only to the shards actually holding
the requested keys.

**TTL and staleness.** Online-store entries carry a TTL — a feature not refreshed within its
freshness SLA should be treated as stale (served with an explicit staleness flag, or omitted and
defaulted) rather than served indefinitely as if current, since a serving path silently using a
week-old "real-time" feature is a worse failure mode than a visible cache miss the calling model
can fall back from.

## Bottlenecks & Scaling

The online store is the latency-critical path and the first thing to need sharding as entity
count or feature count grows — it's a pure key-value access pattern, so it scales
straightforwardly by adding shards, provided the entity-key sharding scheme avoids hot keys (a
viral item receiving disproportionate feature-read traffic needs the same hot-key mitigations —
replication or local caching — as any high-skew KV workload). The point-in-time join for offline
training dataset generation is the component most likely to become a batch-processing
bottleneck as the number of requested features and the size of the label set both grow — it's
effectively a large distributed multi-way join and scales with the product of those two
dimensions, which is why materializing and reusing common training datasets (rather than
re-running the full join for every experiment) is a common cost optimization once this becomes
expensive. Stream processing infrastructure for streaming features has to scale with raw event
volume independent of both of the above, and is usually the first component to need
backpressure/load-shedding logic when upstream event volume spikes beyond provisioned capacity.
