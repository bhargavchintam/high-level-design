# Instagram-Style Photo Sharing

## Requirements

### Functional
- Upload photos/videos with a caption.
- Follow other users; see a feed of posts from people you follow.
- Like and comment on posts.
- View a user's profile grid of their own posts.

### Non-functional
- Upload should feel fast to the user even though media processing (resizing, transcoding)
  takes real time — the user shouldn't wait on it synchronously.
- Media reads (viewing photos/videos) dominate traffic by a wide margin and must be
  low-latency globally.
- Feed generation has the same fan-out tradeoffs as any social feed (see
  case-studies/twitter-news-feed.md) — this doc focuses on the parts specific to media.

## Capacity Estimation

500M DAU, each user uploads on average 1 photo every 2 days (~0.5/day) and views ~100
photos/day scrolling their feed.

Uploads: 500M × 0.5 = 250M photos/day → 250M / 86,400 ≈ 2,900 writes/sec average, ~8,700/sec
peak.

Reads: 500M × 100 = 50B photo views/day → ~580,000 reads/sec average, ~1.7M/sec peak. This
read volume is why the architecture centers almost entirely on caching and CDN offload — no
origin fleet serves 1.7M reads/sec directly.

Storage: assume an average original upload is 2 MB (mix of photos and short video, photos
dominate by count). 250M/day × 2 MB = 500 TB/day of new raw media. Add derived assets — each
original generates multiple resized/transcoded variants (thumbnail, feed-resolution, full-
resolution, maybe 4-5 variants) at a combined size roughly 1.5x the original — call total
ingest ~1.25 PB/day into object storage. Over a year that's well into the hundreds of
petabytes, which is why this tier is built on blob storage (S3-class) with lifecycle policies,
never on a traditional database.

Metadata (post_id, user_id, caption, like_count, timestamps) is tiny by comparison — same
order of magnitude as the tweet metadata numbers in the Twitter case study, a few hundred
bytes per post.

## API Design

```
POST /v1/posts               multipart upload: media file + caption
                              -> 202 Accepted { post_id, status: "processing" }
GET  /v1/posts/{id}          -> post metadata + CDN URLs for each media variant
GET  /v1/feed?cursor=...     -> paginated feed (same shape as twitter-news-feed.md)
POST /v1/posts/{id}/like
POST /v1/posts/{id}/comments
```

Upload returns `202 Accepted` immediately, not `201 Created` with a finished object — media
processing is asynchronous, and the client polls or gets a push notification when the post
is ready to appear.

## Data Model

```
posts
  post_id (PK)
  user_id
  caption
  media_variants: { thumbnail_url, feed_url, full_url }   (CDN-backed object storage paths)
  created_at
  like_count, comment_count   (denormalized)

media_objects (in blob storage, not a relational table)
  key: {post_id}/{variant}.jpg
  stored in S3-class storage, served via CDN in front of it

likes  (user_id, post_id) composite key — existence check + count
comments (comment_id, post_id, user_id, text, created_at)
```

Post metadata lives in a database (sharded by `user_id` or `post_id`, wide-column store fits
well given simple key-based access); the actual image/video bytes never touch that database —
only the CDN URL pointing at object storage does.

## High-Level Architecture

```
 upload:
 client ──upload──► App server ──► write original to Object Storage (S3)
                          │        write post row (status: processing) to DB
                          │
                          └──► enqueue job on Message Queue
                                        │
                                        ▼
                          Media Processing workers (async, horizontally scaled)
                          - generate thumbnail / feed-res / transcoded variants
                          - write each variant back to Object Storage
                          - update post row (status: ready) + push notification
                                        │
                                        ▼
                          Object Storage variants ──► CDN (edge cache)

 read:
 client ──GET feed/post──► App server ──► post metadata from cache/DB
                                       ──► media URLs point directly at CDN,
                                           client fetches image bytes from
                                           CDN edge, not through the app server
```

The app server and database are never in the path of actual image bytes on read — they only
ever hand back a CDN URL. This is the single most important architectural decision in the
whole system: it keeps the read-heavy media traffic (1.7M/sec peak) entirely off the
application tier.

## Deep Dives

**Asynchronous media processing.** Generating multiple resolutions and formats from a single
upload (thumbnail for feed thumbnails, medium for feed display, original for full-screen,
possibly re-encoded video at multiple bitrates) is CPU-heavy and takes seconds, which is far
too slow to do inline on the upload request. The upload path writes the original to object
storage, enqueues a processing job, and returns immediately; a pool of workers consumes the
queue, and the post only becomes visible in feeds once processing completes. This decouples
upload latency (fast, user-facing) from processing latency (slower, invisible to the user) —
see concepts/message-queues.md for the general pattern.

**CDN and cache-first reads.** Given the read:write ratio here (roughly 200:1 by request
count, and orders of magnitude more by bytes since each read pulls image data and each write
is comparatively small), essentially all image bytes should be served from CDN edge caches,
with object storage as the origin the CDN falls back to on a cache miss (see concepts/cdn.md).
A well-tuned setup should see a 90%+ CDN hit rate for anything beyond the first few minutes
after upload, since a photo's popularity is heavily front-loaded right after posting and then
tapers — meaning the origin's actual sustained load is a small fraction of total view volume.

**Feed generation.** Uses the same push/pull hybrid fan-out described in
case-studies/twitter-news-feed.md — post metadata (not media bytes) gets pushed into
followers' feed caches, and the feed itself is a list of post_ids that the client resolves to
CDN URLs on render. The media-specific addition here is that a post shouldn't fan out into
feeds until its processing job completes — fanning out a post before its feed-resolution
image variant exists would show followers a broken or missing image.

**Variant selection.** Serving the right resolution for the right context (a small thumbnail
in a grid view vs. full resolution in a detail view) matters for both latency and bandwidth
cost — sending full-resolution images into a scrolling feed of thumbnails wastes bandwidth by
an order of magnitude or more with zero visual benefit. The variant URL scheme needs to be
decided at upload-processing time, not on the fly at serve time, so the CDN can cache each
variant as its own immutable object.

## Bottlenecks & Scaling

The media-processing worker pool needs to scale with upload volume independently of the app
tier — it's a classic queue-depth-driven autoscaling target (see concepts/message-queues.md
on backpressure). Object storage itself is effectively infinitely horizontally scalable by
design (that's what S3-class storage is built for) so it's rarely the bottleneck; the CDN hit
rate is the number that actually determines whether origin (object storage + the app tier
generating signed URLs) can keep up — a drop in hit rate, from a cache purge or a traffic
pattern shift, is what turns this into an origin-scaling problem rather than a CDN-capacity
problem.
