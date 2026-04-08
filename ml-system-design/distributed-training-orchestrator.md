# Distributed Training Orchestrator

## Requirements

### Functional
- Schedule many concurrent, independent training jobs (each itself internally parallelized per
  distributed-training-system.md) onto a shared multi-tenant GPU cluster, admitting a job only
  when its full required resource set is available as one atomic allocation — gang scheduling —
  not partially.
- Support elastic training jobs whose world size (number of participating workers/GPUs) can grow
  or shrink during a run without a full manual restart, reacting automatically to cluster capacity
  changes — a node evicted, capacity freed by another job completing.
- Coordinate checkpoint and restore at the scheduling layer: trigger an out-of-band checkpoint
  ahead of a planned preemption, and resume a preempted or failed job from its last checkpoint on
  newly allocated capacity without the job owner manually intervening.
- Detect straggling workers within a running job — a node or rank meaningfully slower than its
  peers, which silently caps the whole job's step time since synchronous training runs at the
  speed of its slowest participant — and take corrective action.

### Non-functional
- Gang scheduling must avoid resource-fragmentation deadlock — multiple large jobs each holding a
  partial allocation, none able to acquire the rest of what they need because every other job is
  also holding a partial allocation — a structurally different failure mode from ordinary queueing
  delay, requiring its own admission-control discipline.
- Preemption and elastic resize events must not corrupt training correctness — a job resuming with
  a different world size must produce a training run whose loss curve is still a legitimate
  continuation of the pre-resize run, not silently invalid.
- Scheduling decisions at cluster scale (thousands of GPUs, tens to hundreds of concurrent jobs)
  must complete in well under the time a training step itself takes, so the scheduler is never the
  bottleneck on job start or resize latency.
- Fairness across tenants sharing the cluster must be enforced by policy — priority classes,
  guaranteed minimum allocations, fair-share queuing — not by whichever job's launch script
  happened to submit first.

## Capacity Estimation

Cluster: 10,000 GPUs across ~1,250 nodes (8 GPUs/node) — larger than any single training run in
distributed-training-system.md's 2,048-GPU example, because this is the shared multi-tenant pool
many such runs draw from concurrently, not one run's dedicated allocation.

**Concurrent jobs.** Assume 60 concurrent training jobs at any time, spanning a wide size
distribution: a handful of large jobs (512-2,048 GPUs each) and a long tail of smaller jobs
(8-128 GPUs) — roughly matching real-world cluster job-size distributions, where a small number of
large jobs consume a majority of aggregate GPU-hours.

**Scheduling event rate.** Job submissions, completions, node-loss-triggered resizes, and
priority-preemption evictions together generate on the order of ~200 scheduling-relevant
events/hour across the cluster → **roughly one event every 18 seconds** requiring a scheduling
decision, each of which has to resolve against a state space of ~1,250 nodes well within a small
number of seconds — comfortably faster than a training step, which typically runs sub-second to a
few seconds for a well-tuned large model, so the scheduler doesn't become the throughput
bottleneck it would be if a decision took as long as the step it's gating.

**Straggler detection volume.** Per-step timing telemetry collected from every rank of every job:
at 60 jobs averaging ~256 ranks/job ≈ **15,360 ranks** reporting step-time telemetry. At a step
cadence of roughly 1/sec for a representative job, that's ~15,360 timing samples/sec
platform-wide to ingest and compare against peer ranks within the same job — cheap in raw volume,
but latency-sensitive: a straggler needs to be caught within a handful of steps, not after hours
of quietly degraded throughput.

**Checkpoint coordination load.** Per distributed-training-system.md, a large job checkpoints
roughly every 20-30 minutes. Across 60 concurrent jobs staggered in time, that's on the order of
**2-3 checkpoint events/minute cluster-wide** the orchestrator needs visibility into — not to move
checkpoint bytes (already sized in distributed-training-system.md), but to know timing: avoiding
triggering a preemption mid-checkpoint-write, and knowing each job's last-checkpoint recency when
deciding preemption/eviction order.

**Fragmentation risk.** With job sizes spanning 8 to 2,048 GPUs on a 10,000-GPU/1,250-node
cluster, naive first-fit placement can strand small pockets of free capacity (say, 40 free GPUs
scattered across 10 different nodes) that no pending large job can use — despite the cluster
having enough aggregate free capacity in principle. This is the core scheduling problem gang
scheduling plus periodic defragmentation exists to bound (see deep dive).

## API Design

