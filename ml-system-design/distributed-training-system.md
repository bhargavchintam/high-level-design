# Distributed Training System

## Requirements

### Functional
- Train a large model (assume a 70B-parameter decoder-only transformer, consistent with the
  scale in llm-inference-serving.md) on a multi-trillion-token dataset across hundreds to
  thousands of GPUs.
- Checkpoint training state periodically such that a failure loses at most a bounded amount of
  progress (not the entire run) and training can resume from the last checkpoint.
- Tolerate individual node/GPU failures during a run that spans days to weeks — at this scale
  and duration, hardware failure during the run is a certainty, not an edge case.
- Support mixed on-demand and spot/preemptible instances to reduce training cost, given that
  compute is the dominant cost of the entire project.

### Non-functional
- GPU utilization (measured as achieved FLOPs / theoretical peak FLOPs, "MFU" — model FLOPs
  utilization) is the primary efficiency metric — most of the engineering effort in a system
  like this goes into keeping GPUs from stalling on data loading, communication, or
  synchronization, since idle GPU-hours at this cluster size are extremely expensive.
  A well-tuned large-model training run typically achieves 35-50% MFU; below ~30% usually
  indicates a communication or data-loading bottleneck worth investigating.
- Fault recovery time (detect failure, evict/replace bad node, resume from last checkpoint)
  should be minutes, not hours — at thousands of GPUs running for weeks, slow recovery
  compounds into a large fraction of total wall-clock time lost to failure handling rather than
  training.
- Gradient synchronization correctness is non-negotiable — a silent numerical bug in
  distributed gradient sync (e.g. from a race condition or a misconfigured collective) can
  produce a model that trains "successfully" (loss goes down) but converges to a worse optimum
  than a correct run, and this class of bug is notoriously hard to detect after the fact.
- Reproducibility: given the same seed, data order, and configuration, a re-run should be able
  to reproduce the same loss curve closely enough to debug regressions — this constrains how
  nondeterminism (async data loading order, non-deterministic GPU kernels) is handled.

## Capacity Estimation

Model: 70B parameters. Training tokens: 2 trillion (roughly in line with published
compute-optimal training recipes for a model this size).

**Compute estimate (the 6ND rule).** Training FLOPs ≈ 6 × N (parameters) × D (tokens) = 6 ×
70×10⁹ × 2×10¹² ≈ **8.4 × 10²³ FLOPs** total for the run.

**Cluster size and wall-clock time.** An H100 delivers roughly 990 TFLOPs of dense fp16/bf16
compute at peak (per published spec); at a realistic ~40% achieved MFU for a well-tuned large
run, that's ~396 TFLOPs/sec/GPU sustained. A 2,048-GPU cluster sustains ≈ 2,048 × 396 TFLOPs ≈
8.1 × 10¹⁷ FLOPs/sec. Wall-clock time ≈ 8.4×10²³ / 8.1×10¹⁷ ≈ **~1.04 × 10⁶ seconds ≈ ~12 days**
of continuous training — this is the kind of order-of-magnitude estimate that determines
whether a training run is a days-long or months-long commitment, and it moves linearly with
both cluster size and achieved MFU, which is exactly why MFU is treated as the headline
efficiency metric above.

**Memory footprint per GPU (why sharding is mandatory, not optional).** Naive data parallelism
requires every GPU to hold: fp16 model weights (70B × 2 bytes = 140GB), fp16 gradients (another
140GB), and fp32 optimizer state for Adam (first + second moment, each fp32 = 4 bytes/param ×
2 = 560GB, plus commonly an fp32 master weight copy, another 280GB) — combined, well over 1TB
per GPU just for model/optimizer state, against 80GB of HBM on an H100. This is the concrete
reason ZeRO-style sharding (see deep dive) exists: it's not an optimization, it's the only way
this model fits on this hardware at all.

**Checkpoint size and frequency.** A full checkpoint (fp16 weights + fp32 optimizer state) is on
the order of the ~1TB/GPU-equivalent figure above, aggregated ≈ 70B params × (2 + 2 + 4 + 4 + 4)
bytes ≈ 70B × 16 bytes ≈ **1.12 TB** total checkpoint size (weights + gradients + optimizer
moments + master copy, before sharding-aware dedup). At a checkpoint cadence of every 30 minutes
(a reasonable balance between recovery granularity and I/O overhead), and a cluster-aggregate
checkpoint write bandwidth of a few GB/sec to durable storage, a full checkpoint write takes on
the order of minutes — which has to be pipelined with continued training (async checkpointing)
rather than pausing the whole cluster for the duration of the write, or checkpointing overhead
alone would eat a meaningful fraction of total training time.

