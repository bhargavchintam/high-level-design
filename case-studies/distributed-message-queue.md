# Distributed Message Queue

## Requirements

### Functional
- Producers publish messages to a topic/queue; consumers read them.
- Support both work-queue semantics (each message consumed once, by one of many competing
  consumers) and pub/sub semantics (each message delivered to every subscriber).
- Messages persist until consumed (or until a retention window expires), surviving broker
  restarts.
- Preserve ordering within a partition/key.

### Non-functional
- High sustained throughput (this is a Kafka-class design problem).
- Durability: an acknowledged write must not be lost, even across a broker crash.
- At-least-once delivery by default; consumers should be able to build exactly-once semantics
  on top via idempotency.
- Horizontal scalability on both the producer and consumer side, independently.

## Capacity Estimation

Target: 1M messages/sec sustained ingest, average message size 1 KB (typical event/log
payload).

Throughput: 1M msg/sec × 1 KB = 1 GB/sec of write throughput the cluster needs to absorb.
A single disk can sustain roughly 100-200 MB/sec sequential write; to hit 1 GB/sec across the
cluster with headroom, you need at minimum 5-10 broker nodes purely from disk I/O math, more
in practice once you add replication overhead (each write is replicated to 2+ other brokers,
roughly tripling total disk I/O across the cluster to ~3 GB/sec of aggregate write traffic).

Storage: with a 7-day retention window (a common default), 1M msg/sec × 1 KB × 86,400 sec/day
× 7 days ≈ 604 TB of retained data, before replication; at 3x replication, ~1.8 PB across the
cluster. This is the number that dictates broker disk sizing and partition count — it's a
genuinely large amount of always-on storage, which is why retention windows and log
compaction (deep dive below) matter so much for cost.

Partition count: to parallelize 1M msg/sec across consumers, and given a single partition
tops out around 10-50 MB/sec of throughput on typical hardware, you need on the order of
50-100+ partitions per topic minimum to spread that load, sized up further based on target
consumer parallelism (partition count is also the hard ceiling on how many consumers in one
consumer group can be active in parallel).

## API Design

```
# producer
publish(topic, partition_key, payload) -> { offset, partition }

# consumer
subscribe(topic, consumer_group) 
poll() -> [ messages ]              # pull-based, not push
commit(offset)                      # mark progress, per partition
```

Pull-based consumption (the consumer asks for more, rather than the broker pushing) is a
deliberate choice — it lets each consumer control its own pace and avoids the broker having to
track per-consumer backpressure state, which push-based systems need.

## Data Model

The core structure is a log, not a table:

```
topic "orders"
  partition 0: [msg0, msg1, msg2, msg3, ...]   append-only, offset-indexed
  partition 1: [msg0, msg1, msg2, ...]
  partition 2: [msg0, msg1, msg2, ...]

consumer_group_offsets
  (group_id, topic, partition) -> last_committed_offset
```

Each partition is an append-only, immutable sequence of messages on disk, addressed by a
monotonically increasing offset. Consumers don't get messages pushed to them and don't cause
messages to be deleted on read — a message stays in the log until its retention window expires,
independent of whether any consumer has read it, which is what lets multiple independent
consumer groups replay the same stream from different points.

## High-Level Architecture

```
 producers ──► Broker (leader for partition P) ──replicate──► Broker (follower)
                        │                                  ──replicate──► Broker (follower)
                        │
                append to partition log on disk,
                ack once replicated to enough
                followers (durability setting)

 consumers (group A) ──poll──► Broker ──► read from partition log at committed offset
 consumers (group B) ──poll──► Broker ──► read same log, independent offset, unaffected
                                            by group A's progress

 Coordination (partition leadership, broker membership, consumer group
 assignment) handled by a separate consensus layer (Zookeeper historically,
 or a built-in Raft-based controller in newer Kafka/Kafka-like systems).
```

Each partition has one leader broker at a time; producers and consumers for that partition
talk to the leader; followers replicate for durability and take over on leader failure.
Different partitions of the same topic can have their leaders spread across different
brokers, which is how the system parallelizes both writes and reads across the cluster.

## Deep Dives

**Partitioning and ordering.** Ordering is only guaranteed within a partition, never across
an entire topic — this is the load-bearing tradeoff that makes horizontal scaling possible at
all. Producers pick a partition per message, typically by hashing a key (e.g. `order_id`), so
all messages for the same key land in the same partition and are therefore strictly ordered
relative to each other, while messages for different keys can be processed out of order
relative to one another with no correctness impact. Choosing a low-cardinality or skewed key
(e.g. partitioning by a boolean flag) creates a hot partition — the same hot-key problem that
shows up in sharded databases and caches.

**Replication and durability.** Each partition is replicated across N brokers (commonly 3).
Writes can require acknowledgment from just the leader (`acks=1`, fast, small risk of loss on
leader failure before followers catch up) or from a quorum/all in-sync replicas (`acks=all`,
slower, durable even through a leader crash). This is the same latency/durability tradeoff
that shows up in concepts/replication-and-consistency.md, applied to a log instead of a table.

**Consumer groups and rebalancing.** Multiple consumers in the same group split the
partitions of a topic between them — each partition is owned by exactly one consumer within
a group at a time, which is what gives work-queue-style load distribution. When a consumer
joins or leaves the group, partitions get reassigned (a rebalance), which briefly pauses
consumption for the affected partitions — frequent rebalances (e.g. from consumers crash-
looping) are a common real-world source of consumer lag spikes, not just a theoretical
concern.

**Exactly-once in practice.** True end-to-end exactly-once across a network is not really
achievable without cooperation from the consumer. The standard pattern: broker guarantees
at-least-once delivery (a message might be redelivered after a consumer crash before it
committed its offset), and the consumer makes its own processing idempotent — e.g. keyed by
message ID, so reprocessing the same message twice has no additional effect (an upsert instead
of an insert, a dedup table, etc.). Systems that claim built-in exactly-once (Kafka's
transactional API) deliver this only within their own ecosystem (broker-to-broker,
producer-to-broker); the moment a consumer writes to an external system (a database, an
API call), that external write still needs its own idempotency.

**Log compaction.** For topics representing current state rather than a pure event stream
(e.g. "latest known address per user_id"), retention by time doesn't make sense — you want to
keep the latest message per key forever and drop older messages for the same key. Log
compaction runs a background process that rewrites each partition, keeping only the most
recent message per key, which bounds storage growth for state-representing topics
independent of message volume over time.

## Bottlenecks & Scaling

Partition count is the primary lever for both throughput and consumer parallelism, but it's
not free to increase after the fact — repartitioning an existing topic changes which broker
and which consumer owns which keys, similar in spirit to resharding a database (see
concepts/database-sharding.md), so partition count is usually set generously up front based on
target peak throughput rather than grown incrementally. Disk I/O per broker is the physical
ceiling on a single node's contribution — sequential writes (the log-append pattern) are what
make this tractable on spinning disks and even more so on SSDs, and is a big part of why
log-structured storage (see concepts/indexing.md on LSM-trees) rather than in-place updates
is the standard implementation choice for message brokers.
