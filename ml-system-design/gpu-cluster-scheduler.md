# GPU Cluster Scheduler

## Requirements

### Functional
- Allocate GPU resources — whole GPUs or MIG-partitioned fractional GPUs — to heterogeneous
  workloads (multi-node training jobs, inference deployments, interactive dev sessions) submitted
  by many teams against one shared physical cluster. This is the allocation substrate that
  [llm-training-platform.md](llm-training-platform.md) and [multi-tenant-gpu-serving.md](multi-tenant-gpu-serving.md) are both built on top of, not a
  replacement for either.
- Gang-schedule multi-GPU/multi-node jobs atomically: a job requesting N GPUs must receive all N
  simultaneously or none at all — a partial allocation for a synchronous distributed job is worse
  than no allocation, since idle ranks holding resources while waiting for the rest of the job to
  be placed is pure waste and can deadlock against other pending gang requests.
- Support priority classes with preemption: a higher-priority job can reclaim resources from a
  lower-priority job, which must be signaled to checkpoint and yield gracefully rather than killed
  outright.
- Minimize fragmentation, so that aggregate free capacity across the cluster actually translates
  into schedulable capacity for real job shapes, rather than being stranded as unusable slivers
  scattered across many partially-occupied nodes.
- Support fractional GPU allocation via MIG (hardware partitioning) for workloads that don't need
  a full GPU — small inference models, dev/debug sessions — improving utilization for the long
  tail of sub-GPU-sized demand.

### Non-functional
- Scheduling decision latency: sub-second for typical single-node/single-GPU jobs, single-digit
  seconds for large gang-scheduled jobs, even with thousands of nodes and hundreds of pending jobs
  in the queue — this is the number that determines whether the scheduler is a background system
  or a visible bottleneck in every team's iteration loop.
- Cluster-wide GPU utilization (GPUs actively allocated and computing, over total GPUs) is the
  headline efficiency metric — fragmentation and poor bin-packing decisions show up directly as a
  gap between "free capacity exists" and "free capacity is usable."
- Preemption latency: from decision-to-preempt to the victim job's resources being reclaimed
  should be bounded (a few minutes), tied to how long a graceful checkpoint-and-yield takes, not
  to an indefinite grace period that defeats the purpose of preemption.
- Topology awareness: placement must respect network locality (NVLink within a node, faster
  intra-rack fabric, slower cross-rack fabric) because job performance — specifically
  [distributed-training-system.md](distributed-training-system.md)'s gradient all-reduce cost — depends heavily on how physically
  close the allocated GPUs are to each other, not just on GPU count.

## Capacity Estimation

Cluster: 16,384 GPUs (2,048 nodes × 8 GPUs), a realistic large multi-pod cluster.

**Submission load.** ~2,000 job submissions/day → 2,000 / 86,400 ≈ 0.023/sec, roughly one every 43
seconds on average — a low absolute arrival rate, but each scheduling decision is combinatorially
harder than typical container scheduling because of the gang + topology constraints: the scheduler
isn't just answering "is there capacity," it's answering "is there a topologically coherent subset
of capacity," which is a much smaller and harder-to-verify set of valid placements.

**Fragmentation, worked example.** Suppose the cluster has 15% of its 16,384 GPUs free (2,458
GPUs) but scattered as 1-2 free GPUs per node across many partially-occupied nodes, rather than
concentrated as free whole nodes. A job requesting an 8-GPU single-node allocation (needed for
intra-node tensor parallelism, per [distributed-training-system.md](distributed-training-system.md)) cannot be placed at all despite
2,458 aggregate free GPUs existing — the usable capacity for that job shape is effectively zero.
This is the concrete cost fragmentation imposes: aggregate utilization numbers alone can look
healthy while the cluster is unable to admit the job shapes it actually receives.

