# LLM Inference Serving Platform

## Requirements

### Functional
- Serve text generation from a large decoder-only transformer (assume a 70B-parameter class
  model, the scale at which single-GPU serving is no longer an option) behind a request/response
  and a streaming API.
- Support variable-length prompts and variable-length generations, with early termination
  (stop sequences, client cancellation) that frees resources immediately rather than running to
  a max-token cap.
- Support multiple concurrent model versions/fine-tunes behind the same endpoint (routed by
  request metadata) without each needing a dedicated idle GPU fleet.
- Token-level streaming to the client, so the user sees output incrementally instead of waiting
  for the full generation.

### Non-functional
- Time-to-first-token (TTFT) p50 < 300ms, p99 < 1.5s — this is latency-sensitive and dominated
  by queueing delay and prompt-processing (prefill) time, not generation itself.
- Inter-token latency (ITL) p50 < 50ms (~20 tokens/sec perceived streaming speed per request) —
  this is throughput-sensitive and is where batching decisions matter most.
- GPU utilization is the primary cost lever at this scale — idle or under-batched GPU time is
  the dominant line item in the infrastructure budget, more so than any other layer of the
  stack.
- Autoscaling must react to load within the time it takes a GPU node to come online (often
  minutes), which is far slower than request-level bursts — the design has to absorb burst
  traffic with queueing/batching headroom on already-running capacity, not by waiting for new
  nodes.

## Capacity Estimation

Assume 2M requests/day, average prompt 800 input tokens, average generation 400 output tokens.
Total tokens/day: input 2M × 800 = 1.6B, output 2M × 400 = 0.8B → 2.4B tokens/day total.

Request rate: 2M / 86,400 ≈ 23 req/sec average, ~70 req/sec peak (3x). Output token throughput
needed: 0.8B tokens/day × 3 (peak factor) / 86,400 ≈ 27,800 output tokens/sec at peak across the
fleet — this is the number that sizes GPU count, not request count, since cost scales with
tokens generated, not requests received.

**Weight memory.** A 70B-parameter model at fp16 needs ~140 GB just for weights (2 bytes/param).
A single H100 (80GB) can't hold it — minimum 2-way tensor parallelism (2 × 80GB = 160GB) leaves
only ~20GB for KV cache and activations, too tight for meaningful concurrency. A practical
deployment uses 4-way tensor parallelism (4 × 80GB = 320GB), leaving ~180GB of headroom for KV
cache across concurrent sequences — this is the real reason parallelism degree is chosen the way
it is: not compute, but memory headroom for the KV cache.

**KV cache memory.** Per-token KV cache size ≈ 2 (K and V) × n_layers × d_model × 2 bytes
(fp16). For a 70B-class model with grouped-query attention (a handful of KV heads shared across
many query heads, standard in real 70B deployments to shrink exactly this number), a realistic
figure is on the order of 300-500 KB/token rather than the multi-MB/token figure a naive
multi-head-attention calculation would give — GQA is, in large part, adopted specifically to make
KV cache memory tractable at long context lengths. At ~400KB/token, a 2K-token sequence (prompt +
generation) costs ~800MB of KV cache; with ~180GB of headroom per 4-GPU replica, that bounds
concurrent sequences per replica to roughly 200-250 before KV cache, not compute, becomes the
limiting resource.

**Fleet size.** To sustain ~27,800 output tokens/sec at peak, and assuming a well-batched 4-GPU
replica achieves on the order of 3,000-4,000 output tokens/sec aggregate (realistic for
continuous batching on H100s at this model size), the fleet needs roughly 7-9 replicas × 4 GPUs
≈ 28-36 GPUs at peak, provisioned with autoscaling headroom above that.

