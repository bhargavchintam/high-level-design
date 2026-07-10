# GPU Fleet Autoscaler

## Requirements

### Functional
- Scale GPU capacity up and down across a shared fleet serving many independent workloads
  (online inference services, training jobs, batch scoring jobs) based on each workload's own
  demand signal — queue depth, an SLA-derived target, or a custom metric — not one
  platform-wide scaling rule applied uniformly.
- Bin-pack heterogeneous GPU requests (different GPU types — H100, A100, L4 — and different
  whole-GPU vs. fractional/MIG-partitioned requirements) onto a fleet of mixed instance types,
  rather than assuming a single GPU SKU the way a single-service autoscaler can.
- Mix spot/preemptible and on-demand capacity per workload according to that workload's own
  interruption tolerance, and automatically reclaim and rebalance placement when spot capacity is
  preempted.
- Predictively pre-provision capacity ahead of known or forecasted demand (diurnal traffic,
  scheduled batch jobs, a planned launch) to absorb GPU node boot time, rather than reacting to
  load only after it has already queued.

### Non-functional
- Scale-up decision-to-capacity-ready latency is dominated by node boot, driver/runtime
  initialization, and model/weight load — typically minutes, not seconds — and this is a
  structural floor reactive scaling cannot beat; burst absorption below that floor has to come
  from queueing and headroom on already-running capacity, not from a faster reactive loop.
- Aggregate fleet utilization, not any single workload's isolated efficiency, is the primary cost
  metric the autoscaler is judged on — idle GPU-hours anywhere in the shared fleet are the
  dominant line item, and a design that optimizes one workload's headroom at the expense of fleet-
  wide packing efficiency is optimizing the wrong thing.
- Priority isolation is required, not best-effort: a demand spike in a low-priority batch job must
  not be able to starve a latency-sensitive inference service of capacity it needs to hold its
  SLA.
- Preemption handling must complete within a bounded time per workload class — seconds for
  inference (fast failover to remaining replicas) and minutes for training (checkpoint-triggered
  resume) — because at this fleet size, preemptions are routine, recurring load, not an
  exceptional incident each one has to be individually triaged as.

## Capacity Estimation

Fleet: 5,000 GPUs across mixed types — 3,000 H100, 1,500 A100, 500 L4 (a cheaper class reserved
for inference-only, latency-tolerant workloads) — spanning roughly 600 physical/instance nodes.
Workload mix: 60% of capacity allocated to online inference (needs immediate, low-latency
scaling), 30% to training (long-running, more schedule-tolerant), 10% to batch/offline scoring
(most preemption-tolerant, most spot-eligible).

**Scaling-decision rate.** Assume ~150 independently-scaled inference services registered with the
autoscaler, each evaluated on a 30-second reactive interval → 150 / 30s = **5 scaling decisions/
sec** platform-wide from the reactive loop alone — cheap in raw compute, but every decision fans
out into a placement/bin-packing calculation against current fleet state, which is the actual cost
center (see deep dive).

**Boot latency.** GPU node provisioning (cloud instance boot, GPU driver init, container/runtime
pull) runs 3-5 minutes; loading a large model's weights onto that node adds another 1-3 minutes
(pulled from object storage, the same weight-loading path described in
llm-inference-serving.md) → **total scale-up latency of roughly 4-8 minutes**, decision to
capacity actually serving traffic.

**Predictive scaling horizon.** To fully absorb the 4-8 minute boot floor, forecast-driven
pre-provisioning needs to act at least 8 minutes ahead of predicted demand; in practice, the
forecaster targets a 15-20 minute horizon, refreshed every few minutes, to leave margin for
forecast error rather than provisioning right up against the deadline.

**Preemption rate.** At 30-40% of fleet capacity on spot (~1,500-2,000 GPUs) and a typical cloud
spot interruption rate on the order of a few percent of instance-hours/day for in-demand GPU
SKUs, expect **roughly 50-100 preemption events/day** across the fleet — frequent enough that the
scheduler has to treat preemption as routine background load with its own steady-state handling
path, not as an incident triggering manual intervention each time.

**Bin-packing frequency.** Every fleet-state change (a node joins, a node is preempted, a workload
scales) could in principle trigger a full re-solve of the placement problem, but at ~600 nodes and
150+ independently-scaled workloads a full re-solve on every event is too expensive to run inline.
Placement is instead incremental (fit the changed workload against currently free capacity) with a
periodic full-defragmentation pass — hourly is a reasonable default — to reclaim the fragmentation
that incremental-only placement accumulates over time.

## API Design

```
POST /v1/workloads/{workload_id}/scaling-policy
     { min_replicas, max_replicas, scale_signal: queue_depth|utilization|custom,
       target, priority_class, gpu_type, spot_eligible_pct }
     -> 200

GET  /v1/fleet/capacity
     -> { by_gpu_type: [{type, total, allocated, free, spot_pct}],
          nodes_provisioning, nodes_draining }

POST /v1/fleet/reserve                      (internal — issued by a workload's scaling decision)
     { workload_id, gpu_type, count, priority_class }
     -> 202 { reservation_id, eta_ready_s }

POST /v1/fleet/preemption-webhook           (cloud-provider-invoked, spot reclaim notice)
     { node_id, reclaim_at }
     -> 200   (triggers drain/reschedule for every workload placed on node_id)
```