**Spot instance economics.** If 30-40% of the cluster runs on spot/preemptible capacity at
roughly 60-70% cost discount versus on-demand, and preemptions occur frequently enough to trigger
a handful of checkpoint-and-resume cycles per day across that portion of the fleet, the net cost
savings still typically outweigh the lost-progress and recovery overhead — provided checkpoint
frequency is tuned tightly enough that a single preemption never loses more than a few minutes of
work per affected node group.

## API Design

There's no end-user-facing API here in the request/response sense — the "interface" is a
training job specification and a set of operational control-plane endpoints:

```
POST /v1/training-jobs
     { model_config, dataset_uri, cluster_size, parallelism_strategy,
       checkpoint_interval_min, spot_fraction }
     -> 202 { job_id, status: "provisioning" }

GET  /v1/training-jobs/{job_id}/status
     -> { status: running | checkpointing | recovering | completed | failed,
          current_step, loss, mfu_pct, nodes_healthy/nodes_total }

POST /v1/training-jobs/{job_id}/checkpoint
     -> 202   (force an out-of-band checkpoint, e.g. before a planned maintenance window)

POST /v1/training-jobs/{job_id}/resume
     { checkpoint_id? }   (defaults to latest)
     -> 202
```

## Data Model / Feature Schema

```
training_job (control-plane record, one per run)
  job_id, model_config, dataset_uri, parallelism_strategy
  (data/tensor/pipeline degree), cluster_size, status

checkpoint_manifest (one per checkpoint event)
  checkpoint_id, job_id, step, wall_clock_ts, shard_locations[]
  (a checkpoint is itself sharded across many files/objects, one
   or more per rank, not a single monolithic blob — see deep dive)

node_health (per node, updated continuously by the cluster manager)
  node_id, job_id, gpu_health, last_heartbeat_ts,
  status: healthy | degraded | evicted

training_metrics (time series, one point per logging interval)
  job_id, step, loss, grad_norm, learning_rate, mfu_pct,
  tokens_per_sec, ts
```

`checkpoint_manifest.shard_locations` reflects that at this scale a checkpoint isn't one file —
it's potentially thousands of shards (one per data-parallel/tensor-parallel/pipeline-parallel
rank combination under fully-sharded training), and resuming requires every rank to load its own
matching shard, which is itself a distributed operation with its own failure modes.

## High-Level Architecture

```
 training path:
 Dataset (sharded, e.g. tokenized + pre-chunked in object storage)
                    │
                    ▼
        Data loader workers (per-node, prefetch + shuffle,
        must keep pace with GPU consumption or GPUs stall — see
        data loading deep dive)
                    │
                    ▼
    ┌───────────────────────────────────────────────────────────┐
    │  GPU cluster (e.g. 256 nodes x 8 GPUs = 2,048 GPUs)         │
    │                                                              │
    │  Parallelism layout (composed, not either/or):               │
    │   - Tensor parallel (e.g. degree 8, within a node,           │
    │     NVLink-connected — splits each layer's matmuls)          │
    │   - Pipeline parallel (e.g. degree 8, across node groups —   │
    │     splits layers into stages)                               │
    │   - Data parallel (remaining degree, across the rest of      │
    │     the cluster — each replica group processes a different   │
    │     batch shard)                                              │
    │   - ZeRO-style sharding of optimizer state/gradients/         │
    │     weights across the data-parallel dimension                │
    │                                                              │
    │  forward/backward pass ──► gradient all-reduce (within DP    │
    │  group) ──► optimizer step (sharded, ZeRO stage 2/3)          │
    └───────────────────────────────────────────────────────────┘
                    │
                    ▼
        Async checkpoint writer (streams sharded state to durable
        object storage without blocking the next training step)
                    │
                    ▼
        Cluster manager: heartbeats every node, detects failure,
        evicts + replaces bad nodes, triggers resume-from-checkpoint
        for the affected parallel group
```

## Deep Dives

**Data, tensor, and pipeline parallelism — why all three, composed.** These solve different
problems and real large-model training uses them together (so-called 3D parallelism), not as
alternatives. Data parallelism (each replica holds a full model copy, processes a different
data shard, gradients averaged across replicas) scales training throughput with data volume but
does nothing to fit a model too large for one GPU's memory — that's the problem tensor and
pipeline parallelism solve. Tensor parallelism splits individual layers' computation across
GPUs (as in the serving case in llm-inference-serving.md), requiring high-bandwidth
interconnect since every layer needs a synchronization step, which is why it's typically
confined to GPUs within a single NVLink-connected node. Pipeline parallelism splits the model's
layers themselves across groups of nodes (stage 1 on nodes A, stage 2 on nodes B, ...), needing
far less inter-node bandwidth than tensor parallelism but introducing pipeline bubbles — later
stages sit idle waiting for the first micro-batch to arrive from earlier stages. Micro-batching
(splitting each training batch into several smaller micro-batches pipelined through the stages)
is the standard mitigation, trading some memory and complexity for keeping pipeline bubbles a
small fraction of total step time. The practical layout at 2,048 GPUs is a 3D grid: tensor
parallel within a node (degree matching intra-node GPU count), pipeline parallel across node
groups, and data parallel across the remaining dimension — sized so each dimension respects its
own bandwidth constraint.