**Cost per 1M tokens.** At a blended cloud price of ~$3.50/GPU-hour for H100, a 4-GPU replica
costs $14/hr. At 4,000 output tokens/sec sustained, that's 4,000 × 3,600 = 14.4M tokens/hr, so
cost ≈ $14 / 14.4M × 1M ≈ **$0.97 per 1M output tokens** at good utilization — this is an
order-of-magnitude anchor, not a quote; it moves a lot with batching efficiency, sequence length
distribution, and GPU spot pricing, but it's the right ballpark for self-hosting a 70B-class
model versus paying a hosted-API premium.

## API Design

```
POST /v1/completions           { model, prompt, max_tokens, stream: bool, stop?: [...] }
     -> non-streaming: 200 { text, tokens_generated, finish_reason }
     -> streaming: text/event-stream, one SSE chunk per token (or small token group)

POST /v1/completions/{id}/cancel
     -> 200  (frees the KV cache slot and stops further generation immediately)

GET  /v1/models
     -> lists deployed model versions and their routing keys
```

Streaming responses are the default assumption for anything user-facing — returning the full
generation only after completion means TTFT and total latency are the same number, which is a
much worse perceived-latency profile for long generations even if total throughput is identical.

## Data Model / Serving-State Schema

There isn't a persistent "data model" in the database sense here — the state that matters is
per-request GPU-resident state, not rows in a store:

```
request_state (in-memory, per in-flight request, lives on the GPU node serving it)
  request_id, prompt_tokens[], generated_tokens[]
  kv_cache_handle          (pointer into the paged KV cache blocks for this sequence)
  status: queued | prefilling | decoding | done | cancelled

routing_table (small, low-latency, e.g. in a coordination service)
  model_version -> [replica endpoints], current load per replica

request_log (offline, for eval/fine-tuning data collection)
  request_id, model_version, prompt, completion, latency_ms, token_counts, ts
```

The KV cache itself is the closest thing to "data" this system manages at scale, and it's
treated as a scarce, actively-managed memory resource (allocated in fixed-size pages, reclaimed
the instant a sequence finishes or is cancelled) rather than as a conventional data store.

## High-Level Architecture

```
 request path:
 client ──POST /completions──► API gateway / router (picks replica for requested
                                 model version, load-aware)
                                          │
                                          ▼
                        Inference server (per GPU-replica group, e.g. 4-GPU
                        tensor-parallel shard of one model instance)
                          - scheduler: continuous batching loop, admits new
                            requests into the running batch every iteration
                          - prefill: process the full prompt, populate KV cache
                          - decode: generate one token per request per iteration,
                            for every sequence currently in the batch
                                          │
                                          ▼
                        tokens streamed back to client as generated (SSE/chunked)

 fleet management:
 autoscaler ──watches──► queue depth + tokens-in-flight per replica (not raw QPS)
     │
     └─► adds/removes replicas; new replica load = weights from model registry
          (weights pulled from object storage, sharded per tensor-parallel rank)

 (optional) speculative decoding path:
 small draft model ──proposes k tokens──► large model verifies all k in one
 forward pass ──► accepted tokens emitted immediately, rejected ones re-drafted
```

## Deep Dives

**Continuous (iteration-level) batching.** Static batching — waiting to fill a batch, running it
to completion, only then admitting new requests — wastes GPU time because sequences finish at
different lengths; a batch is only as fast as its longest member, and finished slots sit idle
until the whole batch completes. Continuous batching (the technique behind systems like vLLM and
Orca) instead makes a scheduling decision every decode iteration: any sequence that finishes
frees its slot immediately, and a new queued request can be admitted into the batch on the very
next iteration rather than waiting for the whole batch to drain. This is the single largest
throughput lever in LLM serving — it's the difference between GPUs idling on short sequences
while long ones finish, and GPUs staying saturated continuously.

