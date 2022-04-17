# Twitter-Style News Feed

## Requirements

### Functional
- Users post short text updates (tweets), optionally with media.
- Users follow other users.
- A user's home feed shows recent posts from everyone they follow, roughly reverse-
  chronological (real systems rank it, but ranking is a separate concern layered on top of
  feed generation).
- Users can like, reply to, and repost.

### Non-functional
- Feed load latency should be low (sub-200ms) — this is the most-hit read path in the product.
- High write availability — posting should almost never fail.
- Eventual consistency is acceptable for feed delivery (a follower seeing a new post a few
  seconds late is fine); it is not acceptable to lose a post.
- Must handle extreme skew: some accounts have 100M+ followers, most have a few hundred.

## Capacity Estimation

100M DAU. Assume each user posts on average 2 tweets/day and reads their feed 10 times/day.

Writes: 100M × 2 = 200M tweets/day → 200M / 86,400 ≈ 2,315 writes/sec average, ~7,000/sec at
peak (3x factor).

Reads: 100M × 10 = 1B feed loads/day → ~11,600 reads/sec average, ~35,000/sec peak. Read:write
ratio here is roughly 5:1 on raw request count, but the fan-out multiplies the effective write
work far beyond that (see below).

Storage: average tweet ~300 bytes (text + metadata: user_id, tweet_id, timestamp, counts,
media pointer). 200M/day × 300 bytes ≈ 60 GB/day, ~22 TB/year, ~66 TB over 3 years before
replication; ~200 TB at 3x replication. Media (images/video) is stored separately in
object storage / CDN, not in this row, and dominates total bytes but not query load.

Social graph: assume average 200 follows per user. 100M users × 200 ≈ 20B edges. At ~16 bytes
per edge (follower_id + followee_id), that's ~320 GB — small enough to keep heavily cached or
in a dedicated graph-friendly store, but the fan-out computation over this graph is the real
cost center, not its storage.

## API Design

```
POST /v1/tweets            { text, media_ids? }              -> 201 { tweet_id }
GET  /v1/feed?cursor=...    -> paginated list of tweets for the home feed
POST /v1/users/{id}/follow
POST /v1/tweets/{id}/like
GET  /v1/users/{id}/tweets  -> a single user's own timeline
```

## Data Model

```
tweets
  tweet_id (PK, snowflake-style ID — sortable by time)
  user_id
  text
  media_ids[]
  created_at
  like_count, retweet_count   (denormalized counters, eventually consistent)

follows
  follower_id, followee_id (composite key, indexed both directions)

feed_cache (per-user, precomputed — see fan-out deep dive)
  user_id -> [tweet_id, tweet_id, ...]   (sorted list, capped length, e.g. last 800)
```

`tweets` is sharded by `tweet_id` (which embeds a timestamp, so it's roughly time-ordered
within shards — good for a user's own profile timeline, requires a secondary index elsewhere
for feed assembly). `follows` needs to be queried in both directions (who does X follow, who
follows X) so it's typically indexed/stored both ways.

## High-Level Architecture

```
 write path:
 client ──POST tweet──► App server ──► tweets store (Cassandra, sharded by tweet_id)
                              │
                              └──► Fan-out service ──► pushes tweet_id onto each
                                                        follower's feed_cache in Redis
                                                        (for followers under the fan-out
                                                        threshold — see deep dive)

 read path:
 client ──GET feed──► App server ──► Redis feed_cache[user_id] ──► hydrate tweet_ids
                                        (miss/cold) │              from tweets store
                                                     ▼
                                          fall back to on-demand
                                          fan-out-on-read merge
                                          across followed users
```

## Deep Dives

**Fan-out on write vs. fan-out on read.** This is the central design decision for any feed
system.

Fan-out on write (push): when a user posts, immediately push the tweet ID into the precomputed
feed cache of every follower. Feed reads become a cheap cache lookup (fast, the common case —
optimize for reads since reads vastly outnumber writes). The problem is celebrity accounts: a
user with 100M followers triggers 100M cache writes for a single tweet, which is both slow and
wasteful if most of those followers never even open the app that day.

Fan-out on read (pull): don't precompute anything. When a user opens their feed, fetch recent
tweets from everyone they follow and merge them on the fly. No wasted write work, but feed
reads become expensive — merging across hundreds of followed users on every single feed load,
at far higher read volume than write volume.

**The hybrid, which is what Twitter actually does.** Fan-out on write for the vast majority of
users (normal follower counts), skip fan-out for accounts above a follower threshold (a few
hundred thousand, say). When assembling a feed, merge the precomputed cache (covers everyone
normal the user follows) with a live, on-demand fetch of recent tweets from any celebrities
the user follows (there are only ever a handful of celebrity accounts any one user follows, so
this merge is cheap even done on every read). This gets the read speed of push for the common
case without paying the write amplification of push for the pathological case.

**Ranking vs. chronological.** A pure reverse-chronological feed is what's described above; a
ranked feed (what most real products show by default) adds a scoring pass — pull a candidate
set (the same fan-out-produced list, or a larger candidate pool), score each item with a model
using engagement signals, recency, and affinity, then sort by score instead of time. This adds
a real-time inference cost per feed load but reuses the same candidate-generation
infrastructure described above; ranking is a layer on top, not a different feed architecture.

**Feed cache bound.** Precomputed feed caches are capped (e.g. 800 tweet IDs per user) rather
than unbounded, since nobody scrolls back further than that in practice, and an unbounded
per-user list would grow storage without bound for accounts that rarely get read.

## Bottlenecks & Scaling

The celebrity fan-out problem above is the headline bottleneck and the hybrid push/pull model
is the answer. Beyond that: the `tweets` store needs to handle very high write throughput with
simple key-based access (a wide-column store fits better than a relational one here), the
`follows` graph needs to support fast "who follows X" lookups for fan-out and fast "who does X
follow" lookups for the on-demand celebrity merge — often denormalized into both directions
rather than queried relationally. The feed cache (Redis) is what actually serves the bulk of
read traffic and needs to be sharded by user_id with enough replication that a node loss
doesn't degrade feed availability for the users it owned.
