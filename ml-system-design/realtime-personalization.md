# Real-Time Personalization System

## Requirements

### Functional
- Personalize a ranked list of items or content for a user within a single session, incorporating
  signals from actions taken seconds ago in that same session — not only stable, long-horizon
  profile signals.
- Serve the full candidate-generation + ranking + re-rank pipeline within a tight end-to-end
  latency budget, tighter than a general recommendation surface, because this system typically
  backs a highly interactive surface (live feed re-ranking after every scroll or interaction,
  search-as-you-type personalization) where the UX contract is "reacts within this interaction,"
  not "loads a feed."
- Reflect an in-session event (a click, an add-to-cart, a dwell signal) in the very next request's
  ranking — not the next batch cycle, and not even the next few minutes.
- Degrade gracefully to session-agnostic personalization when no in-session signal yet exists (a
  cold session), without a separate code path per session-warmth state.

### Non-functional
- End-to-end p99 latency < 100ms for the full request (retrieval + real-time feature fetch +
  ranking + re-rank) — tighter than the 150ms budget in recommendation-system.md specifically
  because of this surface's tighter interaction contract.
- Feature freshness: the round trip from a user action to that action being usable as a ranking
  input must be well under a second — a personalization signal that's still several seconds stale
  by the time the next request lands has already missed the interaction it was meant to react to.
- Session state must survive across requests within a session without requiring the client to
  resend session history on every call, and must be reachable from any serving node handling that
  session's traffic within the latency budget.
- Consistency under partial failure: if the real-time signal fetch times out, the request must
  still complete — falling back to non-real-time signals — rather than failing the whole ranking
  request; a stale-but-present ranking beats no ranking.

## Capacity Estimation

Assume 20M DAU on this surface, each session averaging 15 in-session requests (each scroll or
interaction triggers a re-rank) → 20M × 15 = **300M requests/day** → 300M / 86,400 ≈ 3,470/sec
average, ~10,400/sec peak (3x).

**Latency budget breakdown (100ms total).** ~10ms session-state fetch, ~20ms candidate retrieval
(smaller candidate pool than a general recommendation system, since this system re-ranks a
session-scoped working set rather than retrieving fresh against the full catalog every request —
see deep dive), ~15ms real-time feature fetch (session + recent-event aggregates), ~40ms ranking,
~15ms overhead/network. Every stage here is tighter than the equivalent stage in
recommendation-system.md's 150ms breakdown, which is the direct reason the candidate pool per
request is deliberately kept small rather than retrieved fresh from the full catalog on every one
of the 15 in-session requests.

**In-session event volume.** Assume 8 loggable in-session events/request (impressions plus the
triggering interaction) × 300M requests/day = **2.4B events/day** → 2.4B / 86,400 ≈ 27,800/sec
average, ~83,000/sec peak — this is the stream the real-time feature pipeline (see
real-time-ml-pipeline.md) has to turn into usable ranking features inside the sub-second freshness
budget above.

**Session state store.** 20M concurrent-ish daily sessions × ~2KB session state (recent event
window, working candidate set reference, session-level aggregates) ≈ **40 GB**, sharded by
session_id, sized for read latency rather than raw volume.

**Feature freshness pipeline overhead.** Hitting a sub-second usability bar requires the streaming
aggregation layer to checkpoint and emit at a much finer interval (sub-second micro-batches, or
continuous processing) than the multi-second windows adequate for a slower-moving personalization
signal — this materially raises per-event processing overhead relative to a system with a looser
freshness requirement, and is the direct cost of the tighter freshness SLA above.

## API Design

```
GET  /v1/personalize?session_id=...&surface=live_feed&count=20
     -> 200 { items: [...], model_version,
              latency_breakdown_ms: {retrieval, feature_fetch, ranking} }

POST /v1/session-events        { session_id, user_id, item_id, event_type, ts }
     -> 202  (fire-and-forget into the real-time feature pipeline; ALSO updates
              in-session state synchronously for same-session reads — see deep dive)

GET  /v1/session/{session_id}/state       (internal — used by the personalization
     service itself, not client-facing)
     -> { recent_events: [...], working_candidate_set_ref, session_aggregates }
```

