# Web Crawler

## Requirements

### Functional
- Given a set of seed URLs, discover and fetch pages, extract links, and recursively crawl
  discovered URLs.
- Extract and store page content (or hand it off to an indexing pipeline).
- Respect `robots.txt` and per-site crawl-rate policies.
- Avoid re-crawling unchanged content unnecessarily; detect and prioritize updated content.

### Non-functional
- Scale to billions of pages.
- Politeness: never hammer a single host — this is as much a constraint as a feature
  requirement, since an impolite crawler gets IP-banned and generates real complaints.
- Avoid infinite loops and crawler traps (URLs that generate infinite new URLs, e.g. a
  calendar page with a "next month" link forever).
- Freshness: important pages should be recrawled more often than a random long-tail page.

## Capacity Estimation

Target: crawl 10B pages over 30 days (a substantial but not planet-scale crawl budget). That's
10B / (30 × 86,400) ≈ 3,900 pages/sec sustained fetch rate.

Average page size ~500 KB (HTML plus embedded resources, on the higher end deliberately since
a lot of the modern web is heavier than plain HTML). 3,900 pages/sec × 500 KB ≈ 1.95 GB/sec of
inbound bandwidth at sustained crawl rate — enough that bandwidth and fetch concurrency, not
CPU, are the primary throughput constraints.

Storage: 10B pages × 500 KB ≈ 5 PB of raw fetched content if kept in full; in practice much of
this is processed (extracted text, stripped of markup, deduplicated) rather than stored
verbatim, and content-hash deduplication (identical pages mirrored across multiple URLs is
common) reduces this meaningfully, similar to the dedup story in
[case-studies/google-drive-dropbox.md](google-drive-dropbox.md).

URL frontier size: with an average fanout of, say, 20 new links discovered per crawled page,
the frontier (queue of URLs not yet crawled) can balloon far larger than the crawl budget
itself — needs its own scalable, persisted queue rather than an in-memory list.

## API Design

This system has no real external API — it's an internal pipeline. The closest thing to an
interface is between its own stages:

```
Frontier.next_batch(worker_id, count) -> [urls]
Frontier.add(url, priority, discovered_from)
Fetcher.fetch(url) -> { status, headers, body } | error
ContentStore.put(url, content_hash, body, fetched_at)
```

## Data Model

```
url_frontier (priority queue, partitioned by host)
  url, priority_score, discovered_at, host

crawl_metadata
  url (PK) -> { last_crawled_at, content_hash, status,
                etag/last_modified, crawl_frequency_hint }

seen_urls (bloom filter or hash set, for fast dedup check)
  used to answer "have we already queued/crawled this URL" cheaply

robots_cache
  host -> { rules, fetched_at }   (refreshed periodically, not on every fetch)
```

## High-Level Architecture

```
 seed URLs ──► URL Frontier (priority queue, partitioned by host to
                enforce per-host politeness independently per partition)
                        │
              ┌─────────┴──────────┐
              ▼                    ▼
        Fetcher workers      Fetcher workers    (horizontally scaled,
        (pool per host,      (pool per host,     rate-limited per host,
         respects robots.txt  respects robots.txt not globally)
         crawl-delay)         crawl-delay)
              │                    │
              └─────────┬──────────┘
                         ▼
                  Content dedup check
                  (hash body, skip if
                   seen before)
                         │
                new/changed content
                         │
              ┌──────────┴──────────┐
              ▼                     ▼
       Link extractor          Content store /
       (parse HTML,            indexing pipeline
        find new URLs)         (downstream, out of
              │                 scope of the crawler
              ▼                 itself)
       new URLs ──► dedup check (seen_urls)
                    ──► add to Frontier if new
```

## Deep Dives

**Politeness.** A crawler that fetches as fast as it possibly can from a single host will get
IP-banned and, more importantly, can genuinely degrade that host's service for real users —
this is treated as a hard constraint, not a nice-to-have. The frontier is partitioned by host
(or domain), and each host-partition has its own rate limit (respecting `robots.txt`'s
`Crawl-delay` directive where present, and a conservative default like 1 request per few
seconds per host otherwise), enforced independently of overall system throughput — the crawler
can be fetching thousands of pages/sec in aggregate while never exceeding a handful of
requests/sec against any single host. This is the same per-key rate limiting shape as
[concepts/rate-limiting.md](../concepts/rate-limiting.md), applied per-host instead of per-client.

**Avoiding crawler traps.** Some sites generate effectively infinite URLs (calendar
pagination, faceted search filters with combinable parameters, session IDs embedded in URLs
making every visit look like a new URL). Mitigations: cap crawl depth per site, cap total
URLs fetched per host within a time window, detect and normalize/strip known trap patterns
(session ID query params), and set a hard ceiling on how deep the frontier will follow a
single host's link graph before deprioritizing it — a purely reactive per-URL approach doesn't
scale, this needs to be a host-level policy.

**Deduplication.** Two levels: URL-level (don't queue the same URL twice — a bloom filter over
`seen_urls` gives a fast, memory-efficient, probabilistic membership check with a tunable false
-positive rate, trading a small chance of skipping a genuinely new URL for a massive reduction
in memory versus an exact set at billions of entries) and content-level (two different URLs
serving byte-identical or near-identical content — common with URL parameters that don't
change the page, or mirrored content — detected by hashing fetched content and skipping
reprocessing of a hash already seen, which also directly reduces the storage estimate above).

**Prioritization and freshness.** Not every page deserves equal crawl frequency — a news
homepage changes hourly, a static documentation page might not change for years. Each URL
gets a priority score in the frontier informed by signals like PageRank-style importance,
historical change frequency (if this URL has changed every time it's been recrawled, prioritize
it higher next time; if it's been identical for the last 5 crawls, deprioritize it), and
`Last-Modified`/`ETag` headers from the server that let the crawler do a cheap conditional
fetch (HTTP 304 Not Modified) instead of re-downloading unchanged content.

**Distributed frontier.** At billions of URLs, the frontier itself doesn't fit on one machine
and needs to be partitioned (commonly by host, which conveniently also aligns with the
per-host politeness partitioning above) across multiple queue shards, each served by its own
pool of fetcher workers — this dual-purpose partitioning (by host, for both politeness and
scale) is the single most important structural decision in the whole system.

## Bottlenecks & Scaling

Per-host politeness constraints mean total system throughput isn't simply "add more fetcher
workers" — a host with a strict rate limit stays rate-limited no matter how many idle workers
exist, so the real lever for aggregate throughput is the number of distinct hosts being
crawled concurrently, not raw worker count. DNS resolution and connection setup at this
fetch rate is itself a meaningful cost (thousands of new TCP+TLS handshakes/sec) and benefits
from connection pooling and a dedicated, cached DNS resolution layer rather than resolving
per-request through a generic system resolver.
