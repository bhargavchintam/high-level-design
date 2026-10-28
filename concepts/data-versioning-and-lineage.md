# Data Versioning and Lineage

Code has had reliable versioning (git) for decades; the datasets and features that code trains on
mostly haven't, and that gap is where a large share of ML reproducibility failures come from. Two
distinct problems get bundled under "data versioning": being able to name and retrieve an exact
past state of a dataset or feature (versioning), and being able to answer what value a feature had
at a specific past moment, not now (point-in-time correctness). Lineage is the connective layer on
top — tracking which versioned inputs produced which downstream outputs, so a model artifact can
be traced back to exactly what built it.

## Dataset and feature versioning

The simplest approach — snapshot the whole dataset on every change — is correct but wasteful: a
500 GB dataset snapshotted daily as full copies costs 500 GB x 365 ≈ 182 TB/year even if only 2% of
the data changes per day. Copy-on-write / manifest-based versioning (Delta Lake, Apache Iceberg,
lakeFS, DVC) instead stores the base data once and records each version as a manifest of which
files or row-groups changed, reconstructing any version by reading the base plus the deltas up to
that point. At the same 2%/day change rate, that's roughly 500 GB + 365 x 10 GB ≈ 4.15 TB/year —
a 40x+ reduction — which is why every mainstream table-versioning format uses a manifest/delta
scheme rather than full snapshots, the same copy-on-write idea PagedAttention's block sharing uses
for KV cache blocks ([kv-cache-management.md](kv-cache-management.md)) applied to storage instead of GPU memory. Feature
versioning adds a second dimension on top of the data itself: the transformation logic that
produces a feature from raw data is versioned separately, since the same raw data run through a
changed transformation (a different window size, a bug fix in an aggregation) produces a different
feature value without the underlying source data changing at all.

## Point-in-time correctness

A training example labeled "user X churned, observed at time T" has to be joined with feature
values as they existed at T, not as they exist at query time (which could be weeks or months
later). The naive join — take whatever the feature's current value is — silently leaks
information: if "total purchase count" has grown since T, the model trains on a value it never
would have seen at real serving time for that example, inflating offline eval metrics that then
fail to hold in production. Concretely: a rolling-30-day-count feature computed on day 0 might read
14; recomputed on day 45 (whenever the training job happens to run) it might read 61, because 45
more days of activity have accumulated. A point-in-time-correct join looks up the value whose
recorded timestamp is the latest one still `<= T` from an append-only, timestamped feature log,
never the latest value the table currently holds. This is exactly the mechanism
[../ml-system-design/feature-store.md](../ml-system-design/feature-store.md)'s offline store implements, and it's the reason an offline
feature store can't just be "the online store, but bigger" — the online store intentionally
overwrites old values because it only answers "what's true now," while point-in-time correctness
requires retaining history to answer "what was true at T" for arbitrary past T.

## Lineage

Lineage is the graph connecting a raw source through every transformation to a final artifact:
`raw table -> feature version -> training dataset version -> training run -> model version`, the
same chain [../ml-system-design/ml-experiment-tracking-platform.md](../ml-system-design/ml-experiment-tracking-platform.md)'s lineage_edges table records.
Two practical uses drive most lineage tooling (OpenLineage is the common open standard): impact
analysis (if an upstream source column is deprecated or found to be corrupted, which downstream
features, datasets, and deployed models are affected — a graph traversal, not a manual audit) and
compliance (proving a deletion request actually propagated through every derived, cached, or
versioned copy of a user's data, not just the source table it originated in — a graph with a
dangling or unreachable derived copy is a compliance gap, not just a data-quality one).

## Reproducibility ties it together

Versioning without lineage answers "what did this dataset look like at version 12" but not "which
models were trained on it"; lineage without versioning answers "this model came from this dataset"
but not "what that dataset actually contained," since an unversioned, mutable source undermines the
lineage pointer the moment the source changes underneath it. Both are needed for the reproducibility
guarantee [mlops-and-model-cicd.md](mlops-and-model-cicd.md)'s promotion gates and rollback path depend on — a registry can
faithfully record a dataset-version identifier next to a promoted model, but that record is only
trustworthy if the versioning system underneath actually guarantees that identifier points at
something immutable, months or years later, when an audit or an incident response needs to
reconstruct exactly what a flagged model was trained on.
