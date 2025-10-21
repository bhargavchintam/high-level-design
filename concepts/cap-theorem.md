# CAP Theorem

CAP says a distributed system, in the presence of a network partition, can offer either
Consistency or Availability, but not both. Not "pick any two of three" — partition
tolerance isn't optional in a real distributed system, since the network will partition
whether you design for it or not. The actual choice is C vs A, and only when a partition is
actually happening.

## What each letter means, precisely

- Consistency here means linearizability: every read gets the most recent write, and all
  nodes agree on order. This is a stronger definition than "consistency" in ACID.
- Availability means every request to a non-failing node gets a response — not necessarily
  the right one, just a response, and not an error.
- Partition tolerance means the system keeps operating when network messages between nodes
  are lost or delayed.

## The actual tradeoff

During a network partition, split into two sides. If a write comes in on one side, you have
two choices:

- Accept the write and risk the other side serving stale data (or a conflicting write) —
  you chose Availability over Consistency (AP).
- Refuse the write (or the read) until the partition heals and you can confirm agreement —
  you chose Consistency over Availability (CP).

Outside of a partition, there's no tradeoff — a well-built system gives you both C and A when
the network is healthy. CAP only bites during the partition itself, which is why it's a
narrower and more situational theorem than it's often presented as in interviews.

## Where real systems land

- CP systems: HBase, MongoDB (with majority read/write concern), etcd, Zookeeper. Choose
  these when correctness matters more than uptime — configuration stores, leader election,
  financial ledgers.
- AP systems: Cassandra, DynamoDB, Riak. Choose these when uptime matters more than
  momentary staleness — shopping carts, social feeds, presence/status, view counters.
- Systems aren't purely one or the other — Cassandra can be tuned toward C with quorum reads
  and writes (see replication-and-consistency.md), and most "CP" systems remain available for
  reads on the majority side of a partition; it's really about what each system does on the
  minority side.

## Why this framing is incomplete

CAP only describes behavior during a partition, but partitions are rare relative to total
uptime — most of the interesting latency/consistency tradeoffs happen with no partition at
all, just normal network delay. PACELC extends this: if Partitioned, choose A or C, Else (no
partition) choose Latency or Consistency. That second half is where most engineering
decisions actually live — do you wait for a quorum ack (higher consistency, higher latency)
or return as soon as the local node is written (lower latency, weaker consistency)? That
choice is made on every request, partition or not.

## Practical takeaway for design interviews

Don't just declare "this system is AP" and move on. Say which operations need strong
consistency and why (a payment ledger, an inventory count you can't oversell) and which can
tolerate eventual consistency (a like count, a follower count, a feed). Most real systems mix
both within the same product — strongly consistent for the parts where being wrong is
expensive, eventually consistent everywhere else for latency and availability.