```
POST /v1/jobs
     { job_name, min_workers, max_workers, gpu_type, priority_class,
       elastic: bool, checkpoint_interval_min }
     -> 202 { job_id, status: "pending_admission" }

GET  /v1/jobs/{job_id}/status
     -> { status: pending|gang_scheduled|running|resizing|checkpointing|
                   preempted|completed|failed,
          current_world_size, nodes: [...] }

POST /v1/jobs/{job_id}/checkpoint-now      (orchestrator-triggered — e.g. ahead of
     a planned preemption)
     -> 202

POST /v1/jobs/{job_id}/resize              (internal — triggered by a capacity
     change for an elastic job)
     { target_world_size }
     -> 202

GET  /v1/cluster/scheduling-state
     -> { pending_jobs: [...], running_jobs: [...], fragmented_capacity_estimate }
```

`min_workers`/`max_workers` rather than a single fixed `workers` count is what makes a job
elastic-eligible in the first place — a job submitted with `min_workers == max_workers` is telling
the scheduler it must be gang-scheduled at exactly that size and never resized, which the
orchestrator treats as a materially different (and less flexible) scheduling and preemption target
than a job with real elastic range.

## Data Model

```
training_job (control-plane record, one per submitted job)
  job_id, job_name, min_workers, max_workers, current_world_size, gpu_type,
  priority_class, elastic: bool, status, checkpoint_interval_min,
  last_checkpoint_ts, last_checkpoint_step

gang_allocation (one per admitted job's current resource grant)
  job_id, node_ids: [...], allocated_at, status: active|draining

rank_health (per rank of a running job, updated continuously)
  job_id, rank_id, node_id, last_step_ts, step_duration_ms,
  status: healthy|straggling|failed

scheduling_queue (pending jobs awaiting gang-scheduling admission)
  job_id, priority_class, requested_gpus, requested_at, fairness_weight
```

`rank_health` is keyed at rank granularity, not node granularity, because a straggler is a
per-process phenomenon (one rank on an otherwise-healthy node can lag its peers from something as
mundane as a noisy-neighbor process sharing that node) — node-level health alone would miss this
class of degradation entirely.

## High-Level Architecture

```
 Job submission ──► Admission control / gang scheduler
                          │
             is there a placement satisfying min_workers as one
             atomic allocation, right now?
                          │
              ┌───────yes─┴──no───────────┐
              ▼                            ▼
    gang_allocation created,      scheduling_queue (priority + fair-share
    job -> running, workers        ordered), periodically re-evaluated as
    launched on allocated nodes    capacity frees or a reservation matures

 Straggler monitor: ingests rank_health telemetry every step, across all
 running jobs
        │
        ▼
 per-job peer comparison (this rank's step time vs. this job's median) ──►
 straggling rank flagged ──► corrective action: soft (deprioritize/deweight
 in next collective) or hard (evict + replace node, trigger resize/resume)

 Elastic resize coordinator: cluster capacity change (node freed by a
 completing job, node lost to failure/preemption)
        │
   ┌────┴─────────────────────────┐
   ▼                                ▼
 elastic job eligible to grow/     non-elastic job affected by a lost node:
 shrink ──► triggers /resize ──►    checkpoint-resume cycle — last checkpoint
 job's own training framework       located ──► new gang allocation
 (distributed-training-system.md)   requested ──► job resumes at last
 handles the actual world-size      checkpoint on new nodes
 change / re-sharding math

 Preemption coordinator: higher-priority job needs capacity held by a
 lower-priority job
        │
        ▼
 issues checkpoint-now to the lower-priority job ──► waits (bounded) for
 completion or timeout ──► evicts ──► lower-priority job re-enters
 scheduling_queue
```

## Deep Dives

**Gang scheduling and fragmentation avoidance.** Starting a large job with fewer workers than
`min_workers`, hoping to grow into the rest as capacity frees up, seems like a reasonable way to
improve utilization — but at cluster scale, if many large jobs do this simultaneously, each one
can end up holding a partial allocation while waiting on the others to release capacity that would
let it complete its own allocation, and none of them ever reach `min_workers` — a resource-holding
deadlock, not merely a delay. Atomic all-or-nothing admission avoids this at the cost of
utilization: a job either gets its full `min_workers` allocation at once or it waits, holding
nothing in the interim. The practical refinement is a bounded reservation: once the scheduler
determines a large job's requirement will be satisfiable soon (capacity actively draining toward
availability), it reserves that capacity for a bounded time rather than either fully blocking new
allocations against it or giving up and losing the placement to a scramble of smaller jobs.
Backfill scheduling fills the gap productively during that reservation window — smaller jobs that
fit into currently-free capacity *and* will complete or release before the reservation matures are
allowed to run in the gap, so the large job's wait doesn't leave capacity sitting idle in the
meantime.

