# KV Cache Management

llm-inference-optimization.md covers why the KV cache exists (avoiding quadratic recomputation of
attention over the prefix) and introduces PagedAttention at a high level. This note goes one level
deeper into how a paged KV cache is actually managed as a scarce resource in production: block
allocation mechanics, what happens under memory pressure, and how sharing a prefix across requests
turns cache management into a throughput lever, not just a memory one. The KV cache is usually the
binding constraint on how many sequences a replica can serve concurrently — not compute, not raw
GPU memory bandwidth — so how it's allocated, evicted, and shared directly determines serving
capacity.

## Size math

KV cache bytes per token ≈ 2 (K and V) × n_layers × n_kv_heads × head_dim × bytes_per_element.
With multi-head attention (n_kv_heads = n_heads), a 7B-class model (32 layers, hidden size 4096,
fp16) costs 2 × 32 × 4096 × 2 bytes ≈ 512 KB/token. A single 4K-token conversation already costs
~2 GB of KV cache — larger than the activation memory for that same forward pass. Grouped-query
attention (GQA) shrinks this by sharing K/V projections across multiple query heads: a 70B model
with 8 KV heads instead of 64 costs roughly 320 KB/token despite having 10x the parameters (see
../ml-system-design/llm-inference-serving.md's capacity estimate), which is why GQA is close to
mandatory for serving long-context models economically.

Worked budget: a 13B model in fp16 needs ~26 GB for weights. On an 80GB GPU, after weights and a
few GB of activation/workspace headroom, roughly 50 GB remains for KV cache. At ~400 KB/token,
that's 50 GB / 400 KB ≈ 131,000 tokens of total cache capacity across all concurrent sequences. At
an average 2K-token context per sequence, that bounds the replica to roughly 64 concurrent
sequences before the KV cache — not the compute — is what's full. This is the number continuous
batching's scheduler is actually constrained by on every admission decision.

## Paged allocation

Naive KV cache allocation reserves a contiguous buffer per sequence sized for the maximum possible
length, which wastes memory whenever the actual output is shorter and fragments the remaining
space as sequences of different sizes come and go. PagedAttention manages the cache as fixed-size
blocks (16 tokens/block is a common default) allocated on demand, referenced per sequence through
a block table that maps logical block index to physical block location — the same indirection a
CPU's page table provides for virtual memory. Because blocks are allocated only as a sequence
actually grows, and any free block can serve any sequence, internal fragmentation is capped at
under one block's worth of waste per sequence (at most 15 wasted token-slots, versus potentially
thousands wasted by over-allocating for a worst-case length) and external fragmentation
effectively disappears, since blocks never need to be physically contiguous.

## Eviction and preemption

When the cache fills up under load, the scheduler has to reclaim space from some in-flight
sequence to admit a higher-priority or already-started one. Two strategies, both cheaper than
losing the sequence outright:

- Swap: move a preempted sequence's KV blocks from GPU memory to host (CPU) RAM, and swap them
  back when the sequence resumes. Preserves exact state, but is bounded by PCIe bandwidth — for a
  large sequence this can cost tens of milliseconds, competing with the interconnect other
  transfers need.
- Recompute: drop the preempted sequence's KV blocks entirely and, on resume, recompute them from
  the stored token IDs via a fresh prefill pass. Costs GPU compute instead of PCIe bandwidth,
  which is often the better trade at high batch sizes — the same memory-bandwidth-bound-vs-
  compute-bound distinction that governs decode-step cost generally (see
  llm-inference-optimization.md).

This is a different eviction problem from a general-purpose cache (caching.md's LRU/LFU/TTL
policies): the "value" being evicted is mid-computation sequence state on the hot path of a live
request, not idempotent read-through data, so the policy has to weigh recompute/swap cost against
request priority and how much progress would be lost, not just recency of access.

## Prefix sharing

Many concurrent requests share a literal token prefix — a system prompt, a few-shot example block,
or a long document multiple queries are asked against. Recomputing and storing that prefix's KV
cache separately per request wastes both compute and memory: 1,000 concurrent requests sharing a
2,000-token system prompt would cost 1,000 × 2,000 × 512 KB ≈ 1 TB of redundant cache if computed
independently, versus roughly 1 GB computed once and shared. Prefix caching serves the shared
portion's blocks by reference (copy-on-write in the block table — multiple sequences' tables point
at the same physical blocks until one of them diverges and needs its own copy) rather than
duplicating them, and skips prefill entirely for any request whose prefix already has cached
blocks. Systems that generalize this beyond simple string-prefix matching organize cached blocks
in a radix/prefix tree keyed on token IDs (SGLang's RadixAttention is the reference
implementation), so a new request's prefix is matched against the longest existing cached path
rather than requiring an exact, request-scoped match.

## Interaction with fleet-level scheduling

KV cache management operates within one GPU's (or one tensor-parallel group's) memory budget;
which physical GPUs a replica lands on, and how many replicas exist, is a separate decision made
by the cluster scheduler (gpu-scheduling.md) and the autoscaler (autoscaling-inference.md). The
two layers compose: cache eviction and prefix sharing determine how many sequences one replica can
hold, and fleet scheduling determines how many replicas exist to hold them — a cluster that
under-provisions replicas can't be rescued by better cache management, and vice versa.