`latency_breakdown_ms` on the response mirrors the same principle as `retrieval_ms`/
`generation_ms` in rag-system.md — at a 100ms total budget with five contributing stages, knowing
which stage is regressing is the minimum observability needed to fix it, and end-to-end latency
alone doesn't tell you that.

## Data Model

```
session_state (online, keyed by session_id, TTL'd to session length + short grace period)
  session_id, user_id, recent_events (bounded ring buffer, last N events),
  working_candidate_set_ref, session_started_at, last_active_ts

realtime_features (online, keyed by session_id / user_id, streaming-updated,
                    sub-second freshness target)
  clicks_this_session, dwell_time_running_avg, last_category_viewed, ...

candidate_pool (per session, refreshed on a coarser cadence than per-request)
  session_id -> [item_id, ...]     (working set retrieved once per session, or every
  few requests / on a trigger like a category switch — re-ranked in place on every
  request rather than re-retrieved from the full catalog each time)

request_log (offline)
  session_id, request_id, items_shown, ranking_scores, chosen_item,
  latency_breakdown, ts
```

The split between `session_state` (raw recent events, written synchronously) and
`realtime_features` (windowed aggregates, written by the streaming pipeline) is deliberate, not
redundant — see the consistency deep dive below for why ranking reads from both rather than
waiting for one to subsume the other.

## High-Level Architecture

```
 client action ──POST /session-events──►
              │
    ┌─────────┴──────────────────────────┐
    ▼                                      ▼
 synchronous in-session state         async into real-time-ml-pipeline.md's
 update (session_state store,         streaming aggregation ──► realtime_features
 low-latency write — the very         (windowed aggregates, sub-second freshness
 next request in this session         target, catches up shortly behind the
 sees this event immediately)         synchronous write)

 client ──GET /personalize──► Personalization service
                                    │
                                    ├─► session_state fetch (session_id) ──►
                                    │    working_candidate_set_ref
                                    │
                                    ├─► working set stale/absent? ──► candidate
                                    │    retrieval (ANN over catalog, query embedding
                                    │    blends long-term + in-session signal) ──►
                                    │    refresh candidate_pool
                                    │
                                    ├─► realtime_features fetch (session + user)
                                    │    — hard timeout, falls back to last-known
                                    │    snapshot on miss (see deep dive)
                                    │
                                    ├─► ranking model scores the working candidate
                                    │    set using realtime_features + standard
                                    │    (non-real-time) features
                                    │
                                    └─► re-rank (diversity, business rules) ──►
                                         response, async log to request_log
```

## Deep Dives

**Session-scoped candidate working set.** Running full candidate retrieval against the entire
catalog on every one of a session's ~15 requests would blow the retrieval-latency budget for no
real benefit — a user's relevant candidate pool doesn't meaningfully shift request-to-request
within one short session the way it does session-to-session. Instead, retrieval runs once (or on a
coarser trigger, like a detected category switch) to produce a working set stored as
`candidate_pool`, and every subsequent request within the session re-ranks that already-retrieved
set using the freshest available signal rather than re-retrieving from scratch. This decouples two
concerns that a naive design conflates: *what's in the candidate pool* (updated on a coarser
cadence, since it changes slowly within a session) from *how that pool is ordered* (updated on
every single request, using the freshest real-time signal available) — the second is what this
system's tight latency and freshness budgets are actually spent on, and the first is explicitly
not re-paid for on every request.

**Sub-second feature freshness pipeline.** Hitting a sub-second freshness bar requires two changes
relative to the general design in real-time-ml-pipeline.md, not just a smaller window size on the
same architecture. First, checkpoint and emit granularity in the streaming aggregation layer has to
be finer — sub-second micro-batches or continuous processing rather than the several-second windows
that are adequate for a slower-moving signal — which raises per-event processing cost materially.
Second, and more importantly, the triggering event is written synchronously and directly into
`session_state`'s raw recent-event ring buffer at write time, bypassing the full streaming-
aggregation round trip entirely for same-session reads — this is what actually guarantees the very
next request in the same session sees the triggering event, since even a well-tuned sub-second
streaming pipeline has some nonzero latency, and "the next request, immediately after," can't
always wait even a few hundred milliseconds for a windowed aggregate to catch up. The streaming
pipeline's windowed aggregates (`realtime_features`) still matter — a raw recent-event list alone
can't answer "average dwell time this session," which needs actual aggregation — they just aren't
the only source ranking reads from.