**MIG density gain, worked example.** A100/H100 MIG partitions one physical GPU into up to 7
instances. A queue of 500 lightweight inference/dev jobs, each needing roughly 1/7 of a GPU's
memory and compute, would require 500 whole GPUs without MIG. With MIG, the same 500 jobs fit on
⌈500/7⌉ ≈ 72 physical GPUs — a **~7x density gain** for that specific workload class, which is the
core economic argument for MIG over dedicating whole GPUs to small workloads (cross-ref
[multi-tenant-gpu-serving.md](multi-tenant-gpu-serving.md)'s isolation deep dive for the tradeoff MIG makes to get this density).

**Preemption overhead.** If a preemption costs ~2 minutes of checkpoint-and-yield time on average
and the cluster sees ~50 preemption events/day, that's 50 × 2 = 100 GPU-job-minutes/day of pure
overhead — small relative to 16,384 GPUs × 1,440 minutes/day ≈ 23.6M GPU-minutes/day of total
capacity (≈0.0004%), but it's a real, quantifiable "preemption tax" that grows nonlinearly if
priority mix skews toward frequent small high-priority jobs constantly evicting large low-priority
ones (see bottlenecks).

## API Design

```
POST /v1/jobs
     { resource_request: { gpu_count, gpu_type, topology_constraint: same_node | same_rack | any,
       mig_profile? }, priority_class, gang: bool, preemptible: bool }
     -> 202 { job_id, status: "pending" }

GET  /v1/jobs/{job_id}/placement
     -> { status: pending | scheduled | running | preempted,
          assigned_nodes: [...], queue_position }

POST /v1/jobs/{job_id}/preempt
     -> 202   (internal/admin-triggered; signals the job to checkpoint and yield within
               its grace period before hard reclamation)

GET  /v1/cluster/capacity
     -> { total_gpus, allocated_gpus, free_gpus_by_topology_bucket, fragmentation_index }
```

`free_gpus_by_topology_bucket` (rather than a single free-GPU count) is deliberate — the
fragmentation example above is exactly why a single aggregate number is close to meaningless for
predicting whether a given job shape can actually be placed.

## Data Model

```
node_inventory (physical layout)
  node_id, rack_id, pod_id, gpu_count, gpu_type, mig_enabled,
  gpus: [{gpu_id, status: free | allocated | mig_partitioned, allocated_to_job_id}]

job_queue (pending admission)
  job_id, priority_class, resource_request, gang_size, preemptible,
  submitted_at, status

allocation (committed placements)
  job_id -> [gpu_ids], topology_score, allocated_at

preemption_log (audit trail for the fairness/preemption policy)
  job_id, preempted_at, preempted_by_job_id, checkpoint_completed_at
```

## High-Level Architecture

```
 submission path:
 client ──POST /jobs──► Admission control (quota/priority validation)
                                    │
                                    ▼
                    Priority queue (priority_class, then FIFO within class,
                    with aging to prevent starvation)
                                    │
                                    ▼
                    Placement engine: topology-aware bin-packing search
                    over node_inventory's free-capacity buckets; for gang
                    jobs, reserve-then-commit across all required nodes
                    atomically or roll back and re-queue
                                    │
                        found valid placement? ──no──► stays queued,
                                    │yes                re-evaluated on
                                    ▼                    next capacity change
                    Allocation committed to `allocation`; node agents
                    enforce via cgroups/MIG slicing

 execution / reclamation path:
 running job ──heartbeat──► Health monitor
                                    │
                    higher-priority job arrives, no free capacity? ──►
                    Preemption controller selects a victim (lowest priority,
                    least fair-share deficit) ──► POST /jobs/{id}/preempt
                    ──► victim checkpoints (bounded grace period) ──►
                    resources reclaimed ──► reallocated to waiting job
```

## Deep Dives

**Bin-packing and topology-aware placement.** Optimal multi-dimensional bin-packing is NP-hard, so
production schedulers use heuristics over exact search: best-fit or first-fit-decreasing applied
not to raw GPU count but to topology-bucketed free capacity (free GPUs grouped by which node/rack
they belong to). The placement decision is "which specific GPUs," not just "how many" — an 8-GPU
job split across two nodes without an NVLink path between them will tank tensor-parallel
throughput even though the GPU count requested was satisfied, which is why `topology_constraint`
is a first-class field in the resource request rather than an optimization hint the scheduler may
or may not honor.

**Gang scheduling and atomicity.** All-or-nothing allocation is implemented as a two-phase
reserve-then-commit: the placement engine tentatively reserves a topologically valid set of GPUs
across every node the gang job needs, and only commits the allocation once every reservation
succeeds. If any part of the reservation fails (a node became unavailable mid-search), the whole
tentative reservation is rolled back rather than left partially held — holding partial resources
while waiting for the rest is exactly the deadlock/resource-starvation pattern gang scheduling
exists to prevent, since two large jobs each holding half of what they need and waiting on the
other half can otherwise wait on each other indefinitely.

**Fragmentation.** Internal fragmentation (a MIG partition or memory allocation larger than what a
workload actually uses) and external fragmentation (free capacity scattered too thinly across
nodes to satisfy a contiguous request, per the worked example above) both reduce usable capacity
below aggregate free capacity. Mitigations are mostly about *placement policy* rather than
after-the-fact cleanup: packing small/flexible jobs preferentially into partially-occupied nodes
while reserving whole free nodes for large gang jobs (rather than greedily filling whichever node
has any free slot first) keeps large contiguous blocks available longer. Where fragmentation still
accumulates, periodic defragmentation — checkpointing and migrating a subset of running jobs to
consolidate free capacity — is a standing operational job, not a one-time fix, the same way
compaction is a standing job for the KV cache paging in [llm-inference-serving.md](llm-inference-serving.md) or the vector
index in [vector-search-system.md](vector-search-system.md).

**Priority, preemption, and fairness.** Pure priority ordering can starve low-priority work
indefinitely if higher-priority submissions never stop arriving, so the scheduler layers
fair-share accounting (each team/queue has a target GPU-time share; jobs from teams below their
share get an effective priority boost) and aging (a job's effective priority rises the longer it
waits) on top of raw priority classes. When preemption is triggered, the victim is selected to
minimize both priority-class violation and fair-share deficit simultaneously, and is given a
bounded grace period to checkpoint before hard reclamation — an ungraceful kill of a training job
loses unsaved progress, which is a materially worse outcome than the few minutes of delayed
reclamation a graceful preemption costs.

**MIG and fractional GPU allocation.** MIG gives hardware-level isolation — a fixed memory and
compute partition with a guaranteed SLA — but the partition granularity (how a physical GPU is
sliced) is set when the GPU is *configured*, not per scheduling decision, and reconfiguring a
GPU's MIG profile requires draining it of any current workload first, which is disruptive. This
means the scheduler has to track MIG-partitioned capacity as a structurally different resource
type from whole-GPU capacity — a GPU already sliced into seven 1g.10gb MIG instances can't host a
job that needs a full GPU without first draining and repartitioning it, an operation the scheduler
should treat as expensive and rare, not something it does opportunistically mid-schedule.
Software time-slicing is the more flexible alternative (any granularity, no hardware
reconfiguration) but gives weaker isolation — cross-ref [multi-tenant-gpu-serving.md](multi-tenant-gpu-serving.md)'s isolation
deep dive for how that tradeoff plays out at the serving layer specifically.

## Bottlenecks & Scaling

Scheduling decision latency is the first thing to degrade as cluster size and pending-queue depth
grow, since the placement search space grows with node count — the standard mitigation is
partitioning the scheduling domain by pod/rack (each partition scheduled somewhat independently,
with a thinner cross-partition balancer for jobs too large to fit in one partition) rather than
running one global search over the entire cluster on every decision. Fragmentation gets worse as
job-size heterogeneity increases (a cluster running only uniform 8-GPU jobs fragments far less
than one mixing 1-GPU dev sessions with 2,048-GPU training runs), which is why placement policy
has to actively bias against fragmentation rather than assume periodic defragmentation alone will
keep pace. Preemption overhead compounds specifically when the priority mix skews toward many
small high-priority jobs repeatedly evicting large low-priority ones — a form of thrashing where
the cumulative checkpoint-and-yield cost across many small preemptions starts to rival the cost of
just waiting — mitigated with a minimum-preemption-interval or a floor on how much progress a
preemptible job is guaranteed to make between evictions. Finally, MIG reconfiguration's
disruptiveness means mixing MIG and non-MIG demand on the same physical fleet works best with
capacity pre-partitioned into MIG-enabled and whole-GPU pools ahead of time, rather than
dynamically repartitioning GPUs in response to instantaneous queue composition.
