# Distributed Training

A single accelerator stops being enough for two independent reasons, and the fix depends on
which one you're hitting. Either the dataset is too large to iterate over in reasonable
wall-clock time on one device — training would take weeks instead of days — or the model
itself no longer fits in one device's memory, in which case throwing more time at a single
GPU doesn't help at all, it simply can't hold the parameters. The first problem is solved by
data parallelism. The second needs model parallelism, in one of its flavors.

## Data parallelism

Replicate the full model on every device, split each training batch across devices, and each
device computes gradients on its own shard of the batch using its local copy of the weights.
Before the optimizer step, gradients are synchronized across all devices (an all-reduce, so
every device ends up with the same averaged gradient) and each applies an identical update —
keeping all replicas in sync. This is the default first choice: straightforward to implement,
scales close to linearly with device count as long as communication doesn't become the
bottleneck. The catch is memory, not compute — every device needs enough headroom to hold a
full copy of the model weights plus optimizer state (which for Adam-style optimizers is
2-3x the parameter count), so data parallelism alone stops working the moment the model
itself doesn't fit on one device, regardless of how many devices you add.

## Model parallelism

Split the model itself across devices instead of replicating it. Two common flavors, usually
combined rather than chosen exclusively:

- Pipeline parallelism: split the model's layers into sequential stages, each stage living
  on a different device — device 0 holds the first few layers, device 1 the next few, and so
  on, with activations passed forward between them. Naively run, this leaves most devices
  idle most of the time, since device 1 can't start until device 0 finishes the first batch
  and device 0 sits idle waiting for the next batch to arrive — the "pipeline bubble."
  Micro-batching fixes this: split each batch into smaller micro-batches and feed them
  through the pipeline in sequence, so while device 1 works on micro-batch 1, device 0 is
  already processing micro-batch 2. More micro-batches shrink the bubble at the cost of more
  scheduling overhead.
- Tensor parallelism: split individual large matrix operations — the weight matrices inside
  a single attention or feed-forward layer — across devices, so multiple devices cooperate
  on computing the same layer simultaneously rather than each owning a different layer. This
  needs much higher-bandwidth interconnect than pipeline or data parallelism, because
  devices exchange partial results mid-layer, many times per forward pass, instead of once
  per batch. That communication cost is why tensor-parallel groups are almost always kept
  within a single node or rack (NVLink-class interconnect) — Megatron-LM popularized this
  pattern — while pipeline and data parallelism can tolerate the slower interconnect between
  nodes.

## ZeRO (Zero Redundancy Optimizer)

The observation behind ZeRO: in plain data parallelism, every device redundantly stores a
full copy of optimizer state, gradients, and parameters, even though each device only needs
its own shard at any given instant during the all-reduce. ZeRO partitions these across the
data-parallel devices instead of replicating them, in stages of increasing aggressiveness —
stage 1 partitions optimizer state only, stage 2 adds gradient partitioning, stage 3 adds
parameter partitioning itself, reconstructing full layers on the fly via extra communication
only when a device needs them for compute. Each stage trades more communication for a large
cut in per-device memory footprint. This is what lets data-parallel-style training scale to
much larger models before model parallelism becomes strictly necessary — ZeRO doesn't
replace model parallelism, it pushes out the point at which you need it.

## Mixed precision training

Run the forward and backward passes in a lower-precision format (fp16 or bf16) instead of
fp32, for roughly half the memory and faster matrix multiplies on hardware with dedicated
low-precision units. A master copy of the weights is still kept in fp32 for the optimizer
update, because accumulating many small updates directly in low precision compounds
rounding error over the course of training. With fp16 specifically, gradients also need loss
scaling: multiply the loss by a scale factor before backprop so small gradient values don't
underflow to zero in fp16's narrow exponent range, then unscale before the optimizer step.
bf16 has largely removed the need for loss scaling, because it keeps fp32's exponent range
(same number of exponent bits) and only trades away mantissa precision — the underflow
problem loss scaling exists to fix mostly doesn't occur.

## How it composes

None of this is mutually exclusive. Genuinely large-model training typically runs 3D
parallelism: data parallel across replica groups, pipeline parallel across stages within a
replica, and tensor parallel within each stage — often with ZeRO stage 1 or 2 layered onto
the data-parallel dimension to shave per-device memory further, and mixed precision applied
throughout for speed. Each technique is answering a different constraint (wall-clock time,
model memory, cross-layer communication cost, per-device optimizer memory, raw throughput),
and a training run at scale is usually hitting several of those constraints at once.
