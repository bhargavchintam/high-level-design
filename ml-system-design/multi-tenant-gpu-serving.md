# Multi-Tenant GPU Serving Platform

## Requirements

### Functional
- Serve many tenants' models — hundreds to thousands, ranging from high-traffic to nearly idle —
  on a shared pool of GPUs, with each tenant's deployment able to scale independently, including
  scaling to zero when idle and back up on demand. Typically sits behind inference-gateway.md,
  which routes requests to the correct tenant deployment; this document is about what happens once
  a request reaches the shared GPU substrate.
- Guarantee resource isolation between tenants sharing physical GPU capacity, so one tenant's
  traffic spike or misbehaving model cannot degrade another co-located tenant's latency or
  correctness.
- Attribute cost accurately back to each tenant — actual GPU-seconds consumed, not a flat
  per-model fee — so shared-infrastructure economics can be passed through fairly.
- Support tenants with wildly different resource footprints, from a full dedicated multi-GPU
  deployment to a tiny fractional-GPU model, under one allocation and billing model.

### Non-functional
- Cold-start latency — from "a scaled-to-zero tenant receives its first request" to "first
  response returned" — must be bounded tightly enough to be viable for interactive, user-facing
  traffic; this is usually the hardest constraint in the entire design, since loading a model onto
  a GPU is inherently slower than a typical serverless CPU cold start.
- Fair-share guarantees must hold even under contention, and must be auditable — a tenant paying
  for a given capacity tier needs to be able to verify they actually received it, not just trust
  that they did.
- Cost attribution must be fine-grained enough (GPU-seconds, not GPU-hours rounded up) to support
  metered billing without materially over- or under-charging any tenant relative to their actual
  consumption.
- Isolation failures — a noisy-neighbor tenant measurably degrading another tenant's p99 latency —
  are an SLA violation, not just a quality issue, and must be monitored and alertable per
  tenant-pair, not only in aggregate.

## Capacity Estimation

**Tenant and traffic distribution.** 3,000 tenants, aggregate 60K req/sec peak, heavily skewed:
top 50 tenants ≈ 70% of traffic (42K req/sec), the remaining ~2,950 tenants share the other 30%
(18K req/sec) — and of those, roughly 2,000 are low-traffic enough to sit scaled-to-zero most of
the time.