`priority_class` appears on both the scaling policy and the reserve call because a reservation
isn't just "give me capacity" — it's "give me capacity, and if none is free, decide whether to
queue behind or preempt a lower-priority workload's allocation," which is the mechanism
underneath the fairness requirement above.

## Data Model

```
workload (one per independently-scaled service/job registered with the autoscaler)
  workload_id, type: inference|training|batch, scaling_policy, priority_class,
  current_replicas, gpu_type, spot_eligible_pct

fleet_node (one per physical/instance node)
  node_id, gpu_type, gpu_count, status: provisioning|ready|draining|preempting,
  capacity_type: spot|on_demand, allocated_to: [{workload_id, gpu_fraction}]

scaling_decision_log (append-only, one row per scaling action taken)
  workload_id, ts, action: scale_up|scale_down|reject_no_capacity, delta_replicas,
  trigger: reactive|predictive, signal_value

demand_forecast (per workload, rolling)
  workload_id, horizon_ts, predicted_signal_value, confidence_interval, model_version
```

`fleet_node.allocated_to` supports fractional GPU allocation (`gpu_fraction`) rather than only
whole-node or whole-GPU assignment, because a low-traffic inference service on an L4 doesn't need
a full GPU, and packing several such services onto shared GPU fractions (via MIG partitioning or
time-slicing) is a meaningful share of total achievable utilization at this fleet's workload mix.

## High-Level Architecture

```
 per-workload scaling signal (queue depth / utilization / custom)
              │
              ▼
 Reactive scaler loop (per workload, ~30s interval) ──┐
                                                        │
 Predictive forecaster (per workload, trained on        ├──► target replica count
 historical demand; outputs forecast ahead of the        │
 4-8min boot floor, ~15-20min horizon)  ─────────────────┘
              │
              ▼
 Placement / bin-packer: target counts across all workloads x current
 fleet_node free capacity x priority_class
              │
    ┌─────────┴──────────┐
    ▼                     ▼
 fits in free       insufficient capacity for a lower-priority
 capacity ──►        workload given current fleet ──► queue,
 schedule onto        or preempt a lower-priority allocation if
 existing node        this request's priority_class permits it
    │
    ▼ (if new nodes needed)
 Cloud provisioning (node boot, 3-5min) ──► node joins fleet_node
 as ready ──► workload's replica scheduled onto it ──► weight/model
 load ──► serving

 preemption path:
 cloud spot-reclaim notice ──► preemption-webhook ──► node marked draining
              │
    ┌─────────┴──────────┐
    ▼                     ▼
 inference workloads   training workloads on this node:
 on this node: fast     checkpoint-triggered (see
 failover — traffic     distributed-training-system.md) —
 drains to remaining    job's scheduler resumes the affected
 replicas, replacement  parallel group on replacement capacity
 requested

 periodic defragmentation pass (hourly): re-solve packing across
 the fleet to consolidate free capacity fragmented by incremental
 placement decisions since the last pass
```

## Deep Dives

