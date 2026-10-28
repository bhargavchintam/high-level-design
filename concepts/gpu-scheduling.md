# GPU Scheduling

GPUs are expensive, indivisible-by-default, and slow to provision, which makes cluster-level GPU
scheduling a different problem from CPU scheduling. A CPU scheduler can slice a core into
millisecond timeshares across dozens of unrelated processes with little consequence; a GPU
scheduler is placing jobs that each want a whole accelerator (or several, in lockstep) onto a
fixed, expensive fleet where a bad placement decision sits idle for hours. The core tensions are
bin-packing (how tightly jobs pack onto physical GPUs without wasting capacity), gang scheduling
(distributed jobs need every worker up before any of them can progress), and isolation (how much
one job can be shielded from a noisy neighbor sharing its GPU).

## Bin-packing and fragmentation

A scheduler placing jobs onto an 8-GPU node has to treat each node as a bin and each job's GPU
request as an item to pack. The complication beyond plain bin-packing: many jobs don't just need
N free GPUs anywhere in the cluster, they need N GPUs in the same NVLink/NVSwitch domain, because
tensor-parallel jobs exchange partial results every layer and can't tolerate cross-node latency
(see [distributed-training.md](distributed-training.md)). A node with 3 GPUs free can still be unusable for a pending 4-GPU
tensor-parallel job even though the cluster-wide free count looks fine — that's external
fragmentation, and it compounds as job sizes get more heterogeneous. Bin-packing heuristics
(best-fit: place a job on the node with the smallest sufficient gap, to keep large contiguous
gaps free for large jobs; worst-fit: spread jobs to keep gaps balanced) trade off differently
depending on the job-size distribution, but neither eliminates fragmentation — clusters running a
mix of 1-GPU inference jobs and 64-GPU training jobs commonly lose 20-30% of nominal capacity to
fragmentation alone, which is why schedulers (Slurm, Kubernetes with a GPU-aware scheduler
plugin, Ray) bias toward packing small jobs onto a subset of nodes and keeping other nodes
maximally free for large ones, rather than spreading jobs evenly.

## Gang scheduling

A distributed training job using data or tensor parallelism across 64 GPUs (see
[distributed-training.md](distributed-training.md)) needs all 64 workers scheduled and running before the first all-reduce
or cross-rank communication can happen at all — a partial allocation doesn't do partial useful
work, it just holds resources while waiting. A scheduler that allocates GPUs one at a time as
they free up, the way a normal job queue would, can deadlock: job A holds 32 GPUs waiting for 32
more, job B holds a different 32 waiting for the same, and neither ever completes. Gang
scheduling fixes this by making the allocation atomic — either all of a job's requested workers
are scheduled together, or none are, and the job waits in the queue as a single unit rather than
dribbling out partial allocations. The cost is lower average utilization while a large job's full
request is pending, which is the reason schedulers pair gang scheduling with backfill (running
smaller jobs in the gap while a large job's full allocation is assembled) to keep the cluster busy
without breaking the atomicity guarantee the large job needs.

## MIG vs. MPS: hardware vs. software sharing

Not every workload needs a whole GPU — a small inference model might use 10% of an H100's compute
and a fraction of its memory, and dedicating a full accelerator wastes the rest. Two different
mechanisms share one physical GPU across multiple workloads:

- MIG (Multi-Instance GPU, NVIDIA A100/H100): hardware-level partitioning. The GPU is split into
  up to 7 isolated instances, each with its own dedicated slice of SMs, memory, and cache — a
  workload on one instance cannot see or interfere with another, the same isolation guarantee as
  separate physical GPUs. An 80GB A100 can be split into profiles like seven `1g.10gb` instances
  (minimum granularity) or a mix such as one `3g.40gb` plus two `2g.20gb`. The tradeoff is that
  partitioning is static — reconfiguring MIG profiles requires draining and resetting the GPU, so
  it suits stable, predictable-size workloads (a fleet of small inference replicas) rather than
  bursty ones.
- MPS (Multi-Process Service): software-level sharing. Multiple processes submit CUDA kernels to
  the same physical GPU concurrently through a shared context, avoiding the overhead of
  time-sliced context switching and improving utilization when workloads don't individually
  saturate the GPU. Unlike MIG, there's no hardware memory or fault isolation — a process that
  crashes or corrupts memory can affect every other process sharing that MPS context, which
  makes it a throughput optimization for trusted, co-located workloads rather than a multi-tenant
  isolation mechanism.

The general pattern — hard-partition when isolation matters, soft-share when only throughput
matters — recurs in the model-serving fleet decisions covered in
[../ml-system-design/model-serving-platform.md](../ml-system-design/model-serving-platform.md), which bin-packs many small models onto shared GPU
capacity at the application level rather than the hardware level.

## Preemption

High-priority or latency-sensitive work (an inference endpoint that just started taking real
traffic) sometimes needs to reclaim GPUs from lower-priority work (an exploratory training run)
already running. Preemption requires the lower-priority job to checkpoint its state and release
the GPU, then resume later — for training this is a natural fit, since training jobs already
checkpoint periodically for fault tolerance (see [../ml-system-design/ml-experiment-tracking-platform.md](../ml-system-design/ml-experiment-tracking-platform.md)),
so preemption cost is bounded by checkpoint interval, not total progress lost. Cloud spot/preemptible
GPU instances are this same mechanism exposed as a pricing tier: 60-90% cheaper than on-demand in
exchange for the provider reclaiming the instance on short notice, which only pencils out for
workloads that checkpoint cheaply and tolerate interruption — a stateless inference replica
usually isn't a good preemption candidate, since killing it mid-request degrades user-facing
latency in a way a checkpointed batch job doesn't.