**GPU fleet, head vs. long tail.** Top-50 tenants get near-dedicated capacity: at ~1.5
GPU-equivalent average (higher than model-serving-platform.md's general 0.5 GPU-equivalent figure,
reflecting these tenants' higher individual traffic) → 50 × 1.5 = **75 GPU-equivalents**. The long
tail of 2,950 tenants is served through fractional/MIG-partitioned sharing plus scale-to-zero, at
roughly ~0.05 GPU-equivalent average once idle time is accounted for → 2,950 × 0.05 ≈ **148
GPU-equivalents**. Total fleet ≈ 75 + 148 ≈ **223 GPU-equivalents** — versus a hypothetical
~3,000+ GPU-equivalents if every tenant received even a modest permanently-reserved dedicated
fraction. That's better than a **10x efficiency gain** from sharing plus scale-to-zero, and it's
the platform's entire economic justification for existing.

**Cold-start budget, worked example.** For a representative 2GB model artifact: loading from
object storage at ~500MB/sec effective bandwidth ≈ 4 sec, container/runtime startup ≈ 2-5 sec,
CUDA context initialization and warmup ≈ 1-3 sec → an unmitigated cold start lands in the
**~8-15 second range**. Mitigations (deep dive below) bring this down substantially for tenants
that need it, at a direct cost in idle-capacity reservation.

**Metering granularity.** Metering at 1-second GPU-time resolution across 3,000 tenants, rolled up
hourly for billing: a modest number of allocation-change events (scale up/down, MIG reassignment)
per tenant per hour is a small, cheap event stream relative to the request-serving data plane
itself — the append-only usage log is never the platform's bottleneck.

## API Design

```
POST /v1/tenants/{tenant_id}/deployments
     { model_artifact_uri, min_replicas: 0, max_replicas,
       resource_tier: "shared_fractional" | "dedicated" }
     -> 201 { deployment_id }

POST /v1/tenants/{tenant_id}/deployments/{id}/predict
     { inputs }
     -> 200 { outputs, cold_start: bool, latency_ms }

GET  /v1/tenants/{tenant_id}/usage?window=24h
     -> { gpu_seconds_consumed, request_count, cost_estimate }

GET  /v1/cluster/fairness-report
     -> [{ tenant_id, guaranteed_share, observed_share, burst_used }]
     (audit surface backing the fair-share guarantee)
```

## Data Model

```
tenant_deployment
  deployment_id, tenant_id, artifact_uri, resource_tier,
  min_replicas, max_replicas, current_replica_count,
  status: active | scaled_to_zero

gpu_allocation (physical or MIG-partition-level record)
  gpu_id | mig_instance_id -> tenant_id, deployment_id,
  allocation_start_ts

usage_meter (append-only — never updated in place, rolled up on read,
  same reasoning as ml-experiment-tracking-platform.md's metrics_timeseries)
  tenant_id, deployment_id, gpu_seconds_delta, ts

fair_share_policy
  tenant_id -> tier, guaranteed_min_gpu_equivalent, burst_ceiling
```

## High-Level Architecture

```
 request path:
 client ──(via inference-gateway.md)──► routed to tenant's deployment
                                    │
                    deployment scaled to zero? ──yes──►
                        Cold-start path: allocate GPU/MIG slice
                        (gpu-cluster-scheduler.md) ──► load weights
                        ──► warm up ──► request queues briefly, then
                        served
                                    │no
                                    ▼
                        Already-warm replica serves immediately

 allocation / fairness control loop (continuous, independent of any
 single request):
 Fair-share controller ──monitors──► gpu_allocation vs. fair_share_policy
        │
        └─► rebalances: reclaims burst capacity from over-share tenants
             the instant another tenant needs their guaranteed floor;
             triggers scale-to-zero for idle deployments past a timeout

 billing path:
 every allocation change ──► usage_meter (append-only)
        │
        └─► hourly rollup ──► per-tenant gpu_seconds_consumed ──►
             GET /usage
```

## Deep Dives

**Fair-share scheduling across tenants.** This is max-min fairness / weighted fair queueing
applied to GPU-time rather than network bandwidth: each tenant has a guaranteed minimum share (per
their purchased tier) and can burst into currently-unused capacity up to a ceiling, but burst
capacity is reclaimed the instant another tenant needs their guaranteed floor back. This is
conceptually the same fairness problem gpu-cluster-scheduler.md solves for training jobs, applied
to a workload with a very different shape — mostly short, bursty individual requests rather than
long-running gang-scheduled jobs — which changes the rebalancing cadence from "occasional, over
minutes" to "continuous, over seconds."

**Isolation mechanisms.** MIG hardware partitioning gives the strongest isolation guarantee — a
dedicated memory and compute slice with a hard SLA — at the cost of fixed partition granularity
decided when the physical GPU is configured, not per request (cross-ref
gpu-cluster-scheduler.md's MIG deep dive for the underlying partitioning mechanics this platform
consumes). Process/container-level isolation with soft resource limits (cgroups, time-slicing) is
more flexible — any granularity, no hardware reconfiguration — but weaker: a compute-bound
neighbor can still cause latency jitter for a co-located tenant even under configured quotas,
because GPU compute scheduling isn't preemptible at fine granularity the way CPU scheduling is.
The choice between the two is a direct isolation-strength-vs-flexibility tradeoff, not a strictly
dominant option.

**Cold start mitigation.** Layered techniques, roughly in order of cost: keep a small warm pool of
pre-loaded-but-idle replicas for tenants above a traffic threshold rather than scaling fully to
zero, trading idle-capacity cost for near-zero cold starts; pre-fetch and cache model weights on
nodes statistically likely to serve a given tenant next (based on recent activity), so a cold
start only pays container-and-init cost rather than the full weight-download cost; snapshot a
warmed process's memory state — including loaded weights and an initialized CUDA context — and
restore from snapshot rather than reinitializing from scratch, cutting several seconds off the
slowest part of a cold start. The right mix is a genuine cost/latency tradeoff each tenant sits at
a different point on, which is why `resource_tier` is exposed as a tenant-level choice rather than
a single platform-wide constant.

**Cost attribution and metering.** Accurate chargeback needs GPU-second resolution, not GPU-hour
rounding, because at thousands of low-traffic tenants, hour-granularity rounding either
overcharges tenants for brief cold-start-triggered activations (rounding a 3-second activation up
to a full billed hour) or, rounded down instead, silently under-recovers platform cost at that
same scale. Metering also has to account for fractional allocation directly — a tenant occupying
1/7 of a MIG-partitioned GPU should be billed roughly 1/7 of that GPU's rate for the time held, not
a full-GPU rate, which means `usage_meter` records fractional GPU-equivalents rather than a binary
occupied/free flag.

**Noisy-neighbor detection.** Isolation guarantees are only as good as the platform's ability to
detect violations. Per-tenant-pair (or per-node) latency-correlation monitoring — specifically
watching whether one tenant's p99 latency degrades in correlation with a co-located tenant's
traffic spikes — is what turns "isolation" from a design intent into an operationally verified
property. Without this, a subtle isolation leak (e.g. contention on a time-sliced GPU) can persist
for a long time before a tenant's own complaint surfaces it, since aggregate cluster-wide metrics
won't show a problem that only affects one specific tenant pairing.

## Bottlenecks & Scaling

Cold-start latency is the fundamental tension running through the whole design: every mitigation
(warm pools, prefetching, snapshotting) trades away some of the cost savings scale-to-zero exists
to capture in the first place, so the platform is always operating somewhere on a cost/latency
frontier rather than at one universally optimal point — and different tenants deliberately sit at
different points on it via `resource_tier`. Fair-share enforcement gets harder as tenant count and
heterogeneity grow, since the controller has to rebalance fast enough to matter (reclaiming burst
capacity promptly) without thrashing allocations for tenants sitting near their guaranteed floor.
The metering/billing pipeline scales with allocation-change event rate rather than request volume,
which stays manageable even at thousands of tenants since allocation changes are far less frequent
than individual requests. Isolation monitoring has an inherently combinatorial cost — full
pairwise tenant monitoring doesn't stay tractable as tenant count grows — so it has to be
approximated in practice, typically by monitoring only currently co-located pairs rather than
every possible pairing across the whole tenant population.
