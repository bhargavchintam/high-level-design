# Indexing

A database index is a separate data structure that lets you look up rows without scanning the
whole table. Without an index, `WHERE email = 'x@y.com'` on a 100M-row table means reading up
to 100M rows (a full table scan); with an index on `email`, it's a lookup that costs roughly
`O(log n)`. The cost: every index has to be maintained on every write, and it consumes disk
and memory, so indexes aren't free — you're trading write cost and storage for read speed on
specific query patterns.

## B-tree indexes

The default index type in almost every relational database (Postgres, MySQL InnoDB). A
balanced tree structure where each node holds sorted keys and pointers to children; leaf
nodes point to the actual rows (or contain the row itself, in a clustered index). Good for
equality lookups, range queries (`WHERE age > 25`), and sorted scans (`ORDER BY`), because the
keys are stored in order. This is the right default unless you have a specific reason to
reach for something else.

## Hash indexes

Hash the key, store a pointer to the row. O(1) average lookup, faster than a B-tree for exact
equality matches, but useless for range queries (`>`, `<`, `BETWEEN`) or sorting, since hashing
destroys ordering. Used inside hash-based key-value stores and as a secondary index option in
some databases (Postgres has hash indexes, rarely the better choice over B-tree in practice
given B-tree's range-query flexibility for similar equality-lookup cost).

## Other structures worth knowing

- LSM-trees (log-structured merge trees): writes go to an in-memory buffer (memtable) and are
  flushed to disk in sorted, immutable segments (SSTables), periodically compacted. Optimized
  for write-heavy workloads because writes are sequential appends rather than in-place B-tree
  updates. This is what Cassandra, RocksDB, and LevelDB use underneath. Reads are more
  expensive than a B-tree (may need to check multiple SSTables) but bloom filters mitigate
  that.
- Inverted index: maps each term to the list of documents containing it — the core structure
  behind full-text search (Elasticsearch, Lucene). Built for "find all documents containing
  this word," which a B-tree can't do efficiently.
- Geospatial indexes (R-tree, geohash, quadtree): index by 2D/3D location for "find everything
  within X km" queries. Postgres's PostGIS uses R-trees (GiST); geohashing (encoding lat/lng
  into a string prefix) is common in simpler systems because it reduces to a prefix range
  query on a normal B-tree.

## Composite and covering indexes

A composite index spans multiple columns, e.g. `(user_id, created_at)`. It speeds up queries
filtering on `user_id` alone or on `user_id` + `created_at` together, but not on `created_at`
alone — column order in the index matters, and it only helps queries that use a left prefix
of the indexed columns.

A covering index includes every column a query needs, so the database can answer entirely
from the index without touching the table (an "index-only scan"). Meaningfully faster when it
applies, at the cost of a bigger index.

## The tradeoff

Every index slows down writes (insert/update/delete now has to update the index too, possibly
several indexes) and costs storage. A table with 10 indexes on a write-heavy workload can
spend more time maintaining indexes than doing the actual write. The practical rule: index
what you query by and filter by often, especially in `WHERE` and `JOIN` clauses; don't index
columns you never filter or sort on just because you can. Check `EXPLAIN ANALYZE` output
before adding an index, not after — verify the query planner will actually use it.
