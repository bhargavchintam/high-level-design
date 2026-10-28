# Consistent Hashing

The problem: you have N cache or database nodes and you distribute keys with `hash(key) % N`.
It works fine until N changes — add or remove one node, and nearly every key's assigned node
changes, because the modulus changed. For a cache that means a near-total cache wipe; for a
sharded store it means moving almost all the data. Consistent hashing fixes this so that
adding or removing a node only remaps roughly `1/N` of the keys.

## How it works

Map both nodes and keys onto the same hash space, visualized as a ring (0 to 2^32-1, say).
Hash each node's identifier (e.g. `hash("cache-node-3")`) to get its position on the ring.
Hash each key the same way. To find which node owns a key, walk clockwise from the key's
position until you hit a node — that node owns it.

When you add a node, it takes a position on the ring and inherits only the keys between it
and the previous node counter-clockwise — everything else is untouched. When you remove a
node, its keys fall to the next node clockwise, and nobody else's assignment changes.

## Virtual nodes

Plain consistent hashing with one point per physical node gives uneven distribution — with
only a handful of nodes, the gaps between them on the ring vary a lot by chance, so some
nodes end up owning much bigger arcs than others. The fix is virtual nodes: each physical
node gets hashed to many points on the ring (100-200 is typical), so its total ownership is
an average over many small arcs instead of one big arc. This also smooths out rebalancing —
when a node leaves, its load spreads across many other nodes instead of dumping entirely
onto its one clockwise neighbor.

## Where it's used

- Cache clusters: client-side hashing in Memcached deployments, so any client can compute
  which node owns a key without a lookup service.
- Redis Cluster uses a related but distinct scheme — 16384 fixed hash slots distributed
  across nodes, and rebalancing moves slot ownership rather than rehashing the ring. Same
  goal (minimize data movement on membership change), different mechanism.
- DynamoDB and Cassandra: partition data across nodes using consistent hashing (Cassandra
  literally inherited the idea from Amazon's Dynamo paper), then layer replication on top —
  each key is stored on the N nodes walking clockwise from its ring position.
- Load balancers: hashing a client ID or session key to a backend, so the same client
  consistently reaches the same backend without a shared session table (see
  [load-balancing.md](load-balancing.md)).

## Tradeoffs

Still not perfectly even — you're trading exact balance for cheap rebalancing, and skewed key
popularity (a hot key) isn't solved by consistent hashing at all, since one key always maps
to the same node regardless of ring position. Hot keys need a separate mitigation (replicate
the specific hot key, or add an application-level cache in front). Consistent hashing also
adds a layer of indirection — every client either needs to know the ring layout or ask a
coordinator, which is its own small piece of infrastructure to keep correct as nodes join and
leave.
