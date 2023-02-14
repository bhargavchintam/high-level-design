# Database Sharding

Sharding splits one logical database into multiple physical databases (shards), each holding
a subset of the rows, so that no single machine has to store or serve the whole dataset. It's
what you reach for once vertical scaling (bigger box) and read replicas stop being enough —
usually when write throughput or total data size outgrows a single primary.

## Why not just replicate

Replication (see replication-and-consistency.md) copies the same data to multiple machines
and helps with read throughput and availability, but every replica still holds 100% of the
data and the primary still takes 100% of the writes. Sharding is the only way to scale writes
and total storage horizontally, because each shard only owns a slice of the data.

## Sharding strategies

- Range-based: rows are partitioned by a key range (user IDs 1-1M on shard 1, 1M-2M on shard
  2). Simple, and range queries within a shard are efficient. Downside: uneven distribution —
  if IDs are assigned sequentially, all new writes hit the last shard (hotspot).
- Hash-based: hash the shard key and mod by shard count (or use consistent hashing, see
  consistent-hashing.md). Distributes load evenly, but range queries now have to fan out to
  every shard.
- Directory-based: a lookup service maps each key to its shard explicitly. Flexible — you can
  rebalance individual keys — but the directory becomes a critical dependency and a potential
  bottleneck/SPOF.
- Geo-based: shard by region (EU users on EU shard, US users on US shard). Useful for latency
  and data residency compliance (GDPR), less useful as a general load-balancing strategy since
  usage isn't evenly distributed by geography.

## Choosing a shard key

The shard key decides everything downstream. Good shard keys have high cardinality, spread
load evenly, and match your dominant query pattern so most queries hit one shard. For a
social app, user_id is a common choice because most queries are "get this user's data." Pick
wrong and you get a hot shard (one celebrity user generating 10% of all traffic) or
cross-shard queries on every request.

## Cross-shard problems

- Joins: a join across two shards means fetching from both and joining in application code —
  the database can't do it for you anymore.
- Transactions: an atomic update across two shards needs a distributed transaction (two-phase
  commit) or a saga pattern with compensating actions. Both are meaningfully more complex and
  slower than a single-node transaction.
- Aggregation: `COUNT(*)` or `SUM()` across the whole dataset means querying every shard and
  combining results (scatter-gather), which is only as fast as the slowest shard.
- Secondary indexes: querying by a non-shard-key field means either fanning out to all shards
  or maintaining a separate global index service.

## Resharding

Adding shards later means moving data, and naive hash-mod resharding (`hash(key) % N`)
reshuffles almost every key when N changes. This is the exact problem consistent hashing
solves — see consistent-hashing.md. Many systems also over-provision logical shards (e.g. 4096
hash slots mapped to a smaller number of physical nodes, as Redis Cluster does) so that
rebalancing means moving slot ownership, not rehashing every key.

## When to shard

Shard later rather than earlier. It adds real operational complexity — schema migrations now
run N times, cross-shard queries need application support, and rebalancing is a project, not
a config change. Get as far as you can with read replicas, caching, and a bigger primary
first; shard when a single primary genuinely can't take the write volume or the dataset no
longer fits comfortably on one machine's disk.