**Latency budget decomposition and graceful degradation.** Each of the five pipeline stages has
its own tight per-stage budget (10/20/15/40/15ms), and each stage has an explicit behavior for
what happens when it's at risk of blowing that budget, rather than one shared end-to-end timeout.
The real-time feature fetch carries a hard timeout and falls back to the last known non-real-time
feature snapshot rather than blocking the request — a slightly stale ranking is a strictly better
outcome than a timed-out request on an interactive surface. The ranking stage falls back to a
cheaper or smaller model variant if the primary ranker's own p99 is trending toward risking the
overall budget, the same graceful-degradation principle used in recommendation-system.md, but
triggered at a tighter threshold given how much less absolute slack a 100ms budget leaves compared
to a 150ms one. The practical consequence is that this system needs per-stage p99 monitoring, not
just end-to-end p99 — an end-to-end number within budget can still be hiding one stage regularly
eating another stage's margin, which only shows up once that other stage's own load pattern shifts
and the shared budget runs out.

**Cold session handling.** A session with zero in-session events yet has no populated
`working_candidate_set_ref` and no session-scoped `realtime_features`. Rather than special-casing
this as a separate code path, cold-session state simply resolves to sensible defaults within the
identical request flow: candidate retrieval falls back to long-term user-profile-based retrieval
(or population-level defaults for a genuinely new user) when no in-session signal exists yet, and
`realtime_features` reads return population-level defaults rather than nulls that the ranking model
would have to special-case. The first request of a session and the fifteenth request take the
exact same code path — they simply see empty versus populated state — which avoids the class of bug
where a "cold start" branch silently drifts out of sync with the main path over time because it's
tested and maintained far less often.

**Consistency between the synchronous fast-path write and the streaming pipeline.** The
synchronous `session_state` write and the async streaming-pipeline write are two different paths
writing related but non-identical data — `session_state` holds the raw recent-event ring buffer,
`realtime_features` holds the pipeline's windowed aggregates — and they can transiently disagree:
an event is reflected in `session_state` milliseconds before its corresponding aggregate feature
(say, `clicks_this_session`) actually updates downstream. Ranking is built to tolerate this by
design, reading raw recent events directly from `session_state` for signals that need to be
instantaneous (was the last-viewed item in this category) while reading `realtime_features` for
signals that are inherently aggregate and can't be computed from a raw event list alone (a running
average). This is a deliberate departure from real-time-ml-pipeline.md's general pattern of
serving exclusively from the feature store — that pattern is correct when the feature store's own
freshness SLA is tight enough to be the only source of truth; here, the freshness bar is tight
enough that the system needs a faster, narrower path in addition to it.

## Bottlenecks & Scaling

The tightened latency budget leaves less absolute margin at every stage than a general
recommendation system's looser budget, so a latency regression anywhere in the pipeline consumes a
proportionally larger share of the remaining budget and trips the degradation path sooner — this
is the direct reason per-stage p99 monitoring is a harder operational requirement here than in a
system with more slack to absorb noise. Session state store read/write volume scales with
in-session request rate, not DAU — a small number of unusually engaged sessions generating rapid-
fire interactions can concentrate disproportionate load onto a single session's shard, a hot-key
pattern structurally similar to the ones covered in concepts/database-sharding.md, just keyed by
session rather than by a natural entity ID. Streaming pipeline overhead per event is higher than a
system with a looser freshness bar tolerates, because finer checkpoint granularity costs more per
event processed — the real-time feature pipeline backing this system is provisioned and tuned with
freshness, not throughput headroom, as its binding constraint. Finally, the working-candidate-set
refresh cadence is a direct tradeoff between retrieval staleness (a long-lived working set can miss
a genuine shift in what's relevant partway through an unusually long session) and retrieval cost
(refreshing too often reintroduces the full retrieval-latency cost this design exists to amortize
away) — the right cadence is surface-specific, tuned against that surface's typical session length
and interaction density rather than set to one platform-wide constant.
