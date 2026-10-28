# LLM Agent Orchestration System

## Requirements

### Functional
- Execute multi-step tasks by interleaving LLM reasoning calls with tool/function calls (search,
  code execution, internal APIs, sub-agent delegation), continuing until the task is complete or
  a stopping condition is reached.
- Split planning from execution: a planning step decomposes a task into a sequence of sub-steps,
  and an executor carries out each step — with the ability to replan when a step fails or returns
  a result the original plan didn't anticipate, rather than blindly executing a fixed plan.
- Maintain memory across a task's execution (working memory, scoped to one run) and across
  sessions for a given user or agent (long-term memory), so context beyond the model's fixed
  context window survives between calls and between tasks.
- Support concurrent tool calls within a step when sub-steps are independent (parallel fan-out to
  multiple tools or sub-agents), rather than always serializing execution one call at a time.

### Non-functional
- Cost per task must be bounded, not just monitored: the agent loop is multiplicative by
  construction (every step is an LLM call, every retry is another), so a misbehaving loop must be
  structurally prevented from burning unbounded tokens — hard step/token/wall-clock budgets
  enforced by the orchestrator, not a dashboard alert that fires after the damage is done.
- Latency: interactive tasks must stream intermediate progress, since end-to-end completion can
  take seconds to minutes — a blocking "wait for the whole task" UX is not viable once a task
  involves more than one or two steps.
- Tool-call safety: every model-generated tool call is untrusted input to whatever the tool
  touches — schema validation and sandboxed execution are required on every call, not only for
  tools deemed "risky" at design time.
- Partial-failure tolerance: one failed tool call or one failed sub-agent must not fail the whole
  task outright — the orchestrator needs a defined retry/replan/degrade policy per failure class,
  distinct from a generic "task failed" outcome.

## Capacity Estimation

Assume 500K agent tasks/day, averaging 8 LLM calls/task (an initial plan, several step
executions, periodic reflection/replan checks). Average prompt size grows with accumulated
context — call it 3K input tokens / 500 output tokens per call as a task-level average.

Total LLM calls/day: 500K × 8 = **4M calls/day** → 4M / 86,400 ≈ 46/sec average, ~140/sec peak
(3x). Token volume: input 4M × 3K = 12B tokens/day, output 4M × 500 = 2B tokens/day → **14B
tokens/day** total (see [llm-inference-serving.md](llm-inference-serving.md) for what serving this volume costs downstream).

Tool calls: assume 60% of steps invoke a tool rather than reasoning purely in-context → 4M × 0.6
≈ 2.4M tool calls/day → 2.4M / 86,400 ≈ 28/sec average, ~83/sec peak.

**Task wall-clock and concurrency.** A fully serial 8-step task at ~3s/step (LLM call latency
plus tool latency) would take ~24s. With parallel fan-out on the ~30% of steps that have
independent sub-work (see deep dive), realistic median wall-clock lands around 15-18s/task, with
a long tail from retries and replanning pushing p99 into the 60-90s range. At 46 tasks/sec
initiated on average and an 18s median duration, steady-state **in-flight tasks ≈ 46 × 18 ≈
830**; at peak (140/sec initiated, longer average duration from retry-heavy load) that climbs to
roughly **3,500 concurrent in-flight tasks** — this is the number that sizes the orchestrator's
state store and worker pool, not raw call-per-second throughput, since each in-flight task holds
live state for the duration of its run.

**Long-term memory store.** Assume 10M distinct agent/user identities, each with a rolling window
of recent interactions summarized into roughly 2KB of retrievable memory: 10M × 2KB = 20 GB,
embedded and indexed for retrieval (see [vector-search-system.md](vector-search-system.md) for the ANN mechanics this reuses)
— small relative to the LLM call volume above, but on the query hot path for any task that pulls
long-term context.

## API Design

```
POST /v1/agents/{agent_id}/tasks
     { input, tools_allowed: [...],
       budget: {max_steps, max_tokens, max_wall_clock_s} }
     -> 202 { task_id, status: "running" }

GET  /v1/tasks/{task_id}/stream          (SSE)
     -> one event per step:
        { step_index, type: plan | tool_call | tool_result | reflection | final,
          content, tokens_used }

POST /v1/tasks/{task_id}/cancel
     -> 200

GET  /v1/tasks/{task_id}
     -> { status, steps: [...], total_tokens, total_cost_usd, result }

POST /v1/tools/register
     { tool_name, json_schema, endpoint, timeout_ms, sandbox_policy }
     -> 201
```

