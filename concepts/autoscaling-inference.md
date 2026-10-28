# Autoscaling Inference

Autoscaling a web service and autoscaling a GPU inference fleet look similar from a distance —
watch a signal, add or remove capacity — but the mechanics diverge on every axis that matters:
what signal to scale on, how long new capacity takes to come online, and whether it's ever worth
scaling to zero. A CPU service can add a replica in seconds and scale on CPU utilization because
CPU load tracks request cost reasonably well. A GPU inference replica can take minutes to
provision and load weights, and utilization is a misleading signal because request cost varies
wildly and the binding resource is often memory, not compute.

## Queue depth vs. utilization

Raw GPU utilization looks saturated at 100% both when a replica is doing useful batched work and
when it's memory-bound and stalling on KV cache pressure rather than compute — a batch of one
short request can peg utilization briefly without meaning the replica is actually near capacity,
and a replica juggling many long sequences can show moderate compute utilization while its KV
cache ([kv-cache-management.md](kv-cache-management.md)) is the real thing about to run out. Request count is similarly
misleading for LLM-style workloads: a 50-token completion and a 4,000-token completion are "one
request" each but differ by up to 80x in GPU-time consumed, so scaling on QPS either
under-provisions for a burst of long generations or over-provisions for a burst of short ones. The
signal that actually tracks load is queue depth (or tokens-in-flight, or an estimated
time-to-drain the current queue at current throughput) per replica — see
[../ml-system-design/llm-inference-serving.md](../ml-system-design/llm-inference-serving.md)'s autoscaling section for the concrete case. This
generalizes past LLMs: any workload with heterogeneous per-request cost should scale on a
work-remaining signal, not a request-count or plain-utilization one.

## Cold starts

Bringing up a new inference replica isn't just "start a process" — it's provisioning a GPU node,
pulling a container image, and loading model weights into GPU memory, and every step is slow at
this scale. Pulling a 70B model's ~140 GB of fp16 weights from object storage over a 10 Gbps link
takes roughly 140 GB × 8 / 10 Gbps ≈ 112 seconds just for the transfer, before CUDA context
initialization, kernel warmup, and health checks add more. A GPU node itself can take minutes to
provision if the cluster doesn't already have spare capacity. Reactive autoscaling that waits for
queue depth to spike before requesting a new replica is therefore chasing load with a multi-minute
lag — by the time the replica is serving traffic, the burst that triggered the scale-up may already
have passed, or (worse) users have been queued or dropped for the entire provisioning window.
Mitigations: keep a warm buffer of idle-but-loaded replicas above the reactive floor, pre-bake
container images and cache weights on nodes rather than pulling on every scale-up, and size the
buffer to the provisioning latency rather than to steady-state load.

## Scale-to-zero

For low-traffic or bursty endpoints — a dev/staging environment, an internal tool hit a few times
an hour — running zero replicas between requests can be the right cost tradeoff, at the price of
paying the full cold-start latency (the ~100+ seconds above, easily) on whichever request arrives
first after idling. That tradeoff is rarely acceptable for latency-sensitive, user-facing
production traffic, where a multi-minute first-request penalty is a visible outage, not a
rounding error. The usual middle ground is scale-to-min rather than scale-to-zero: keep one warm
replica per model as a floor, and let true zero-replica states apply only to genuinely idle,
non-critical endpoints. Some platforms narrow the gap further with a pre-warmed pool loaded with
common base weights that a LoRA adapter or small delta can attach to quickly, avoiding the full
weight-load cost even on a cold path.

## Predictive autoscaling

Purely reactive scaling is fundamentally rate-limited by provisioning latency — it cannot outrun a
demand spike that arrives faster than a new replica can come online, no matter how sensitive the
queue-depth threshold is set. Predictive autoscaling addresses this by pre-scaling ahead of
anticipated load using historical patterns — diurnal traffic curves, day-of-week seasonality,
scheduled events (a product launch, a marketing push) — so capacity is already in place before the
reactive signal would have fired. This doesn't replace reactive scaling, it front-runs it: known,
recurring patterns are handled predictively (with enough lead time to absorb GPU provisioning
delay), while genuinely unanticipated bursts still fall back to the reactive queue-depth loop. The
combination mirrors general capacity planning — long-lead-time forecasting for the known-seasonal
component, fast reactive headroom for the unpredictable residual.

## Where it fits

Autoscaling decides how many replicas exist; [gpu-scheduling.md](gpu-scheduling.md) decides which physical GPUs those
replicas land on and how tightly they pack against everything else in the cluster; [load-balancing.md](load-balancing.md)
decides how traffic is spread across whichever replicas are currently up. All three have to agree
for the fleet to behave well under load — an autoscaler that adds replicas the cluster scheduler
can't actually place (no free GPU meeting the job's NVLink-domain requirement) just produces
pending, unschedulable replicas instead of served capacity.
