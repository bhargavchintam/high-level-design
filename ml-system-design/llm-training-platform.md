# LLM Training Platform

## Requirements

### Functional
- Let any team submit a training job (full pretraining, continued pretraining, supervised
  fine-tuning, or preference tuning like DPO) against a shared GPU cluster via a self-service
  API, without operating its own cluster orchestration — see distributed-training-system.md for
  the mechanics of what happens inside one such job once it's running.
- Automatically derive a parallelism plan (data/tensor/pipeline degree) from the requested model
  size and GPU count, or accept an explicit override from teams that want to hand-tune it.
- Manage checkpoints as a first-class, cross-job platform resource: automatic periodic
  checkpointing, retention policy, and — distinct from a single job's own checkpoint/resume
  cycle — the ability for a new job to *fork* from an existing checkpoint (e.g. a fine-tune
  branching off a shared pretrained base) without re-deriving or re-uploading the base weights.
- Provide fault tolerance across many concurrently running jobs sharing one physical cluster: a
  node failure inside Job A's allocation must be contained to Job A and must not degrade,
  preempt, or delay Job B running on different nodes of the same cluster.
- Queue and schedule jobs against finite GPU capacity by team quota and priority, with elastic
  resize of a running job's world size when capacity opens up or has to shrink (the scheduling
  primitives themselves are covered in gpu-cluster-scheduler.md; this platform is a consumer of
  that scheduler, not a reimplementation of it).

### Non-functional
- Two distinct utilization metrics matter and must both be tracked: per-job MFU (model FLOPs
  utilization, how efficiently a running job uses the GPUs it holds — see
  distributed-training-system.md) and cluster allocation utilization (what fraction of the
  cluster's GPUs are allocated to *some* job at all, vs. sitting idle in the queue's shadow).
  A platform can have every running job at 45% MFU and still waste money if half the cluster is
  unallocated because of poor scheduling.
- Time from job submission to first training step running should be seconds when capacity is
  free, low minutes when the scheduler has to wait for a gang allocation to become available —
  not the hours a team would spend hand-provisioning a bespoke cluster.
- Automated fault recovery: a job must resume from its last checkpoint without a human paging in,
  within a bounded window (minutes), even when failures are a near-daily occurrence at cluster
  scale (see capacity estimation).
- Fairness across teams: no single team's job, however high-priority, can indefinitely starve
  every other team's quota — enforced by the underlying scheduler's fair-share policy, not by
  this platform re-deriving fairness logic of its own.

## Capacity Estimation

Cluster: 8,192 H100 GPUs (1,024 nodes × 8 GPUs), shared org-wide.

**Job mix and allocation.** At any moment, assume ~40 concurrent jobs: a long tail of ~30 small
fine-tune/SFT jobs at ~64 GPUs each (30 × 64 = 1,920 GPUs), ~8 mid-size continued-pretraining or
large-fine-tune jobs at ~256 GPUs each (8 × 256 = 2,048 GPUs), and 2 large pretraining runs at
2,048 GPUs each (2 × 2,048 = 4,096 GPUs). Total: 1,920 + 2,048 + 4,096 = **8,064 GPUs**, ≈ 98.4% of
the 8,192-GPU cluster allocated — the platform is designed to run hot, with the queue (not idle
capacity) absorbing demand variance.

**Failure rate the platform must absorb automatically.** distributed-training-system.md notes
that a 2,048-GPU cluster running for days-to-weeks sees a failure "every few hours." At 8,192 GPUs
(4x the hardware), the same failure density implies roughly one failure somewhere in the fleet
every 30-60 minutes → on the order of **25-45 failure events/day** platform-wide. At that rate,
manual failure handling is not viable at all; every one of those events has to be detected,
contained to its job, and recovered without a human in the loop, which is the central design
constraint of this platform more than any single number in the capacity math.

**Queueing.** Model job arrivals/departures with Little's law as a sanity check, not a precise
queueing model: if jobs arrive at ~15/day (0.625/hour) and the average job holds its allocation
for ~48 hours, average concurrent jobs in service ≈ arrival rate × service time = 0.625 × 48 ≈ 30
— consistent with the ~40 assumed above once queueing/startup overlap is included. The takeaway:
at this scale, queue depth is driven far more by a handful of very large (2,048-GPU) jobs
occupying a large fraction of the cluster for days at a time than by the number of jobs itself —
one large pretraining run can visibly extend queue wait times for every smaller job behind it.

