# Model Monitoring and Drift

A model that passed every eval gate and canary check at rollout ([mlops-and-model-cicd.md](mlops-and-model-cicd.md)) can
still degrade in production weeks later without a single line of code changing, because the thing
it was trained to model — the input distribution, or the relationship between inputs and outcomes
— keeps moving after training stops. Monitoring exists to detect that movement before it shows up
as a business-metric regression discovered by someone outside the ML team. The two failure modes,
data drift and concept drift, need different detection mechanisms and run on different clocks, and
conflating them is the most common design mistake in home-grown monitoring.

## Data drift vs. concept drift

Data drift is a shift in the input feature distribution itself — a new acquisition channel changes
the mix of device types in traffic, a seasonal effect shifts purchase-amount distributions —
detectable purely by comparing feature distributions over time, with no labels required at all.
Concept drift is a shift in the relationship between features and the target: the same feature
values now correlate with a different outcome than they did at training time (fraud tactics change,
so a previously-predictive pattern stops meaning what it used to), detectable only against
ground-truth labels. A model can suffer serious concept drift with zero measurable data drift,
which is why performance-against-labels monitoring is a genuinely separate pipeline from
feature-distribution monitoring, not a redundant one — see
[../ml-system-design/ml-monitoring-and-drift.md](../ml-system-design/ml-monitoring-and-drift.md) for the full two-pipeline architecture (a
label-join pipeline for the delayed, ground-truth path; a distribution aggregator for the
label-free path) this distinction drives.

## Measuring drift: PSI and KL-divergence

Population Stability Index buckets a feature's values into bins fixed from the training-time
baseline, then compares the proportion of current observations in each bin against the baseline
proportion: `PSI = sum((actual_pct - expected_pct) * ln(actual_pct / expected_pct))` over bins.
The widely-used rule of thumb: PSI < 0.1 is stable, 0.1-0.25 is a moderate shift worth watching,
> 0.25 is significant enough to investigate. KL-divergence, `D_KL(P||Q) = sum(P(x) * log(P(x)/Q(x)))`,
gives a more information-theoretic distance but is asymmetric — `D_KL(P||Q) != D_KL(Q||P)` — which
matters when deciding which distribution is the reference, and is less immediately interpretable
to an on-call engineer triaging an alert at 2am. PSI (or a symmetric variant like Jensen-Shannon
divergence) dominates operational alerting for that interpretability reason; KL-divergence sees
more use in offline/research analysis where the asymmetry is a feature, not a bug, and there's
time to reason about it carefully.

## Label latency

Detection speed for concept drift is structurally bounded by how fast ground truth arrives, no
matter how sophisticated the monitoring pipeline is. A fraud model's label (confirmed fraudulent
or not) can arrive within seconds to minutes via a downstream check, so performance monitoring can
be near-real-time. A churn model's label is only known 30 days out by definition — the monitoring
system cannot detect a performance regression faster than that window permits, and pretending
otherwise just produces a metric with silently poor confidence. The practical mitigation for
long-label-latency models is leaning more heavily on label-free proxies — data drift and
prediction-distribution monitoring — as an early warning, while being honest that true performance
confirmation lags by the label window regardless. Any monitoring SLA needs to be set per model
against its own label latency, not a single platform-wide number.

## Alerting without alert fatigue

A drift score checked against a fixed threshold, across every feature of every monitored model, on
an hourly cycle, produces false positives from pure statistical noise if thresholds aren't tuned —
some features naturally have higher baseline variance than others, and one global threshold either
misses real drift in low-variance features or spams alerts on high-variance ones. The fixes that
matter in practice: tune thresholds per feature from that feature's own historical volatility
rather than a constant, require sustained deviation across multiple consecutive windows before
firing (filtering single-window noise), and deduplicate correlated alerts — ten features that drift
together from one underlying cause should page once with all ten listed, not page ten times. A
monitoring system whose alerts get ignored because they fire constantly on noise is a worse outcome
than not monitoring at all, since it creates false confidence that degradation would be caught.

## What monitoring feeds

Drift and performance signals aren't just dashboards — they're inputs to the rollback and
promotion decisions in [mlops-and-model-cicd.md](mlops-and-model-cicd.md) (a model already at 100% traffic that starts
showing concept drift is a rollback or retrain trigger, not just an FYI) and the natural comparison
point for shadow deployment ([model-serving-patterns.md](model-serving-patterns.md)), which feeds a candidate's predictions into
the same monitoring infrastructure before it ever takes real traffic. Monitoring that's disconnected
from those downstream actions — drift detected but nothing automated or on-call reacts to it — has
all the engineering cost of the pipeline with none of the safety benefit.