`budget` on task creation is not optional metadata — it's the parameter the orchestrator enforces
against on every step boundary. A task submitted without an explicit budget gets the platform
default applied, never an unbounded one; there is no code path that starts a task with no ceiling
on steps, tokens, or wall-clock.

## Data Model

```
task (one per invocation)
  task_id, agent_id, input, status: planning|executing|waiting_tool|done|failed|cancelled,
  budget: {max_steps, max_tokens, max_wall_clock_s}, tokens_used, cost_usd, created_at

step_log (append-only, one row per step within a task)
  task_id, step_index, type: plan|tool_call|tool_result|reflection|final,
  llm_call_ref              (pointer to prompt/completion, not inlined — see cost note below)
  tool_name?, tool_input?, tool_output?, latency_ms, status

tool_registry
  tool_name, json_schema, endpoint, timeout_ms,
  sandbox_policy            (network/filesystem scope granted to this tool's execution)
  owning_team

memory_store (per agent_id, two tiers with different lifetimes)
  working_memory   (ephemeral, scoped to one task_id, evicted on task completion)
  long_term_memory (vector-indexed, keyed by agent_id, summarized past interactions,
                     see vector-search-system.md)
```

`step_log.llm_call_ref` points at the actual prompt/completion pair rather than inlining it,
because prompts grow with accumulated context (a step 6 prompt includes everything steps 1-5
produced) — inlining full prompt text into every step row would multiply storage roughly
quadratically with task length instead of linearly.

## High-Level Architecture

```
 client ──POST /tasks──► Orchestrator (task state machine)
                               │
                               ▼
                       Planner (LLM call): decomposes input into an
                       initial step plan, constrained by tools_allowed
                       and budget
                               │
                               ▼
              ┌──────── Executor loop (per step) ─────────────┐
              │                                                │
              │  independent steps in this plan node ─fan-out─►│
              │       │              │              │          │
              │       ▼              ▼              ▼          │
              │   Tool executor  Tool executor  Sub-agent       │
              │   (sandboxed)    (sandboxed)    (recursive      │
              │       │              │           orchestrator   │
              │       │              │           invocation)    │
              │       └──────────────┴──────┬───────┘           │
              │                             ▼                   │
              │                 results appended to             │
              │                 working_memory                  │
              │                             │                    │
              │                             ▼                    │
              │        Reflection/replan (LLM call): continue    │
              │        plan | replan | done | fail                │
              └────────────────────┬───────────────────────────┘
                                    │
                    budget enforcer checks steps/tokens/wall-clock
                    after every step ── over budget? ──► stop,
                    return partial result + reason
                                    │
                                    ▼
                    Memory writer: summarizes completed task into
                    long_term_memory (async, off the critical path)
                                    │
                                    ▼
                    step events streamed to client throughout (SSE)
```

## Deep Dives

**Planner/executor loop and replanning.** Two control-flow shapes dominate in practice. Plan-then-
execute produces the full step sequence up front from one planning call, then executes it — cheap
(one planning LLM call) and easy to reason about, but brittle: if step 3's tool result invalidates
the assumption behind step 5, a plan-then-execute loop has no built-in mechanism to notice until
step 5 actually fails. ReAct-style interleaving (reason, act, observe, repeat) instead makes a
planning decision after every single step's result — much more adaptive to unexpected tool
output, at the cost of one extra LLM call per step. Most production systems land on a hybrid: an
initial coarse plan from one planning call, with a lightweight reflection check after each step
that only triggers a full replan (another planning-shaped call) when the step's result actually
diverges from what the plan expected — full ReAct-style replanning on every step is rarely worth
its token cost when most steps do go as planned. Bounding replan attempts per task (not just total
steps) is required specifically to prevent a pathological case: a step that keeps failing in a way
that triggers a replan, whose new plan hits the same failure, indefinitely, is a structurally
different runaway-cost pattern than a long-but-progressing task and needs its own cap.