**Checkpoint storage bandwidth at platform scale.** Each large job checkpoints roughly 1.1 TB
every 20-30 minutes on its own (per distributed-training-system.md). With 2 large jobs
checkpointing independently but not perfectly staggered, worst-case simultaneous writes are ~2.2
TB in a write window of a few minutes → **several GB/sec of aggregate checkpoint write bandwidth**
the shared storage tier must sustain at peak, on top of the mid-size and small jobs' much smaller
contributions — this is why checkpoint storage is provisioned and monitored as shared platform
infrastructure, not sized per job.

**Checkpoint reuse economics.** If 30 fine-tune jobs all fork from one of 3 shared base-model
checkpoints (rather than each uploading its own copy of ~140GB of base weights), checkpoint
storage for those forks is dominated by content-addressed dedup of the shared base (cross-ref
ml-experiment-tracking-platform.md's artifact dedup) — the alternative, 30 independent full
copies, would cost ~30 × 140GB ≈ 4.2TB of redundant storage for weights that are byte-identical
across forks until each job's fine-tuning diverges them.

## API Design

```
POST /v1/training-jobs
     { job_type: pretrain | continued_pretrain | sft | dpo,
       model_config OR fork_from_checkpoint_id,
       dataset_uri, requested_gpu_count, parallelism_override?,
       quota_pool, priority_class, checkpoint_interval_min }
     -> 202 { job_id, status: "queued", queue_position }

GET  /v1/training-jobs/{job_id}/status
     -> { status: queued | provisioning | running | checkpointing | recovering |
                   completed | failed,
          parallelism_plan: {dp, tp, pp}, nodes_healthy/nodes_total,
          current_step, loss, mfu_pct }

POST /v1/checkpoints/{checkpoint_id}/fork
     { new_job_spec }
     -> 202 { job_id }   (launches a new job whose initial weights are this checkpoint,
                          without re-uploading or re-deriving them)

GET  /v1/quota/{team}
     -> { gpu_hours_allocated, gpu_hours_used_mtd, active_jobs }
```

`fork` is the operation that doesn't exist in a single-job training system
(distributed-training-system.md's `resume` only continues the *same* job) — it's the platform-level
primitive that turns a checkpoint from one job's private recovery artifact into an org-wide shared
starting point for new jobs.

## Data Model

```
training_job (control-plane record)
  job_id, team, quota_pool, priority_class, job_type,
  requested_gpu_count, parallelism_plan, status,
  queued_at, started_at, fork_parent_checkpoint_id?

quota_pool (per team, enforced at admission)
  team, gpu_hours_allocated_per_period, gpu_hours_used, priority_tier

checkpoint_lineage (distinct from ml-experiment-tracking-platform.md's dataset->run->model
  lineage — this tracks checkpoint-to-checkpoint forking across jobs)
  checkpoint_id, produced_by_job_id, forked_from_checkpoint_id?, step, storage_uri

cluster_capacity (a view the scheduler exposes; this platform reads, not owns)
  node_id, gpu_count, allocated_to_job_id | free, health_status

fault_event (per detected failure, feeds the automated-recovery SLA)
  event_id, job_id, node_id, detected_at, contained: bool,
  recovery_action: resume_from_checkpoint | elastic_shrink | failover_spare,
  recovered_at
```

## High-Level Architecture

```
 submission path:
 team ──POST /training-jobs──► Admission control (quota check against
                                 quota_pool; reject/queue if over budget)
                                          │
                                          ▼
                        Parallelism planner: given model size + requested
                        GPU count + cluster topology, derive (dp, tp, pp)
                        respecting per-node NVLink limits (see
                        distributed-training-system.md's 3D parallelism
                        deep dive) — or accept explicit override
                                          │
                                          ▼
                        Job queue (priority + quota ordered) ──►
                        gpu-cluster-scheduler.md: gang-allocates the full
                        requested GPU set atomically, or leaves job queued

 execution path (once scheduled):
 Launched job runs the standard 3D-parallel training loop described in
 distributed-training-system.md (forward/backward, gradient all-reduce,
 ZeRO-sharded optimizer step) on its allocated nodes
                                          │
                                          ▼
                        Async checkpoint writer ──► shared checkpoint
                        object store (content-addressed; forkable by
                        other jobs via checkpoint_lineage)
                                          │
                                          ▼
                        Platform-level watchdog: heartbeats every job's
                        nodes; on failure, contains blast radius to the
                        owning job, triggers resume-from-checkpoint or
                        elastic world-size shrink — never touches other
                        jobs' allocations on unaffected nodes
```

## Deep Dives

**Multi-job scheduling and elastic world size.** A training job needs gang scheduling — all
requested GPUs simultaneously, or none, since synchronous data/tensor/pipeline parallel training
cannot start with a partial allocation (a stalled rank blocks every other rank's collective ops).
This platform delegates the actual bin-packing and gang-allocation mechanics to
gpu-cluster-scheduler.md and focuses on what's specific to training workloads: when a node inside
a running job's allocation fails or is reclaimed, an elastic-capable training framework can shrink
world size and continue (adjusting data-parallel degree, re-deriving the affected pipeline stage's
assignment) rather than requiring a full job restart. This matters directly at the failure rate
computed above — the difference between "shrink and continue" and "kill and restart from
checkpoint" compounds into a meaningfully different fraction of wall-clock time lost across
25-45 failure events/day platform-wide.

**Automatic parallelism planning.** distributed-training-system.md's deep dive describes *why*
tensor parallelism is confined within a node and pipeline parallelism spans node groups; this
platform automates the *decision*. Given a requested model size (parameter count, layer count,
hidden dim) and GPU count, the planner picks a tensor-parallel degree bounded by intra-node GPU
count (so TP never crosses the NVLink boundary), a pipeline-parallel degree that divides the
remaining topology into node groups with even layer counts, and a data-parallel degree from
whatever's left — then validates the resulting per-GPU memory footprint against the ZeRO-sharded
estimate from distributed-training-system.md before admitting the job, rejecting configurations
that would OOM before they ever reach the scheduler. Advanced teams can override this with an
explicit `parallelism_override`, but the default path is what makes the platform usable by teams
who don't want to reason about 3D parallelism topology themselves.

**Checkpoints as a shared, forkable platform resource.** A single job's checkpoint (per
distributed-training-system.md) exists to resume *that* job after a failure. At the platform
level, the same artifact is also the starting point for every fine-tune job that forks from it —
`checkpoint_lineage` tracks this explicitly rather than treating each fork as an independent
upload. This has real storage economics (the dedup math above) and a real operational
implication: a base checkpoint that many active fine-tunes have forked from cannot be
garbage-collected on the same retention schedule as an ordinary intermediate checkpoint, so
retention policy has to check for live fork dependents before reclaiming storage, not just
checkpoint age.

**Fault isolation across co-located jobs.** At 8,064 of 8,192 GPUs allocated across ~40
concurrent jobs, jobs are physically co-located on a shared network fabric even though they're
logically independent. Two failure-isolation concerns follow: first, a node failure must be
contained to its owning job's watchdog and recovery path — the platform's job-scoped fault_event
tracking exists specifically so one job's failure never triggers action on another job's
allocation. Second, and less obvious, two large jobs running simultaneous all-reduce collectives
can contend for cross-rack network bandwidth even without any node failure at all, silently
degrading both jobs' MFU — which is why the scheduler's topology-aware placement
(gpu-cluster-scheduler.md) tries to keep each large job's allocation network-local where possible,
not just GPU-count-sufficient.

**Priority, preemption, and fairness across teams.** Quota pools cap each team's GPU-hour budget,
but a rigid priority order alone can starve low-priority jobs indefinitely if high-priority
submissions never stop arriving. The platform's admission and scheduling policy combines priority
classes (a large pretraining run can preempt a low-priority exploratory fine-tune) with fair-share
enforcement and aging (a long-queued low-priority job's effective priority rises over time) —
the same tension gpu-cluster-scheduler.md resolves generically, applied here to the specific
consequence that a preempted training job must checkpoint before yielding its allocation, since an
ungraceful kill loses unsaved progress that a graceful preemption (checkpoint-then-yield) would
not.

## Bottlenecks & Scaling

Queue wait time is dominated by the largest jobs in the mix, not job count — a single 2,048-GPU
pretraining run occupying a quarter of the cluster for days visibly delays every smaller job
queued behind it, which argues for reserving a bounded capacity slice for small/interactive jobs
rather than letting pure priority-and-quota ordering starve them during a large run. Shared
checkpoint storage bandwidth becomes the platform's own bottleneck exactly when it's needed most —
multiple large jobs' checkpoint intervals correlating (e.g. many jobs configured with the same
30-minute cadence starting near the same wall-clock time) produces the multi-GB/sec write bursts
estimated above, which is why checkpoint writes are staggered/jittered across jobs rather than
left to align by coincidence. Cross-job network contention on shared fabric is the least visible
scaling limit: it doesn't show up as a scheduling failure or a fault event, only as unexplained MFU
degradation on jobs that were otherwise correctly provisioned, which is why topology-aware
placement is treated as a scheduling correctness property, not a performance nicety, once the
cluster runs multiple large jobs concurrently. Finally, the scheduler's own decision latency
(gpu-cluster-scheduler.md) becomes the platform's ceiling on job-submission responsiveness as
cluster size and job-count both grow — this platform's own logic (admission control, parallelism
planning, checkpoint lineage) stays cheap and fast even at large scale; it's the underlying
gang-allocation search that has to scale separately.
