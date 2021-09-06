# CDN

A CDN (content delivery network) is a set of servers distributed geographically that cache
content close to users, so a request doesn't have to travel to your origin server (and back)
every time. The win is almost entirely about latency and origin load, not raw storage:
speed-of-light delay from Bangalore to a us-east origin is ~250ms round trip minimum; a CDN
edge node in Mumbai serving the same asset is single-digit milliseconds.

## What goes on a CDN

Static assets are the classic case: images, video segments, JS/CSS bundles, downloadable
files. These are immutable or versioned (a hashed filename like `app.a3f21c.js`), so caching
them aggressively is safe — a new deploy gets a new filename, so there's no invalidation
problem at all.

Dynamic content can be cached too, more carefully: API responses that are the same for every
user (a product catalog page) can sit behind a CDN with a short TTL. Personalized or
mutating responses generally can't be cached at the edge, though some CDNs support
edge-side includes or edge compute (Cloudflare Workers, Lambda@Edge) to assemble personalized
pages from a cached shell plus a small dynamic fetch.

## Push vs pull

Pull (lazy): the CDN fetches from origin on the first request for a given path (cache miss),
caches it, and serves that copy until TTL expiry. Simple, no separate upload step, but the
first request from every region eats the full origin latency (cold cache per edge location).

Push: you upload content to the CDN ahead of time. Used for large media libraries you know in
advance (a video platform pre-distributing a new upload to edge nodes near where it expects
demand) or when you want to guarantee no cold-cache penalty for launch-day traffic.

## Cache invalidation

Set a `Cache-Control` / `max-age` header and let it expire naturally — the simplest and most
common approach. For content that changes and can't wait out a TTL, CDNs offer explicit
purge APIs, but purging is eventually consistent across edge locations (can take seconds to
minutes to propagate globally) and purging by pattern (wildcard) is usually slower and more
limited than purging a single known URL. The versioned-filename trick sidesteps invalidation
entirely for static assets and is generally preferred over relying on purge.

## Anycast and routing

CDN edge locations typically share a single anycast IP; BGP routes each user's request to the
topologically nearest location automatically, no client-side logic needed. Alternatively,
GeoDNS resolves a hostname to a different IP depending on the resolver's location. Anycast
reacts faster to an edge location going down (BGP reroutes) but is coarser-grained than
GeoDNS in some setups.

## Numbers to have a feel for

A typical CDN cache hit ratio for a mature static-asset setup is 90-95%+; the miss traffic is
what still hits origin. For a site serving, say, 500M image requests/day at a 95% hit rate,
that's 25M requests/day (~290/sec average) actually reaching origin instead of 500M — the
difference between needing a handful of origin servers and needing hundreds. This is usually
the real reason to put a CDN in front of anything with meaningful read traffic: it's origin
capacity planning as much as it is user-facing latency.