**Tool calling: validation and sandboxing.** A model-generated tool call is a JSON object the
model produced from a natural-language reasoning process — it is not guaranteed to conform to the
tool's schema (wrong types, missing required fields, values outside a valid range), and treating
it as trusted input the instant it parses as JSON is the most common source of tool-execution
incidents. Every tool call is validated against `tool_registry.json_schema` before dispatch, and
rejected back to the model as a correctable error (not silently retried against the tool) if it
fails validation — letting the model see its own validation failure and correct it is usually
more reliable than the orchestrator guessing at a fix. Execution itself runs under
`sandbox_policy`: network and filesystem scope explicitly granted per tool, not inherited from the
orchestrator process, so a compromised or misbehaving tool call is contained to exactly the blast
radius that tool was provisioned for. Tools with side effects (sending a message, writing a
record, charging a payment) additionally need an idempotency key derived from the task/step
identity — at-least-once tool-call delivery is the default under retry, and without idempotency
the retry itself becomes the bug (the email gets sent twice, not zero or one times).

**Memory architecture.** Working memory is whatever the current task's context window actually
holds — the running transcript of steps, tool results, and reasoning for this one run, evicted the
moment the task completes. Long-term memory is retrieved on demand, not held in context by
default: a vector-indexed store of summarized past interactions for this agent/user, queried the
same way a RAG system queries a document corpus (see [rag-system.md](rag-system.md) for the retrieval mechanics),
pulled into the prompt only for the steps that need cross-session context. The two need different
handling as a task grows long: a task accumulating verbose tool outputs (a large search result, a
long log dump) faster than the fixed context window can hold verbatim requires active compaction —
summarizing or truncating older working-memory entries before they're evicted outright, so the
model retains the gist of early steps without paying their full token cost on every subsequent
call. Skipping this compaction step is the most common cause of a long-running agent task quietly
degrading in quality mid-run: not from a bug, but from the model's own early context silently
falling out of the window as later tool outputs push it out.

**Concurrency and partial failure in fan-out.** Independent steps (two searches that don't depend
on each other's results, or delegating to two sub-agents on unrelated sub-problems) are dispatched
concurrently rather than serialized — this is the main lever on task wall-clock, since LLM call
and tool call latency otherwise stack linearly with step count. The harder design question is join
semantics: when one branch of a fan-out fails and the others succeed, the orchestrator has to
decide whether the step as a whole fails (safe default, but discards useful partial results) or
degrades gracefully by passing the successful branches' results forward with the failure noted
(riskier, since the next reflection step has to reason correctly over an incomplete result set).
This is structurally the same scatter-gather problem as any distributed service call, except the
"gather" step here is itself an LLM call, which means a badly-designed partial-result format can
cause the model to reason incorrectly about what actually happened rather than just receiving
malformed data — the failure mode is a wrong plan, not a parse error.

**Cost and latency budget enforcement.** Budgets are checked by the orchestrator after every step
boundary, not derived after the fact from logs — a task that would exceed `max_steps`,
`max_tokens`, or `max_wall_clock_s` on its next step is stopped there, returning whatever partial
result exists plus an explicit budget-exhaustion reason, rather than either silently truncating
output or letting the loop continue past its ceiling. This matters most for exactly the failure
mode planning and replanning bounds exist to catch but don't always: a tool that returns
technically-valid but unhelpful results the model keeps reacting to and re-querying can pass every
per-step check individually while still being, in aggregate, a cost runaway — the budget is the
backstop that catches this class regardless of which specific step-level safeguard it slipped
past.

## Bottlenecks & Scaling

LLM call latency dominates per-step latency and is therefore the primary lever on task wall-clock —
concurrent fan-out of independent steps is the main throughput tool available, since reducing
individual call latency further runs into the same serving constraints covered in
[llm-inference-serving.md](llm-inference-serving.md). The reflection/replan step recurs on every step and its cost multiplies
directly with retries, which is why bounding replan attempts is a cost control as much as a
correctness one — an unbounded replan loop is functionally a cost-runaway bug even when every
individual replan call is cheap. Tool executor capacity needs to scale independently from the
orchestrator itself, since tool workloads are wildly heterogeneous (a code-execution tool has a
completely different resource profile — CPU, memory, execution timeout — than a search-API call),
and provisioning them as one shared pool either starves the expensive tools or over-provisions for
the cheap ones. Long-term memory retrieval adds a full RAG-shaped latency and cost tax (embedding
the query, ANN search, reranking) to any task step that uses it, which is why it's typically an
opt-in per task or per step rather than an unconditional part of every prompt assembly — most
steps in most tasks don't need cross-session context, and paying that tax unconditionally would
tax the majority of steps that never benefit from it.