**Elastic training orchestration.** This system and distributed-training-system.md split the
elastic-training problem cleanly along a control-plane/data-plane boundary. The orchestrator
decides *when* and *by how much* to resize a job — a capacity-availability and scheduling-policy
decision — and handles the mechanics of handing new or removed nodes to the job's process group.
The training framework running inside the job (not this system) handles the actual world-size-
change math: re-sharding ZeRO-partitioned optimizer state and gradients across the new rank count,
adjusting data-parallel degree, and resuming the training loop with a consistent view of progress —
exactly the mechanics covered in distributed-training-system.md. This split matters for a concrete
scheduling reason: elastic jobs are strongly preferred preemption and backfill targets over
non-elastic ones, because shrinking an elastic job to free some of its capacity is far cheaper —
in both lost progress and orchestration complexity — than fully evicting and later fully
re-admitting a non-elastic job's entire fixed-size allocation.

**Checkpoint/restore orchestration at the scheduling layer.** The orchestrator deliberately does
not own checkpoint mechanics — what's actually in a checkpoint, how it's sharded, how fast it can
be written, are all distributed-training-system.md's concerns. What the orchestrator does own is
checkpoint *timing* relative to scheduling decisions. Ahead of a planned preemption (a
higher-priority job needs this capacity), the orchestrator triggers an out-of-band checkpoint via
`/checkpoint-now` so the evicted job loses only the partial progress made since its last regular
checkpoint interval, rather than up to a full interval's worth. `training_job.last_checkpoint_ts`
and `last_checkpoint_step` are tracked specifically to inform eviction *order* when multiple
candidate victims exist at the same priority — a job that just checkpointed is a strictly cheaper
eviction target than one 25 minutes into its interval, all else equal, and the orchestrator uses
this to minimize aggregate lost progress across the fleet rather than treating same-priority
candidates as interchangeable.

**Straggler detection and mitigation.** The same synchronous collective-communication dependency
that makes hardware failure an existential threat to a training run in distributed-training-
system.md also makes a slow-but-not-failed rank a pure throughput problem, distinct from and often
harder to detect than an outright failure: the job doesn't crash, doesn't get evicted, and doesn't
trip any health check — it just silently runs at the speed of its slowest participant indefinitely.
Detection compares each rank's step time against its own job's median step time, not a fixed
global threshold, because acceptable step time varies enormously by job and model size — a
threshold tuned for a 2,048-GPU job would be meaningless applied to an 8-GPU job's step-time
distribution. The escalation path runs soft-to-hard: a mildly lagging rank is first deprioritized
or deweighted in the next collective operation if the framework supports it, and only a
persistently or severely lagging rank triggers hard replacement (evict the node, request a
replacement allocation, resume the affected parallel group) — jumping straight to hard replacement
on every minor step-time fluctuation would trade a real but usually-transient throughput dip for
guaranteed, avoidable node-churn overhead.

**Multi-tenant fairness and priority.** Individually well-behaved jobs can still collectively
starve each other absent an explicit fairness policy — a team that happens to submit large jobs
first, or submits continuously, can monopolize the cluster indefinitely under naive first-come
scheduling even without doing anything wrong by their own local logic. Fair-share and
priority-class queuing address this at admission time; preemption policy addresses the harder case
of a long-running lower-priority job that's already running when a higher-priority need arrives.
The same starvation guardrail used for priority-preemption in gpu-fleet-autoscaler.md applies here:
preemption frequency per victim job is bounded, so a persistently high-priority demand stream can
reclaim capacity from a lower-priority job without being able to prevent that job from ever making
meaningful forward progress at all — priority isolation protects the high-priority job's needs, and
the bounded-frequency guardrail protects the low-priority job's right to eventually finish.

## Bottlenecks & Scaling

Scheduling decision latency has to stay well under training step time at cluster scale, which
directly bounds how sophisticated the placement solver can be per individual decision — this is
the same incremental-placement-plus-periodic-defragmentation tradeoff described in
gpu-fleet-autoscaler.md, applied here against a gang-scheduling all-or-nothing constraint rather
than a general fractional-GPU packing one. Fragmentation risk scales with job-size heterogeneity —
a cluster running only uniformly-sized jobs rarely fragments meaningfully, while one running both
8-GPU and 2,048-GPU jobs concurrently fragments readily — which makes periodic defragmentation
(draining and repacking to consolidate scattered free capacity) a recurring operational necessity
on a real-world cluster rather than a rare event. Straggler-detection sensitivity trades off
against false-positive rate in essentially the same way alert-threshold tuning does in
ml-monitoring-and-drift.md — set too sensitive, and ordinary step-time jitter triggers unnecessary
node-replacement churn that itself costs throughput; set too conservative, and a genuine straggler
silently caps a job's throughput for a long time before the detector catches it. Finally, elastic-
job resize frequency is itself a throughput cost, not a free lever — every resize incurs a
re-sharding pause in the affected job — so the orchestrator dampens resize decisions against a
minimum-benefit threshold (a freed or lost node has to be worth enough resize benefit to justify
the pause) rather than reacting to every single individual capacity fluctuation as it happens,
which would otherwise turn elasticity itself into a recurring tax on the jobs meant to benefit from
it.
