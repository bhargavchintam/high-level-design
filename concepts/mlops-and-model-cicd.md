# MLOps and Model CI/CD

Shipping a new model version safely is structurally the same problem as shipping a new code
release — build, test, promote through environments, roll out gradually, roll back fast if it's
bad — but a model has two failure modes ordinary CI/CD doesn't: it can be functionally correct
(no crashes, normal latency) while being silently wrong (worse predictions), and its behavior
depends on training data as much as on code. Model CI/CD is the pipeline that turns a training run
into a promoted, deployed artifact, with automated eval gates standing in for unit tests and
canary/shadow rollout standing in for a gradual traffic ramp.

## Registry and staging

The model registry is the source of truth for "what is the currently-approved artifact for model
X" — a versioned record, decoupled from the training run that produced it, moving through explicit
stages (commonly `None -> Staging -> Production -> Archived`). Registration and promotion are
separate actions: a run finishing training just registers a candidate version; promoting it to
`Staging` triggers deployment to a pre-production environment that mirrors production
configuration (same serving stack, same feature pipeline, non-production traffic or a shadow feed)
specifically so validation happens against the same conditions the model will actually run under,
not a laptop notebook. See ../ml-system-design/ml-experiment-tracking-platform.md for the registry's
data model and how a stage transition becomes the trigger that downstream deployment systems act on.

## Eval gates

An eval gate is an automated check a candidate must pass before it's allowed to advance a stage —
the model equivalent of CI tests blocking a merge. Typical gates: offline metrics on a held-out set
meeting a threshold (e.g. eval accuracy within 0.5% of the current production model, not just
"good in isolation" — a candidate that's better on average but worse on a critical slice should
still fail), latency/throughput regression tests (p99 inference latency within some bound of the
incumbent), fairness/bias checks on protected slices, and a behavioral diff against the current
production model's predictions on a fixed reference set, to catch large unexplained shifts before
they reach real traffic. Gates that only check "is this a valid model" (loads, doesn't crash, meets
schema) catch far less than gates that check "is this model at least as good as what it's replacing,"
which is why a meaningful eval-gate suite needs the incumbent's own metrics as the comparison
baseline, not a fixed absolute threshold decided once and never revisited.

## Canary and shadow rollout

Once a candidate clears eval gates, the deployment mechanism for getting it in front of real
traffic safely is shadow deployment (mirror live traffic, compute predictions, never act on them)
or canary deployment (route and act on a small traffic percentage, ramp up while watching guardrail
metrics) — see model-serving-patterns.md for the full definitions and their tradeoffs, which apply
unchanged here. A typical ramp: shadow for a fixed window (hours to days) to validate behavioral
consistency and operational health with zero user risk, then canary at 1% -> 10% -> 50% -> 100% of
traffic, holding at each step until guardrail metrics (error rate, latency, and — where label
latency allows — business KPIs) stay within bounds for a minimum soak time before advancing. The
eval gates from the previous section run once, before rollout starts; canary guardrails run
continuously, during rollout, against real production behavior the offline gates couldn't fully
anticipate.

## Rollback

Rollback has to be fast and cheap, because it's the response to something already going wrong in
production. Structuring it as a registry pointer flip — routing traffic back to the prior
`Production` version, which is why that version stays warm and deployed rather than being
garbage-collected the moment a new one promotes — is what makes rollback a seconds-scale operation
instead of a redeploy. This mirrors blue-green rollback in general software delivery: the old
version never actually leaves, it just stops receiving traffic, so reverting is a routing change,
not a rebuild. Rollback triggers are usually the same guardrail metrics the canary watches, plus
signals from ongoing production monitoring after full rollout — see model-monitoring-and-drift.md
for how a model already at 100% traffic gets flagged for demotion after a canary window has long
since closed.

## Reproducibility as the load-bearing requirement

Every gate above is only meaningful if the artifact being evaluated can be traced back to exactly
what produced it — the code commit, the environment (container digest, not a loose dependency
range), and the dataset/feature version it trained on. Without that, "roll back to the previous
version" and "retrain to reproduce a flagged model's exact behavior for audit" both become
unreliable. This is why model CI/CD depends on data-versioning-and-lineage.md as a prerequisite,
not an optional nicety: a registry that faithfully records a dataset version pointer is only as
trustworthy as the versioning system's guarantee that the pointed-to data hasn't silently changed
underneath it.
