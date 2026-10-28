# Model Serving Patterns

Getting a trained model in front of traffic is two separate decisions that get conflated:
how predictions get computed and delivered (online, batch, or streaming), and how a new
model version gets rolled out safely (shadow, canary, or bandit). Mixing these up leads to
designs that pick a rollout strategy before the serving pattern is even settled.

## Serving patterns

- Online (synchronous) serving: a request comes in, the model scores it, the response goes
  back in the same request-response cycle. Tight latency budget — usually single-digit to
  low-hundreds of milliseconds — because a live user-facing decision is gated on the
  prediction: ranking a feed, approving a transaction, scoring fraud risk at checkout. This
  is the pattern people mean by default when they say "model serving," and it inherits all
  the usual online-system constraints: p99 matters more than average latency, and the
  serving path needs to degrade gracefully (fall back to a cached score or a simple
  heuristic) rather than block the request if the model is slow. See [load-balancing.md](load-balancing.md) for
  how requests get distributed across model replicas.
- Batch serving: score a large, fixed dataset offline on a schedule — nightly, hourly,
  whatever the freshness requirement allows. No per-request latency pressure at all; the
  optimization target is throughput and cost per prediction, so batch jobs run large-batch
  inference on the accelerator to maximize utilization instead of processing one record at
  a time. Used when predictions are consumed on a delay: a daily churn-risk score written to
  a feature store, overnight recomputation of recommendation candidates. Failure mode is
  different too — a batch job can retry or rerun; a synchronous request can't.
- Streaming (near-real-time) serving: score records as they arrive off an event stream,
  rather than either a single synchronous request or a scheduled batch job. Sits in between
  the other two — fresher than batch (seconds, not hours, of lag) but without the strict
  request-response latency contract of online serving, since nothing is blocking on the
  response in the same way a user's page load is. Typical shape: a Kafka-style pipeline
  where a consumer pulls transaction or event records, scores each one, and writes the
  result downstream (an alert queue, a feature store, another topic). See
  [message-queues.md](message-queues.md) for the underlying transport this pattern usually rides on.

## Rollout patterns

Once the serving pattern is fixed, a separate question is how to get a new model version
into production without breaking things.

- Shadow deployment: mirror live traffic to the new model, compute its predictions, but
  never act on them or show them to users — the production model's output is still what
  gets used. Zero user risk, since the new model's predictions are pure observation, at the
  cost of double compute (every request gets scored twice) and the fact that you can only
  compare prediction-level metrics, not downstream business outcomes, since the shadow
  model's decisions never actually happened.
- Canary deployment: route a small percentage of real traffic to the new model and act on
  its predictions, watching guardrail metrics (error rate, latency, business KPIs) before
  ramping the percentage up. Real user risk, but bounded to whatever slice is exposed. This
  is the same infrastructure pattern as a general software canary rollout — it just gates
  progression on model-quality metrics instead of, or in addition to, error rates and
  latency.
- Multi-armed bandit routing: instead of a fixed split decided up front, dynamically shift
  traffic share toward whichever variant is performing best so far, balancing exploration of
  underperforming variants against exploitation of the current best. Contrast with a fixed
  A/B test, which commits to a static allocation (say 50/50) for the whole experiment
  duration regardless of interim results — see [../ml-system-design/ab-testing-and-experimentation.md](../ml-system-design/ab-testing-and-experimentation.md).
  Bandits converge to the better variant faster and waste less traffic on a known-worse
  option, but that same adaptivity makes after-the-fact statistical analysis harder: because
  the allocation itself changed in response to the data, standard confidence-interval math
  for a fixed-sample test doesn't directly apply, and answering "how much better was variant
  B, precisely" needs bandit-aware statistics rather than a plain t-test on the collected
  traffic.

## The two axes are independent

Serving pattern and rollout pattern compose freely — they're orthogonal choices, not a
package deal. An online-serving fraud model can still be canaried, ramping from 1% to 100%
of live scoring traffic while guardrail metrics are watched. A batch-serving recommendation
model can be shadow-tested against its predecessor for a few nightly runs before the cutover
— both models score the same batch, only the incumbent's output ships, and the two output
sets get diffed offline. A streaming fraud-scoring pipeline can run a bandit across two
scoring models per transaction stream. The rollout pattern answers "how do we trust the new
version," the serving pattern answers "how do predictions reach the point of use" — neither
constrains the other. See [../ml-system-design/ad-click-prediction.md](../ml-system-design/ad-click-prediction.md) and
[../ml-system-design/ml-experiment-tracking-platform.md](../ml-system-design/ml-experiment-tracking-platform.md) for concrete systems where both axes
show up together.