**ZeRO (Zero Redundancy Optimizer).** Standard data parallelism replicates full optimizer
state, gradients, and weights on every GPU — pure redundancy, since every replica in the group
is storing an identical copy for a different purpose (data parallelism only needs the *compute*
replicated, not this storage). ZeRO removes this redundancy in stages: stage 1 shards optimizer
state across the data-parallel group (each GPU owns 1/N of the optimizer state, gathering what
it needs only during the update step), stage 2 additionally shards gradients, stage 3
additionally shards the model weights themselves (each GPU holds only 1/N of the parameters at
rest, gathering the full weight for a layer just before it's needed in the forward/backward pass
and releasing it after). This is precisely what makes the earlier memory-footprint math tractable
— without ZeRO stage 2/3, the >1TB/GPU figure calculated above would make a 70B model
un-trainable on 80GB GPUs at any data-parallel degree; with ZeRO stage 3 sharding across, say,
64-way data parallelism, each GPU's share of that >1TB drops to a very manageable amount, at the
cost of additional communication (gather/scatter of shards) that has to be overlapped with
compute to avoid becoming its own bottleneck.

**Checkpointing at scale.** A checkpoint has to capture enough state (model weights, optimizer
moments, current step, data-loader position) to resume training and continue the loss curve as
if no interruption occurred — and it has to be sharded, since no single node holds the full
model state at rest under ZeRO stage 3. Checkpointing is done asynchronously: training continues
on the next step while the previous step's state streams to durable storage in the background,
because pausing a 2,048-GPU cluster to wait on a >1TB checkpoint write would waste a large,
recurring fraction of total compute time over a run lasting days to weeks. The checkpoint
interval is a direct tradeoff between recovery granularity (shorter interval, less lost progress
per failure) and steady-state overhead (shorter interval, more frequent I/O and potential
compute stalls if writes aren't fully overlapped) — 20-30 minutes is a common real-world
operating point at this scale.

**Fault tolerance and elastic recovery.** At thousands of GPUs running for days to weeks,
individual GPU/node/network failures during the run are expected, not exceptional — the mean
time between some failure somewhere in a cluster this size can be hours, not weeks. The cluster
manager continuously heartbeats every node; on missed heartbeats past a threshold, it evicts the
node, and (depending on the parallelism layout) either fails over to a hot spare already
provisioned in the cluster or shrinks the affected parallel group and resumes from the last
checkpoint with an adjusted world size. Elastic training frameworks that support world-size
changes without a full manual restart of the whole job meaningfully reduce recovery time versus
a full-job restart, which matters directly: at even a modest failure rate, the difference between
minutes-to-recover and hours-to-recover compounds into a significant fraction of total wall-clock
time over a multi-week run.

**Data loading as a hidden bottleneck.** GPUs training at 40% MFU are, by definition, idle a
majority of the time relative to theoretical peak — some of that gap is unavoidable
(communication overhead inherent to the parallelism strategy), but a data loader that can't keep
the GPU fed (I/O-bound on reading/tokenizing/shuffling training data faster than the GPU consumes
it) is a purely wasteful, fixable source of the same symptom, and is one of the most common causes
of a training run underperforming its expected MFU. The standard mitigation is aggressive
prefetching (data loader workers stay several batches ahead of GPU consumption) and pre-tokenizing
the dataset into a format that's cheap to read at training time (versus tokenizing raw text on
the fly), so the data pipeline is never the limiting resource against a well-provisioned GPU
cluster.

## Bottlenecks & Scaling

Cross-node interconnect bandwidth is the hard ceiling on how far tensor parallelism can extend
before pipeline parallelism (which needs far less bandwidth) has to take over — this is a
physical, not just a software, constraint, and is why cluster network topology (which GPUs share
NVLink, which are only connected via slower cross-node fabric) directly shapes the parallelism
layout rather than being an afterthought. Gradient all-reduce cost grows with data-parallel
degree, and at large enough data-parallel width the communication cost of synchronizing
gradients can start to rival compute time per step, which is the practical ceiling on scaling
data parallelism alone rather than combining it with model/pipeline parallelism. Checkpoint I/O
bandwidth to durable storage has to scale with cluster size (a checkpoint from a 2,048-GPU run is
proportionally larger and more shards than one from a 256-GPU run), and under-provisioned
checkpoint storage bandwidth turns "async checkpointing" into a lie in practice — writes fall
behind and eventually do stall training. Finally, the single biggest lever on total project cost
is MFU itself — the gap between theoretical peak cluster FLOPs and achieved FLOPs is where most
of the engineering investment in a system like this actually goes, since every percentage point
of MFU translates directly into either faster training or a smaller cluster for the same
wall-clock budget.
