# SQL vs NoSQL

The real question isn't "which is better" — it's which consistency, schema flexibility, and
scaling characteristics your access pattern needs. Most large systems use both, for different
pieces of data.

## SQL (relational)

Fixed schema, enforced at write time. Data normalized into tables with foreign keys; joins
computed at query time. ACID transactions across multiple rows/tables. Vertical scaling is
the default; horizontal scaling (sharding) is possible but not built in — you're doing it
yourself or with an add-on (Vitess, Citus).

Good fit: data with real relational structure you'll query in ad-hoc ways (a banking ledger,
an e-commerce order/inventory system), anything needing multi-row transactional guarantees,
and cases where you don't yet know all your query patterns and want the flexibility of SQL to
ask new questions later.

Postgres, MySQL are the default choices. Postgres has pulled ahead in general mindshare for
new projects due to richer feature set (JSONB, extensions, better standards compliance);
MySQL remains extremely common in existing large-scale deployments.

## NoSQL

Not one thing — four different families with different tradeoffs:

- Key-value (Redis, DynamoDB): O(1) lookup by key, no query language beyond that. Fastest,
  simplest, most limited. Good for caching, session storage, anything looked up by a single
  known ID.
- Document (MongoDB, Couchbase): JSON-like documents, schema-flexible (each document can have
  different fields), supports querying into nested fields. Good when your data is naturally
  hierarchical and doesn't need strict cross-document consistency — a product catalog, a
  content management system.
- Wide-column (Cassandra, HBase, Bigtable): rows can have different columns, optimized for
  huge write volume and queries by a known partition key. Good for time-series data, event
  logging, anything with massive write throughput and simple access patterns.
- Graph (Neo4j, Amazon Neptune): nodes and edges as first-class citizens, optimized for
  traversal queries (friends-of-friends, shortest path). Good when the relationships between
  entities matter more than the entities themselves — social graphs, fraud detection,
  recommendation engines.

Most NoSQL stores trade ACID for horizontal scalability and availability — they're built to
shard and replicate across commodity hardware natively, usually landing on the AP side of CAP
by default (see cap-theorem.md), with tunable consistency in systems like Cassandra and
DynamoDB.

## The actual decision

Ask: does this data need multi-row transactions and complex ad-hoc queries with joins? Lean
SQL. Is the access pattern simple (get by key, append-only writes) and does write volume or
data size exceed what one machine can hold? Lean NoSQL, and pick the family that matches the
shape of the data — key-value for lookups, wide-column for write-heavy time series, document
for flexible nested records, graph for relationship traversal.

## In practice

A typical large system is polyglot: Postgres for the core transactional data (accounts,
orders, billing), Redis for caching and session state, Cassandra or DynamoDB for high-volume
event data (activity feeds, metrics, logs), Elasticsearch for full-text search, and maybe
Neo4j if there's a genuine graph problem (social connections, recommendations). Almost every
case study in this repo picks a specific store per component rather than one database for
everything — see the Data model section of each case study for concrete choices and why.
