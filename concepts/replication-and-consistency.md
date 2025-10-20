# Replication and Consistency

Replication keeps copies of the same data on multiple machines, for two reasons: durability
(a disk failure doesn't lose data) and availability/throughput (reads can be served from
multiple places, and if one node dies another has the data). The cost is that now you have
to decide how those copies stay in sync, and what happens when they temporarily don't.

## Replication topologies

- Single-leader (primary-replica): one node accepts all writes, replicates them to N
  followers, which serve reads. Simple to reason about — there's one place writes happen —
  but the leader is a write bottleneck and a failover target. This is the default for
  Postgres, MySQL, MongoDB.
- Multi-leader: more than one node accepts writes (often one leader per datacenter), and
  leaders replicate to each other. Solves write latency across regions but now you can get
  conflicting writes to the same row from two leaders and need a conflict resolution
  strategy (last-write-wins, merge functions, CRDTs).
- Leaderless: any replica can accept a write (Dynamo-style — Cassandra, DynamoDB). The client
  or a coordinator writes to multiple replicas directly and uses quorums to reconcile reads.
  No single point of failure for writes, but consistency is entirely down to quorum math.

## Sync vs async replication

Synchronous: the leader waits for the replica to acknowledge before confirming the write to
the client. Guarantees the replica is caught up, but write latency now includes a network
round trip to the replica, and if the replica is unreachable, writes stall.

Asynchronous: the leader confirms the write immediately and ships it to replicas in the
background. Fast writes, but a leader crash before replication completes loses that write —
this is replication lag, and it's the source of most "I wrote it but can't see it" bugs.

Semi-synchronous is the common middle ground: wait for acknowledgment from at least one
replica, ship to the rest asynchronously.

## Consistency models

- Strong consistency: every read sees the most recent write, from any replica. Requires
  coordination on every operation (or routing all reads to the leader), which costs latency.
- Eventual consistency: replicas converge to the same value eventually, given no new writes,
  but a read right after a write might return stale data. Much better latency and
  availability, at the cost of the guarantee.
- Read-your-writes: a weaker, practical guarantee — a user always sees their own writes
  (typically by routing that user's reads to the leader, or to whichever replica they last
  wrote to), even if other users see it later.
- Causal consistency: writes that are causally related (a comment posted after reading a
  post) are seen in the right order by everyone, even if unrelated writes can be reordered.

Most large-scale systems pick eventual consistency for the bulk of reads and add
read-your-writes for the specific paths where a user would notice staleness (like their own
just-posted content).

## Quorums

In a leaderless system with N replicas, define W (write quorum) and R (read quorum) — the
number of replicas that must ack a write, or be queried on a read. If W + R > N, every read
overlaps at least one node that saw the latest write, giving you strong consistency without
a leader. Common config: N=3, W=2, R=2. Tune W and R to trade write latency against read
latency against consistency — DynamoDB and Cassandra both expose this as a per-query knob.

## Failover

When a leader dies, someone has to promote a replica. Manual failover is safe but slow (pager
goes off, human decides). Automatic failover is fast but risky — a false positive (network
blip, not an actual crash) can trigger a promotion while the old leader is still alive and
accepting writes, producing split-brain: two nodes both think they're the leader. Guard
against this with fencing tokens or a consensus system (Raft/Paxos) doing the promotion, not
a simple health-check timer.
