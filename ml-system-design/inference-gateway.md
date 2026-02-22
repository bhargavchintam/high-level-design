# Inference Gateway

## Requirements

### Functional
- Provide one stable client-facing API in front of many heterogeneous model backends (LLM
  inference servers, classic ML rankers, embedding models — different frameworks, different
  protocols), routing each request to the correct backend by model name and version.
- Multiplex many models, including a long tail of low-traffic models and LoRA adapters sharing a
  common base model, onto shared GPU capacity rather than requiring every model to have dedicated
  always-on hardware.
- Autoscale each backend pool independently using queue depth / in-flight-request count as the
  primary signal, not raw CPU/GPU utilization percentage — a GPU that's 100% busy running an
  inefficient small batch looks identical to one running an efficient large batch on a utilization
  chart, but the two have very different headroom for more load.
- Enforce per-tenant/per-API-key rate limits and quota, and support multiple concurrent client
  protocols (REST/JSON, streaming SSE, gRPC) fronting the same underlying backend logic.

### Non-functional
- Gateway-added latency (auth, rate-limit check, routing decision) should be a small, bounded
  slice of the request budget — single-digit milliseconds at p99 — since it sits in front of
  latency-sensitive backends like llm-inference-serving.md's sub-300ms TTFT target, and the
  gateway must never become the dominant term in that budget.
- Gateway availability must exceed the availability of any single backend it fronts — a gateway
  outage takes every model behind it down simultaneously, which is why it's over-provisioned and
  deployed with more redundancy than a typical backend fleet.
- Capacity must be fairly shared across tenants hitting the same backend pool, so one tenant's
  burst doesn't blow another tenant's latency SLA — cross-ref multi-tenant-gpu-serving.md for the
  deeper fairness/isolation mechanics this gateway ultimately depends on at the GPU level.
