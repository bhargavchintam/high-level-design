# Back-of-Envelope Estimation

The point of estimation in a system design discussion isn't precision — it's figuring out
which order of magnitude you're dealing with, because that decides the entire architecture. A
system serving 10K QPS fits on a handful of servers behind a load balancer; the same system at
1M QPS needs sharding, caching layers, and CDN offload from day one. Get the order of
magnitude right and the rest of the design follows from it.

## Numbers worth memorizing

- 1 day = ~86,400 seconds, round to 100,000 for quick math.
- 1 million seconds ≈ 11.5 days. 1 billion seconds ≈ 32 years.
- L1 cache reference: ~1 ns. Main memory reference: ~100 ns. SSD random read: ~100 μs. Round
  trip within same datacenter: ~0.5 ms. Round trip cross-continent: ~150 ms.
- A single modern server can handle roughly 1K-10K QPS for simple in-memory operations, far
  less (10s-100s) for anything hitting disk or doing heavy computation per request.
- 1 KB ≈ 10^3 bytes, 1 MB ≈ 10^6, 1 GB ≈ 10^9, 1 TB ≈ 10^12 — using powers of 10 instead of
  1024 is fine and standard for estimation, the 2.4% error doesn't matter here.

## The general method

1. Get DAU/MAU (daily/monthly active users) — either given or assumed from context (100M DAU
   for a Twitter-scale app, 10M for a mid-size product).
2. Derive requests per user per day for the action you're estimating (average tweets posted,
   average feed refreshes, average messages sent).
3. Multiply to get total daily operations, divide by 86,400 for average QPS.
4. Multiply average QPS by a peak factor (2-3x is a common assumption for daily traffic
   patterns, higher for anything with a strong regional peak) to get peak QPS — always design
   for peak, not average.
5. Estimate average payload size per operation, multiply by daily operation count for daily
   storage, multiply by retention period for total storage, and don't forget replication
   factor (3x is standard) and index overhead (add 20-50%).

## Worked example: storage for a tweet-like system

100M DAU, each user posts on average 2 tweets/day (most users read far more than they post,
but not everyone posts every day, so the average per DAU is low). That's 200M tweets/day. Each
tweet is roughly 300 bytes of text plus metadata (user ID, timestamp, tweet ID, reply/retweet
pointers) — call it 300 bytes total for the row. 200M × 300 bytes ≈ 60 GB/day of raw tweet
data. Over 5 years (roughly 1,800 days) that's ~108 TB, before replication. At 3x replication
for durability, ~324 TB. That's the number that tells you: this needs a horizontally sharded
store, not a single Postgres instance, and it tells you that before you've drawn a single box
in the architecture diagram.

## Worked example: QPS

Same system, 200M tweets/day written. Average write QPS = 200M / 86,400 ≈ 2,315/sec. Reads
dominate on a social app — assume a 100:1 read:write ratio for feed views vs. posts — so read
QPS ≈ 231,500/sec average. Apply a 3x peak factor: ~700K reads/sec at peak. That number alone
tells you this cannot be served by hitting a database directly on every read — you need
aggressive caching (a feed cache, a CDN for media) and the database is mostly there for
durability and the write path, not for serving reads directly.

## What interviewers are actually checking

Not whether your arithmetic is exact — whether you reach for estimation unprompted before
designing, whether the numbers you produce actually inform later decisions (do you say "700K
reads/sec, so we need a cache layer" and then actually design one), and whether you sanity-
check absurd results (if your napkin math says 50 PB/day, something's wrong with an
assumption, go find it rather than plowing ahead).
