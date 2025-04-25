# Uber-Style Ride Hailing

## Requirements

### Functional
- Riders request a ride from their current location to a destination.
- The system matches a rider to a nearby available driver.
- Both parties see the other's live location during the ride.
- Pricing (including surge) is computed based on demand/supply in the area.
- Trip history, receipts.

### Non-functional
- Matching latency must be low — a rider waiting more than a few seconds for a match starts
  to feel broken.
- Location updates from drivers are extremely high frequency and high volume, system-wide.
- Strong consistency matters for a narrow but critical slice (a driver can't be matched to two
  simultaneous rides) — most of the rest of the system can be eventually consistent.
- Geographic locality: almost every operation is inherently scoped to "near a point," which
  shapes the data model more than almost anything else in this design.

## Capacity Estimation

Assume 5M concurrent active drivers globally at peak, each sending a location ping every 4
seconds. That's 5M / 4 ≈ 1.25M location updates/sec sustained, system-wide — this is the
single largest traffic driver in the whole system, well beyond ride requests themselves.

Ride requests: assume 20M rides/day globally → 20M / 86,400 ≈ 230 requests/sec average, ~700/
sec at peak — tiny by comparison to location-ping volume, but each one triggers a
latency-sensitive matching computation.

Storage: location pings are high-volume but low-value individually and mostly needed only for
a short recent window (current position, recent trail for ETA/route) — not retained
indefinitely at full resolution. 1.25M pings/sec × ~100 bytes ≈ 125 MB/sec if written
naively; in practice most of this lives in an in-memory/geo-indexed store (current position
only, overwritten each ping) rather than an ever-growing log, which changes the storage
profile from "petabytes" to "bounded by driver count," not by ping frequency.

## API Design

```
POST /v1/drivers/{id}/location    { lat, lng, timestamp }   -> ack (high frequency, ~every 4s)
POST /v1/rides/request             { pickup, dropoff }       -> { ride_id, matched_driver? }
GET  /v1/rides/{id}                -> current status, driver location, ETA
POST /v1/rides/{id}/cancel
GET  /v1/pricing/estimate          { pickup, dropoff }       -> price estimate, surge multiplier
```

## Data Model

```
driver_locations (current position only, geo-indexed, in-memory)
  driver_id -> { lat, lng, geohash, updated_at, status: available|on_trip|offline }

rides
  ride_id (PK)
  rider_id, driver_id, pickup, dropoff, status, requested_at, fare
  # status: requested -> matched -> in_progress -> completed | cancelled

geo_index
  geohash_cell -> [driver_id, driver_id, ...]   (drivers currently in this cell)
```

The critical design choice is `driver_locations` being a live, overwritten-in-place structure
rather than an append-only log — the system only ever cares about "where is this driver
right now," not the full history of every ping, so there's no reason to accumulate that at
full resolution (a downsampled trip trail is kept separately, at much lower volume, for
receipts/ETAs).

## High-Level Architecture

```
 driver app ──ping every ~4s──► Location Service ──► write current position
                                                        to geo-indexed store
                                                        (geohash or quadtree,
                                                         sharded by region)

 rider app ──request ride──► Matching Service
                                    │
                                    ├──► query geo-index: available drivers
                                    │    near pickup point (geohash prefix
                                    │    match or radius query)
                                    │
                                    ├──► rank candidates (distance, ETA, rating)
                                    │
                                    └──► atomically claim one driver
                                         (compare-and-set on driver status,
                                          prevents double-matching)
                                    │
                                    ▼
                              Rides store (status, fare) ──► push notification
                                                              to both parties
```

## Deep Dives

**Geospatial indexing.** Finding "available drivers within 2 km of this point" against
millions of constantly-moving drivers can't be a linear scan or a naive SQL query — it needs a
spatial index. Two common approaches: geohashing (encode lat/lng into a base32 string where
shared prefixes mean geographic proximity, so a search becomes a prefix range query — trivial
to implement on top of any sorted key-value store) or a quadtree (recursively subdivide space
into cells, denser cells subdivide further, giving finer granularity in dense areas like city
centers and coarser granularity in sparse ones automatically). Uber's own published system
(H3) is a hexagonal-grid variant of the same underlying idea — hexagons avoid some of the
distance-distortion edge cases that square geohash cells have at cell boundaries. All three
solve the same core problem: turn "nearby" into an indexable key range instead of a full scan.

**Matching and avoiding double-booking.** Once candidate drivers near the pickup point are
found, the system has to atomically claim one of them — if two ride requests both try to match
the same driver simultaneously, only one can win. This needs a compare-and-set style update on
the driver's status (`available -> pending_match`, conditioned on it still being `available`)
rather than a read-then-write, or a distributed lock per driver during the matching window.
This is the one place in the system that genuinely needs strong consistency — everything else
(location pings, trip history, pricing estimates) tolerates eventual consistency just fine.

**Surge pricing.** Computed from the real-time ratio of ride requests to available drivers
within a geographic cell (the same geohash/H3 cells used for matching double as the pricing
aggregation unit). This is a streaming aggregation problem — count requests and available
drivers per cell over a sliding recent window, recompute a multiplier periodically (every
minute or so, not per-request, to avoid price flapping) — more akin to the stream-processing
pattern in case-studies/distributed-message-queue.md than to a synchronous per-request
calculation.

**Location update volume.** At 1.25M pings/sec, the location service is the highest-QPS
component in the entire system by a wide margin, and it's write-heavy with a very simple
access pattern (overwrite current position by driver_id). This is a good fit for an in-memory
store sharded by geographic region (so a matching query for "near point X" only has to hit
the shard(s) covering that region, not the whole world) with the geo-index co-located on the
same shards to avoid a cross-shard hop between "find nearby drivers" and "read their current
position."

**ETA computation.** Distinct from matching — once a driver is assigned, computing accurate
ETA and route requires a routing engine (road network graph, real-time traffic data), which is
its own significant subsystem (commonly built on OSRM-style contraction hierarchies or similar
graph-shortest-path precomputation) rather than a simple straight-line distance calculation.
Straight-line distance is fine for the initial "which drivers are roughly nearby" filter in
matching, but not for the ETA shown to the user.

## Bottlenecks & Scaling

Location ping ingestion is the dominant load and scales by sharding geographically — a driver
in Bangalore never needs to be indexed alongside a driver in São Paulo, so partitioning by
region (rather than, say, by driver_id hash) keeps every matching query local to a small
number of shards instead of fanning out globally. The matching service's compare-and-set step
on driver status is the narrow, latency-sensitive, strongly-consistent core of the system and
is kept as small and fast as possible specifically because it can't be scaled away with
eventual consistency the way the rest of the system can.
