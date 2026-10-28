# Distributed Key-Value Store

## Requirements

### Functional
- `put(key, value)`, `get(key)`, `delete(key)` — that's the entire API surface.
- Values are opaque blobs to the store (no schema, no query-by-value).

### Non-functional
- Horizontally scalable: adding nodes increases capacity and throughput linearly.
- Highly available: writes and reads should succeed even during node failures or network
  partitions (this is explicitly an AP system — see [concepts/cap-theorem.md](../concepts/cap-theorem.md)).
- Low, predictable latency: single-digit milliseconds for get/put at p99.
- Tunable consistency: some use cases want strong consistency at the cost of latency, others
  want to always accept writes.
- This is the Dynamo/Cassandra design problem, not a "wrap Postgres" problem — the interesting
  parts are partitioning, replication, and conflict resolution without a central coordinator.

## Capacity Estimation

Target: 10M writes/sec and 50M reads/sec at peak, across a cluster (this is Cassandra/DynamoDB
class scale — a platform backing many services, not a single app). Average value size 1 KB,
average key size 50 bytes.

Storage: assume 500B live keys at 1 KB average value ≈ 500 TB raw. With a replication factor
of 3 (standard for durability against node loss), that's 1.5 PB across the cluster.

Per-node capacity: a reasonable node with SSDs and 64 GB RAM might handle ~50K ops/sec and
hold ~5-10 TB of data comfortably. To hit 1.5 PB total storage at 10 TB/node → ~150 nodes
minimum from storage alone; to hit 60M combined ops/sec at 50K ops/sec/node → ~1,200 nodes
from throughput. Throughput, not storage, is the harder constraint at this scale — this is
typical for KV stores: you run more, smaller nodes than storage math alone would suggest, to
keep per-node request load down.

## API Design

Deliberately minimal — this is the whole point of a KV store:

```
PUT   /v1/kv/{key}    body: raw bytes or JSON blob, optional TTL
GET   /v1/kv/{key}    -> value + version/vector-clock metadata
DELETE /v1/kv/{key}

# consistency knob, if exposed to clients:
GET /v1/kv/{key}?consistency=quorum|one|all
PUT /v1/kv/{key}?consistency=quorum|one|all
```

## Data Model

There isn't one, by design — the value is opaque. What the store manages internally per key:

```
key                 partition key, hashed to a ring position
value               opaque bytes
vector_clock / version   for conflict detection under concurrent writes
replica_set         the N nodes (from consistent hashing) that hold this key
```

## High-Level Architecture

```
        client
          │
     ┌────▼─────┐
     │ Coordinator│   (any node can act as coordinator for any request —
     │   node     │    no leader, no single point of failure)
     └────┬───────┘
          │  hash(key) -> ring position -> walk to next N nodes clockwise
          │
   ┌──────┼──────┬─────────────┐
   ▼      ▼      ▼             ▼
 node A  node B  node C   ...  node N     (consistent hash ring,
   │      │      │               each key replicated on N=3
   │      │      │               consecutive nodes on the ring)
```

Any node in the cluster can receive a request and act as coordinator — it looks up which
nodes own the key via consistent hashing and forwards the operation to them. There is no
leader election, no single write bottleneck; this is the defining trait of a leaderless,
Dynamo-style design (see [concepts/replication-and-consistency.md](../concepts/replication-and-consistency.md)).

## Deep Dives

**Partitioning.** Keys are distributed across nodes with consistent hashing plus virtual nodes
(see [concepts/consistent-hashing.md](../concepts/consistent-hashing.md)) so adding or removing a node only remaps `~1/N` of keys
instead of reshuffling everything. Each key's replica set is the N nodes encountered walking
clockwise from the key's ring position.

**Quorum reads/writes.** With replication factor N=3, define write quorum W and read quorum R.
Setting W=2, R=2 (W+R > N) guarantees every read overlaps at least one replica that has the
latest write, giving strong-ish consistency without a leader. Setting W=1, R=1 maximizes
availability and latency at the cost of possibly reading stale data. This knob is usually
exposed per-operation, not fixed system-wide — a session token write might want W=3 (must be
durable everywhere before confirming), a metrics counter might accept W=1.

**Conflict resolution.** With no leader, two coordinators can both accept a write for the same
key during a partition, producing conflicting versions. Vector clocks track causal history per
key so the system can tell "B is a later version of A" (auto-resolve) apart from "A and B were
concurrent, unrelated writes" (genuine conflict). Genuine conflicts get resolved either by
last-write-wins (simplest, silently drops one write — acceptable for caches, bad for anything
where data loss matters) or are handed back to the application to merge (Dynamo's original
approach — shopping cart merges are the canonical example).

**Hinted handoff and read repair.** If a node that should receive a write is temporarily down,
a neighboring node accepts the write on its behalf with a "hint" to forward it once the
original node recovers — keeps writes succeeding during transient failures without waiting for
a quorum among only-live-nodes. Read repair: when a read finds replicas disagree (one has a
stale version), the coordinator pushes the newer value to the stale replicas as a side effect
of serving the read, so the cluster self-heals from staleness over time without a separate
repair process for every inconsistency.

**Merkle trees for anti-entropy.** Beyond read repair (which only fixes keys that get read),
nodes periodically compare Merkle tree hashes of their key ranges with replica peers to find
and repair inconsistencies in cold data nobody's requested recently — otherwise stale replicas
of rarely-read keys would never get corrected.

## Bottlenecks & Scaling

The two failure modes that dominate at this scale: hot keys (one key gets disproportionate
traffic, and since a key always maps to the same node under consistent hashing, that node
becomes a bottleneck regardless of total cluster size — mitigated by detecting hot keys and
replicating them onto extra nodes beyond the normal replica set), and rebalancing cost when
adding nodes (bounded by the `1/N` property of consistent hashing, but still a real background
data-transfer job that competes with live traffic for disk and network bandwidth, so it needs
to be rate-limited rather than run at full speed).

The quorum knob is the main lever for the throughput/consistency tradeoff under load: dropping
from W=2 to W=1 during a traffic spike buys write latency and availability at the direct cost
of consistency guarantees, and is a legitimate operational decision, not just a design-time one.