**Reactive vs. predictive scaling and the boot-latency floor.** Queue-depth-based reactive scaling
— the mechanism a single-service autoscaler like the one in llm-inference-serving.md relies on —
is necessary but structurally insufficient at fleet scale for one reason: a decision made the
instant load crosses a threshold still takes 4-8 minutes to turn into serving capacity, and a
burst of demand rarely waits politely for that window. Predictive scaling closes this gap by
forecasting demand far enough ahead (15-20 minutes) that pre-provisioned capacity is already
`ready` by the time actual load arrives — but forecasting is a tradeoff, not a free upgrade:
over-forecasting pre-provisions capacity that then sits idle, directly costing the utilization
metric this whole system is judged on; under-forecasting leaves the reactive loop to absorb the
gap through queueing, which risks breaching latency SLAs for workloads that can't tolerate a
multi-minute queue wait. Production systems run both simultaneously by design, not as a fallback
pair: predictive scaling handles the slow-moving, forecastable trend (diurnal load, a scheduled
batch job's known start time), while reactive scaling handles the residual error the forecast
didn't capture, with request queueing as the last line of defense for whatever's left over.

**Bin-packing heterogeneous GPU requests.** Unlike a single-service autoscaler provisioning one
GPU type for one workload, this system has to place workloads with materially different
requirements — some need a full H100 for a large model's memory footprint, some fit comfortably in
a MIG-partitioned fraction of an A100, some are GPU-type-agnostic and can run on whatever's free —
onto a fleet where node composition itself changes continuously as spot capacity comes and goes.
Full re-optimization of the placement problem on every fleet-state change is combinatorially
expensive at 600 nodes and 150+ workloads, so placement runs incrementally: a new or changed
workload is placed against currently free capacity using a greedy best-fit pass, not against a
fully re-solved global optimum. This is fast but accumulates fragmentation over time — small
freed slivers of capacity scattered across many nodes that, individually, don't fit any pending
workload's requirement even though their sum would. The periodic full-defragmentation pass exists
specifically to reclaim that accumulated fragmentation by consolidating workloads onto fewer
nodes and freeing whole nodes back to the pool, trading a burst of rescheduling churn (acceptable
during a scheduled, low-traffic maintenance window) for materially better steady-state packing
density than incremental placement alone would ever reach.

**Spot/on-demand mix and preemption handling.** Spot eligibility is set per workload as a function
of that workload's own interruption tolerance, not a platform-wide spot percentage — inference
workloads under a tight latency SLA typically run mostly on-demand with only a modest spot
fraction (since a preemption has to be masked from the end user within seconds), while training
and batch workloads, which already have to tolerate node failure as a routine event (see
distributed-training-system.md), can run a much larger fraction on spot at a meaningful cost
discount. The two workload classes need genuinely different preemption responses, not a shared
generic one: inference relies on fast failover, where the load balancer simply stops routing to
the draining replica and traffic redistributes across remaining replicas while a replacement is
requested in the background, invisible to callers if capacity headroom exists; training relies on
the checkpoint-and-resume mechanics native to the training system itself, since a training job's
state can't simply "fail over" the way a stateless inference replica can. At 50-100 preemption
events/day, both paths have to be fully automated steady-state handling, not an on-call runbook —
manually triaging even a fraction of that daily event volume would consume more engineering time
than the spot discount is worth.

**Priority-based preemption across workload classes.** Queueing alone is an incomplete answer to
capacity contention: if inference demand spikes while the fleet is fully allocated to
lower-priority batch scoring jobs, waiting behind the queue for batch capacity to naturally free up
can take far longer than an SLA-bound inference service can tolerate. Priority-based preemption
lets a high-priority reservation request reclaim capacity from a lower-priority workload's current
allocation directly, draining and rescheduling the lower-priority workload's affected replicas
rather than making the high-priority request wait. The guardrail this needs is symmetric to the
one guardrail in every priority-preemption system: without a bound on how often and how much a
given low-priority workload can be preempted, it can be starved indefinitely by a
sufficiently persistent stream of higher-priority demand, so preemption is typically rate-limited
per lower-priority workload (a cap on preemptions per hour, or a minimum guaranteed capacity floor
below which even a high-priority request can't reclaim further) — priority isolation protects the
high-priority workload's SLA, and the preemption rate limit protects the low-priority workload
from never making forward progress at all.

**Cold-start mitigation.** For workloads whose demand doesn't forecast well (a sudden, unscheduled
traffic spike with no diurnal pattern to extrapolate from), predictive pre-provisioning has
nothing reliable to act on, which pushes the full 4-8 minute boot latency back onto the reactive
path. Warm pools — a small number of nodes kept booted, driver-initialized, and idle specifically
to skip the 3-5 minute node-boot step — trade a fixed idle-capacity cost for near-elimination of
that portion of scale-up latency, leaving only the smaller weight-load step (1-3 minutes) on the
critical path for a scale-up event. Model-weight prefetching and local caching on warm-pool nodes
(keeping a hot set of frequently-deployed model weights already resident, rather than pulling from
object storage on every scale-up) shrinks that remaining step further. Warm pools are sized as a
deliberate cost/latency tradeoff per workload class — generous for the highest-priority, least-
forecastable inference services where a multi-minute cold start would breach SLA, minimal to
nonexistent for batch and training workloads that already tolerate scheduling delay as part of
their normal operating profile.

## Bottlenecks & Scaling

Node boot latency is the hard structural floor this entire system is designed around, not a
parameter to be optimized away — every mechanism above (predictive scaling, warm pools, weight
prefetching) exists specifically to work around that floor for the workloads that can't tolerate
it, rather than to shrink the floor itself. Bin-packing and defragmentation quality degrade as
fleet heterogeneity grows — more GPU types, more fractional-allocation granularity — trading
placement optimality against the decision latency budget available for each incremental placement,
which is the concrete reason a full re-solve isn't run on every event even though it would in
principle pack better. Priority-preemption logic is the component most exposed to subtle fairness
bugs at this scale, because a starvation bug shows up as one team's jobs silently never running,
not as a visible aggregate utilization regression — it needs monitoring specifically on
per-workload preemption frequency and time-to-capacity, not just fleet-wide utilization, or the
bug can persist for a long time before anyone notices. Finally, demand forecast accuracy is
inherently workload-pattern-dependent — diurnal consumer traffic forecasts well, a genuinely novel
spike (a launch, a viral event) does not — and the autoscaler has to detect low forecast confidence
per workload and fall back gracefully to pure reactive scaling with queueing headroom, rather than
trusting a forecast blindly and under-provisioning ahead of a spike it was never going to predict
correctly.
