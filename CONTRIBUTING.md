# Contributing

Thanks for taking a look. A contribution here is usually a new design doc, a fix to a number, or a
clearer explanation. Write plainly, and say why each choice was made and what it costs.

## Propose a design doc

1. Open an issue first. Name the system you want to design, and say in a few lines why the docs in
   the [README index](README.md) do not already cover it.
2. Put the doc in the folder it belongs to, named in kebab-case:
   - `ml-system-design/` for ML and AI systems, for example `ml-system-design/llm-guardrails.md`;
   - `case-studies/` for classic product and infrastructure systems;
   - `concepts/` for a short note on one building block.
3. Add one row to the matching table in `README.md`: a link and a one-line summary taken from the
   doc's first lines.
4. Open a pull request that links the issue.

## Expected sections

Design docs in `ml-system-design/` and `case-studies/` use the same seven sections, in this order:

1. `## Requirements`, split into `### Functional` and `### Non-functional`. Start the functional list
   with the main thing the system must do; that line becomes the summary in the README. Give the
   non-functional targets as numbers, for example p50 and p99 latency in ms, or availability in nines.
2. `## Capacity Estimation`
3. `## API Design`
4. `## Data Model`
5. `## High-Level Architecture`
6. `## Deep Dives`: three to five of the hardest parts, each with the options and the trade-off.
7. `## Bottlenecks & Scaling`: what breaks first as load grows, and what you would do about it.

Concept notes are shorter. Open with a paragraph that says what the idea is and which problem it
solves, then add a few sections on the mechanics and the trade-offs.

Point to other docs instead of explaining the same pattern again, for example
`[consistent hashing](../concepts/consistent-hashing.md)`.

## Capacity estimates

Every number needs a unit, and every result needs the arithmetic that produced it.

- State each assumption with its unit before you use it: `2M requests/day`, `800 input tokens per
  request`, `~$3.50/GPU-hour`.
- Show the math and keep the units on every line: `2M requests/day / 86,400 s/day ≈ 23 req/s`
  average, `~70 req/s` at a 3x peak.
- Work out the numbers that size the system: QPS, storage per day and per year, bandwidth, memory,
  GPU count, and cost.
- Round to one or two significant figures, and say when a figure is a ballpark rather than a quote.
- Reuse the numbers in [back-of-envelope-estimation](concepts/back-of-envelope-estimation.md) instead
  of copying them.

## Diagrams in Mermaid

Draw new diagrams in Mermaid, inside a fenced code block that starts with `` ```mermaid ``. GitHub renders
it, and it diffs like text. Older docs use ASCII diagrams; turning one into Mermaid is a welcome change.

- Every box must be a component the doc describes, with the same name it has in the text.
- Label an edge with what flows on it (requests, events, tokens, weights) when the boxes do not
  already make it clear. Each label adds height, so skip labels that only repeat the box names.
- Keep one diagram to one path, for example the request path or the fleet-management loop. Split it
  when it grows past about 12 boxes.
- Use the same shapes as the README example: `([Client])` for clients, `[("Store")]` for databases and
  caches, a `subgraph` for a group such as one replica, and dotted edges (`-.->`) for control paths.
- GitHub scales a diagram to the page width, so a long chain of boxes ends up very tall (top to
  bottom) or very small (left to right). A `subgraph` can set its own `direction` to fold the chain,
  but Mermaid ignores it when a node inside the subgraph links to a node outside it. Link to the
  subgraph itself instead, as the README example does.

The [LLM serving diagram](README.md#example-llm-inference-serving) in the README is a good model to copy.

## Ideas that would fit

- `ml-system-design/`: an LLM pretraining data pipeline (crawl, filter, dedup, tokenize); an LLM
  guardrails service on the serving path; a data labeling platform with human review and label quality
  checks; LLM observability (prompt and response logs, token cost, latency).
- `case-studies/`: collaborative document editing, like Google Docs; a distributed job scheduler; a
  metrics and alerting system.
- `concepts/`: consensus and leader election (Raft); idempotency and exactly-once processing.

## Pull requests

- One doc, or one fix, per pull request.
- Before you open it, check that every number has a unit and every box in a diagram is in the text.
- By contributing you agree that your work is released under the
  [Creative Commons Attribution 4.0 International license](LICENSE) (CC BY 4.0).