**Paged KV cache.** Naively allocating a contiguous KV cache buffer sized for the maximum
possible sequence length, per request, wastes enormous memory to fragmentation (most sequences
are much shorter than the max, but the allocator doesn't know that in advance) and to internal
fragmentation between requests. PagedAttention (the technique vLLM introduced) manages KV cache
in fixed-size blocks/pages, allocated on demand as a sequence grows, non-contiguous in memory —
directly analogous to virtual memory paging in an OS. This is what makes the earlier "concurrent
sequences per replica" estimate close to the theoretical memory bound instead of far below it;
without paging, fragmentation alone can cost 50%+ of effective KV cache capacity.

**Tensor parallelism vs. pipeline parallelism for serving.** Tensor parallelism splits each
layer's matrix multiplications across GPUs (all GPUs work on every layer, in lockstep, for every
token) — low latency per token, since every GPU is active on every step, but requires
high-bandwidth interconnect (NVLink) between the GPUs in a shard because of the per-layer
all-reduce. Pipeline parallelism instead splits layers themselves across GPUs (GPU 1 holds layers
1-20, GPU 2 holds layers 21-40, ...) — needs far less inter-GPU bandwidth, but introduces pipeline
bubbles (later stages wait on earlier ones) that hurt latency for a single sequence, though this
is easier to hide when many sequences are in flight via continuous batching. Production serving
of a single large model on a well-connected node (e.g. an 8-GPU NVLink server) typically favors
tensor parallelism within the node for latency, reserving pipeline parallelism for spanning
across nodes when the model doesn't fit in one node's combined memory even with tensor
parallelism maxed out.

**Speculative decoding.** Decoding is inherently sequential — token N+1 depends on token N — so a
single large-model forward pass produces exactly one token no matter how much spare GPU compute
sits idle during that pass (decode is typically memory-bandwidth-bound, not compute-bound, at
small batch sizes). Speculative decoding exploits that idle compute: a small, fast draft model
proposes several tokens ahead (e.g. k=4-8), and the large model verifies all of them in a single
forward pass (cheap, since verification is parallel across the k proposed positions, unlike
generation). Accepted tokens are emitted immediately; on the first rejected token, the large
model's own distribution is used instead and drafting resumes from there. Speedup depends
entirely on acceptance rate — a draft model well-aligned with the target model's distribution
(often a distilled or smaller version of the same model family) can realistically yield 2-3x
wall-clock speedup on latency-bound (low-batch-size) workloads; the benefit shrinks as batch size
grows and the large model's decode step becomes compute-bound anyway, since there's no more idle
compute left to exploit.

**Autoscaling signal.** Scaling on raw request count is misleading here because requests are
wildly heterogeneous in cost — a 50-token completion and a 4,000-token completion are the same
"one request" but differ by 80x in GPU-time consumed. The autoscaler instead watches
queue-depth and tokens-in-flight (or a derived estimate of time-to-drain the current queue at
current throughput) per replica, and scales on that. Because GPU nodes take minutes to
provision, autoscaling reacts to sustained load trends, not instantaneous bursts — burst
absorption is handled by request queueing (with a max queue-wait SLA before shedding load) on
already-running capacity.

## Bottlenecks & Scaling

KV cache memory, not raw FLOPs, is usually the binding constraint on concurrency per replica at
this model scale — which is why paged allocation and GQA both exist specifically to relax that
constraint rather than to speed up compute. Prefill (processing the prompt) and decode
(generating tokens) have very different compute profiles — prefill is compute-bound and
parallel across the whole prompt in one pass, decode is memory-bandwidth-bound and strictly
sequential — and mixing long-prefill and many-decode requests in the same batch can starve
decode latency for already-streaming requests; some serving stacks split prefill and decode onto
separate worker pools for exactly this reason. Cross-node bandwidth is the ceiling on how far
tensor parallelism can scale before pipeline parallelism (or just running independent replicas)
becomes the better lever. Finally, at the fleet level, cost is dominated by keeping GPUs
saturated with well-batched work — the entire architecture (continuous batching, paged KV cache,
prefill/decode disaggregation, speculative decoding) is, underneath the acronyms, about closing
the gap between theoretical GPU throughput and what naive request-at-a-time serving would
actually achieve.
