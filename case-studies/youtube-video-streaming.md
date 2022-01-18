# YouTube-Style Video Streaming

## Requirements

### Functional
- Upload video, process it into streamable form, publish it.
- Stream video to viewers at a quality appropriate to their bandwidth and device.
- Search/browse videos, view counts, likes, comments.
- Resume playback, seek within a video.

### Non-functional
- Playback must start fast (low startup latency) and adapt smoothly to changing network
  conditions without stalling.
- Massive read (view) volume vastly exceeds upload volume.
- Storage and bandwidth costs are the dominant cost center at this scale and drive many
  design choices (transcoding into multiple qualities, aggressive CDN usage).

## Capacity Estimation

Assume 500 hours of video uploaded per minute (a real published order-of-magnitude figure for
a YouTube-scale platform) and 1B hours of video watched per day.

Uploads: 500 hours/min × 60 = 30,000 hours/day of raw uploaded video. At a rough 1 GB per hour
of source video (varies wildly by resolution, but usable as an estimation anchor), that's
~30 TB/day of raw ingest before any transcoding.

Transcoding multiplies storage: each upload is transcoded into multiple resolutions (240p,
360p, 480p, 720p, 1080p, maybe 4K) and multiple codecs/bitrates for adaptive streaming — call
it a combined ~3-4x the original size once all derived renditions exist. So ~30 TB/day raw
becomes roughly 100-120 TB/day of total stored video after processing. Over a year that's
tens of petabytes just from new uploads, on top of the existing catalog.

Watch traffic: 1B hours/day of viewing → 1B / 24 ≈ 41.7M concurrent viewer-hours per hour on
average, and average bitrate for a typical adaptive stream (~2-5 Mbps blended across
resolutions) gives a sense of aggregate egress bandwidth in the terabits/sec range at peak —
this is the number that makes CDN distribution non-optional; no origin fleet serves this
directly.

## API Design

```
POST /v1/videos                multipart/resumable upload
                                -> 202 { video_id, status: "processing" }
GET  /v1/videos/{id}/manifest  -> adaptive streaming manifest (HLS/DASH),
                                   listing available quality renditions + CDN URLs
GET  /v1/videos/{id}           -> metadata (title, description, view_count, ...)
POST /v1/videos/{id}/views     -> fire-and-forget view count increment
```

Playback doesn't fetch "the video" as one file — the client fetches a manifest listing
segment URLs per quality level, then requests individual segments (a few seconds each) from
the CDN, switching quality between segments based on measured bandwidth. This is the standard
adaptive bitrate streaming model (HLS/DASH), not a design detail specific to any one platform.

## Data Model

```
videos
  video_id (PK)
  uploader_id, title, description, created_at
  status: uploading | processing | ready | failed
  duration
  view_count, like_count   (denormalized, asynchronously updated)

renditions
  video_id, resolution, bitrate, codec -> object storage path / CDN URL
  (one row per transcoded output — a single upload produces many of these)
```

Video bytes and their renditions live entirely in object storage behind a CDN, exactly like
the media-storage pattern in case-studies/instagram.md, scaled up — metadata in a database,
bytes never touch the database.

## High-Level Architecture

```
 upload:
 client ──resumable upload──► Ingest service ──► raw video to Object Storage
                                     │
                                     └──► enqueue transcoding job(s) on Message Queue
                                                    │
                                     Transcoding worker fleet (horizontally scaled,
                                     GPU/CPU-heavy, one job produces N renditions:
                                     multiple resolutions x bitrates)
                                                    │
                                     each rendition written to Object Storage,
                                     manifest (HLS/DASH) generated referencing
                                     all rendition segment URLs
                                                    │
                                     video status -> "ready", now visible

 playback:
 client ──GET manifest──► App server / CDN (manifest is small, cacheable)
 client ──GET segments──► CDN edge (segment cache) ──miss──► Object Storage origin
                           (99%+ of segment bytes should be served from CDN edge,
                            not origin, given the read volume above)
```

## Deep Dives

**Transcoding pipeline.** A single upload fans out into many independent transcoding jobs (one
per target resolution/bitrate combination), which parallelize naturally across a worker fleet
— this is an embarrassingly parallel batch job, well-suited to being split into chunks (even
segment-level parallelism, transcoding different time ranges of the same video concurrently on
different workers, for very long videos). This is CPU/GPU-bound work, entirely decoupled from
the upload request itself via the same async-processing pattern as
case-studies/instagram.md — the user sees "processing," not a blocked upload request.

**Adaptive bitrate streaming.** The video is encoded at multiple resolution/bitrate tiers and
sliced into short segments (2-10 seconds each). The client's player continuously measures
achieved download throughput and picks the highest-quality segment it can fetch without
stalling for the next few seconds of playback, adjusting up or down as conditions change. This
is why "streaming" a video is really a sequence of small HTTP requests for individual
segments, not one long-lived connection — which also makes it CDN-cacheable in a way a single
giant file with byte-range requests is not as cleanly.

**CDN and origin shield.** Given the read volume, essentially every played segment should come
from a CDN edge cache. A wrinkle unique to video: not every video is equally popular, and
older/less popular videos ("the long tail") have low cache hit rates by nature — pre-warming
or predictively caching is less effective there than for consistently hot content. A common
mitigation is an origin shield layer (a mid-tier cache between edge nodes and origin object
storage) so that even long-tail content, when it does get a burst of views, doesn't send every
one of those edge-cache misses all the way back to origin storage simultaneously.

**View count and engagement metrics.** Incrementing a view counter on every playback would be
a huge write-amplification problem at this read volume, and is also a target for gaming/abuse
(view-count fraud). Real systems batch and deduplicate view events (e.g. require a minimum
watch duration before counting a view, aggregate counts in a stream-processing layer, and
flush aggregated counts to the video's row periodically) rather than doing a synchronous
`UPDATE videos SET view_count = view_count + 1` per playback.

**Resumable/chunked upload.** For large video files, upload itself needs to tolerate network
interruption — the client uploads in chunks with the ability to resume from the last
successfully received chunk rather than restarting a multi-gigabyte upload from zero on any
connection drop. This is a client-server protocol concern (resumable upload APIs, as most
object storage services provide) more than a distributed-systems one, but it matters enough at
this scale to call out explicitly.

## Bottlenecks & Scaling

Transcoding capacity needs to scale with upload volume and is the most compute-expensive part
of the pipeline (video encoding is CPU/GPU-intensive); it's handled as an autoscaled worker
pool consuming a queue, same shape as the media-processing pattern elsewhere in this repo.
Egress bandwidth, not storage or compute, is usually the dominant operating cost at this scale
— which is precisely why CDN offload and multi-tier caching (edge + shield) receive so much
design attention here relative to, say, a text-heavy system: bytes moved, not requests served,
is the number that matters most for a video platform's infrastructure bill.