- Autoscaling reaction time is fundamentally bounded below by backend cold-start time (see
  multi-tenant-gpu-serving.md's cold-start deep dive); the gateway's queueing and backpressure
  exist specifically to absorb the gap between "load arrived" and "capacity is ready."

## Capacity Estimation

**Aggregate load.** ~150K requests/sec peak across ~300 distinct model endpoints, heavily skewed —
consistent with model-serving-platform.md's pattern, the top 20 endpoints account for roughly 80%
of volume, with a long tail of low-traffic models and adapters sharing the rest.

**Gateway compute footprint.** Per-request gateway work (auth check, rate-limit decrement, route
lookup) is lightweight — call it ~0.3ms of CPU time. At 150K req/sec: 150,000 × 0.3ms = 45,000ms
of CPU-work/sec ≈ **45 CPU-cores' worth of pure gateway logic** — trivially horizontal-scaled since
the gateway itself is stateless per request, in sharp contrast to the GPU-bound backends it fronts.

**Rate-limit state.** ~10,000 active API keys, each with a token-bucket counter checked on every
request routed under that key. At 150K req/sec aggregate, that's up to 150K/sec read-modify-write
operations against the rate-limit store — structurally the same batched, sharded-KV read pattern
as feature-store.md's online store, just with a write on every read rather than a pure read
(cross-ref that document's hot-key mitigations, since a small number of very-high-QPS API keys can
create the same skew problem a viral item creates for a feature store).

**Autoscaling responsiveness.** For an LLM backend pool autoscaled on queue depth (target: ≤5
queued requests per replica before scale-out, per llm-inference-serving.md's autoscaling-signal
discussion), a new replica takes on the order of 90 seconds to 3 minutes to come online (weight
load + warmup, see multi-tenant-gpu-serving.md's cold-start estimate). During that window, the
gateway's queue is the only thing absorbing the burst — at, say, 500 req/sec of unmet demand for
90 seconds, that's up to 45,000 requests that must be queued (bounded by a max-wait SLA) or shed,
not silently dropped or left to time out unmanaged.

## API Design

```
POST /v1/{model_name}/{version?}/invoke
     { inputs, stream: bool }
     -> routes to the backend pool registered for model_name+version;
        streams via SSE if requested and the backend supports it

GET  /v1/gateway/routes
     -> [{ model_name, version, backend_pool_id, protocol, streaming_supported }]

PUT  /v1/gateway/rate-limits/{api_key}
     { requests_per_min, tokens_per_min?, burst }
     -> 200

GET  /v1/gateway/health
     -> { backend_pool_id -> { queue_depth, replica_count, p99_latency_ms } }
```

## Data Model

```
route_table (routing config, read on every request — must be low-latency and
  propagate fast on change)
  model_name, version -> backend_pool_id, protocol, streaming_supported

backend_pool (one per logical model deployment)
  pool_id -> replica_endpoints[], current_queue_depth,
  autoscale_policy: {min_replicas, max_replicas, target_queue_depth}

rate_limit_state (hot, high-write-volume, sharded)
  api_key -> { tokens_remaining, tokens_per_min_limit,
               tpm_remaining?, tpm_limit?, last_refill_ts }

request_log (sampled)
  request_id, api_key, model_name, backend_replica,
  queue_wait_ms, total_latency_ms, ts
```

## High-Level Architecture

```
 control path:
 model owner ──registers──► route_table + backend_pool config
                                    (propagated to every gateway replica,
                                     no restart required)

 data path (per request):
 client ──POST /invoke──► Auth/authn
                                    │
                                    ▼
                          Rate limiter (token-bucket check against
                          rate_limit_state; 429 if exceeded)
                                    │
                                    ▼
                          Router (route_table lookup: model_name+version
                          -> backend_pool_id)
                                    │
                                    ▼
                          Backend pool: request queue + replicas
                          (replica may itself multiplex several small
                          models/adapters — see multiplexing deep dive)
                                    │
                          queue depth > threshold? ──► autoscaler signals
                          scale-out (new replica provisioned via
                          gpu-cluster-scheduler.md / multi-tenant-gpu-
                          serving.md)
                                    │
                                    ▼
                          response (streamed if requested) returned to
                          client; sampled request logged
```

## Deep Dives

**Routing and model multiplexing.** For a small model or a LoRA adapter that doesn't justify its
own dedicated replica, the gateway can route requests to a single backend process that serves
several logical models by swapping adapters per request against a shared base model, rather than
provisioning one replica per model (cross-ref model-serving-platform.md's multi-model serving deep
dive for the bin-packing/co-location angle). The gateway's specific job here is the routing
*decision*: recognizing at request time which models share a base and can be co-batched onto the
same physical backend, which is a routing-table property (which pool a model_name maps to), not a
placement-time bin-packing decision — those are complementary, not the same problem.

**Queue-depth-based autoscaling.** Raw QPS or CPU% are misleading autoscaling signals across
heterogeneous backends for the same reason llm-inference-serving.md's autoscaling deep dive
rejects them for a single LLM fleet: request cost varies wildly, both within one model (a 50-token
vs. 4,000-token generation) and — at the gateway's scope — *across* very different models (a
sub-millisecond classic ML score vs. a multi-second LLM generation). The gateway generalizes that
single-model insight across an arbitrary mix of backends by normalizing on queue depth /
estimated time-to-drain per pool rather than trying to define one cost metric that means the same
thing for every model type it fronts.

**Rate limiting and quota enforcement.** A token-bucket per API key (refill rate = quota/sec,
bucket size = allowed burst) is the standard mechanism (cross-ref concepts/rate-limiting.md for
the general algorithm). For LLM backends specifically, a single request-count limit under- or
over-throttles because a request's true cost is dominated by tokens generated, not request count —
production LLM gateways typically enforce two simultaneous limits, requests/min and tokens/min,
with the tighter of the two binding. This mirrors why llm-inference-serving.md's capacity
estimation sizes the fleet by output-tokens/sec rather than requests/sec: cost and capacity both
live in token space, so rate limiting has to as well for the limit to actually protect backend
capacity rather than just request volume.

**Backpressure and load shedding.** When queue depth exceeds a threshold faster than autoscaling
can add capacity (bounded below by the 90-second-to-3-minute cold-start figure above), the gateway
has two options: queue further, bounded by a max-wait SLA before the request times out, or shed
load outright (reject with 429) starting with the lowest-priority/lowest-tier tenants. This is the
gateway's own admission-control layer, distinct from and in front of any batching-admission
decision happening inside an individual backend replica — the gateway decides whether a request
gets a chance to queue for a backend at all, the backend decides how to batch what it's already
accepted.

**Protocol adaptation and streaming.** Backends behind the gateway can speak genuinely different
native protocols — a raw gRPC model server, an HTTP/SSE LLM server — while clients see one
consistent API. For streaming responses specifically, the gateway has to proxy a long-lived
connection through without buffering the full response to translate between protocols; buffering
an SSE token stream to re-emit it as, say, a different wire format would reintroduce exactly the
TTFT-equals-total-latency problem llm-inference-serving.md's streaming design exists to avoid —
tokens have to pass through the gateway as they're produced, not after the backend finishes.

## Bottlenecks & Scaling

The gateway itself must scale statelessly and horizontally faster than any backend it fronts,
since it's a shared dependency for every model's traffic — any gateway-tier slowdown multiplies
across all 300 endpoints simultaneously rather than affecting one model's users. The rate-limit
state store becomes hot at high API-key cardinality and request rate, requiring the same
key-sharding discipline as any high-read-and-write KV workload (cross-ref feature-store.md's
hot-key mitigations). Autoscaling lag — bounded below by backend cold-start time, not by anything
the gateway itself controls — is the fundamental floor on how fast the system reacts to bursts,
which is why generous queueing/backpressure headroom is load-bearing infrastructure here, not a
nicety to trim under cost pressure. Finally, route-table propagation latency matters more than it
looks: a newly deployed model or a traffic-policy change has to reach every gateway replica
quickly and without a restart, since even a few seconds of staleness at 150K req/sec means a
non-trivial number of requests routed against a stale configuration.
