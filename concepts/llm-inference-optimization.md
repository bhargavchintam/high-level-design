# LLM Inference Optimization

LLM inference is slow and expensive relative to a typical small model because generation is
autoregressive: producing each output token requires a full forward pass conditioned on every
token generated so far, and that pass can't start until the previous one finishes — no way to
parallelize token generation within a single sequence, just one forward pass per token for
however many tokens the response needs. Everything in LLM serving optimization comes down to
doing useful work in parallel across sequences (batching), doing each forward pass cheaper
(quantization), avoiding redundant recomputation within a sequence (KV cache management), or
reducing the number of sequential steps needed (speculative decoding).

## Batching: static vs. continuous

The naive approach, static batching, assembles a fixed batch of requests up front and runs them
through decoding together until every sequence in the batch finishes. The problem is that
sequences finish at different lengths — one request wants a 20-token answer, another wants 500
— and a static batch is held hostage by its slowest member: shorter sequences sit idle (padded)
while the batch waits for the longest one, wasting GPU cycles that could've gone to a new request.

Continuous batching (also called dynamic batching) fixes this at the scheduler level: at every
decoding step, finished sequences leave the batch and new requests join, so the GPU is always
working on a full batch of active sequences instead of padding around finished ones. This is
the core mechanism behind high-throughput serving systems like vLLM and TGI, and it's
consistently one of the largest single throughput wins in LLM serving — often several times the
throughput of static batching under realistic, variable-length request traffic.

## Quantization

Quantization reduces the numeric precision of model weights (and sometimes activations) — from
fp16/bf16 down to int8 or int4 — to shrink memory footprint and increase throughput. This matters
because autoregressive decoding is usually memory-bandwidth-bound, not compute-bound: each
decoding step streams the full set of model weights (and the KV cache) from GPU memory for one
token's worth of compute per sequence, so the bottleneck is how fast bytes move, not how fast
they're multiplied. Cutting weight precision in half roughly halves the bytes moved per step,
which is why quantization improves decoding throughput more than raw FLOP counts would suggest.

Post-training quantization methods like GPTQ and AWQ quantize a model after it's already
trained, without a full retraining pass, calibrating on a small dataset to minimize accuracy loss
from the reduced precision. The tradeoff is accuracy vs. throughput/memory: int8 is usually close
to lossless, int4 saves more memory and bandwidth but shows measurable quality degradation
depending on the model and task, and the right operating point is workload dependent.

## The KV cache

Self-attention means every new token attends over all previous tokens in the sequence. Naively,
that requires recomputing the key and value projections for the entire prefix on every single
new token — quadratic work for a sequence of length n. The KV cache avoids this by storing the
key/value projections for every previous token the first time they're computed, so generating
token n+1 only needs a forward pass for the new token, reusing the cached K/V for everything
before it. That's a straightforward compute-for-memory trade, and the memory side is the catch:
KV cache size grows linearly with sequence length × batch size × number of layers × hidden size.
At long context lengths or large batch sizes, the KV cache — not the model weights — is often
the dominant consumer of GPU memory, and it's the central resource-management problem in LLM
serving: how many concurrent sequences you can batch, and how long a context you can serve, is
gated by how much KV cache fits in memory, not by compute.

## PagedAttention

Naive KV cache management allocates a contiguous block of memory per sequence sized for its
maximum possible length, which wastes memory when the actual output is shorter (over-allocation)
and fragments available memory as sequences of different sizes are allocated and freed
(fragmentation) — both of which shrink the number of sequences that can be batched concurrently.
PagedAttention, introduced by vLLM, borrows the idea of OS virtual memory paging: instead of one
contiguous allocation per sequence, the KV cache is managed in fixed-size blocks that don't need
to be contiguous in memory, referenced through a per-sequence block table. This eliminates both
over-allocation (blocks are allocated on demand, not for a worst-case length) and fragmentation
(any free block can serve any sequence), which is what makes aggressive continuous batching
practical — the scheduler can pack far more concurrent sequences into the same GPU memory than
naive contiguous allocation allows.

## Speculative decoding

Speculative decoding attacks the sequential-steps problem directly rather than the per-step cost.
A small, fast draft model proposes several tokens ahead speculatively; the large target model
then verifies all of them in a single forward pass — checking whether the target model would
have produced each token is one parallel pass across the proposed tokens, unlike generating them
one at a time. The target model accepts the matching prefix of the draft's guesses and resamples
from the first point of divergence, which guarantees the output distribution matches what the
target model would have produced alone. When the draft's guesses are frequently correct — common
since much of token generation is low-entropy (completing a word, closing a bracket) — this
trades extra compute for fewer sequential steps on the large model, cutting latency without
changing output quality.

## Putting it together

These four techniques are complementary, not competing, because they attack different resources:
batching maximizes GPU throughput by keeping it fed with parallel work across sequences,
quantization reduces the memory-bandwidth cost of each forward pass, KV cache management
(PagedAttention) maximizes how many sequences fit concurrently within a fixed memory budget, and
speculative decoding reduces the number of sequential steps per sequence. Production serving
stacks combine all four — a quantized model, served with continuous batching, backed by a paged
KV cache, optionally with speculative decoding for latency-sensitive traffic — because each one
solves a different piece of the same problem: autoregressive decoding is sequential,
memory-bandwidth-bound, and memory-capacity-constrained, and no single technique addresses all
three.
