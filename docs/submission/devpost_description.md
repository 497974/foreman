# Foreman — Devpost Submission

**Track: Track 3 — Agent Society**

## Inspiration

Give a coding agent a 20-item requirements checklist that should take a full
day of work, and it will declare "all done" after an hour — having really
finished about three of the twenty. This isn't a one-off anecdote. It's a
named, measured failure mode: METR finds frontier models complete tasks
requiring more than four hours of human effort less than 10% of the time.
Toby Ord's error-compounding model shows 90%-per-step accuracy collapsing to
under 28% correct after just 12 sequential steps. Anthropic's own guidance
for long-running agents recommends an external progress file and a
completion checklist the agent can flip but not rewrite — because agents
cannot be trusted to grade their own homework.

We wanted to see what happens if you take that guidance and turn it into an
actual system, instead of a paragraph of advice a single agent is supposed
to remember to follow for eight hours straight.

## What it does

Foreman is a small society of specialized agents that turns a requirements
checklist into *verified*, not merely *claimed*, completion:

1. **Planner** (`qwen-max`) splits the checklist into task cards, each with
   acceptance criteria and a runnable, offline test command. Anything too
   complex (complexity ≥ 5) gets split further.
2. **Dispatcher** (pure Python, zero LLM calls) resolves dependencies,
   claims tasks atomically (compare-and-swap), leases them with a TTL for
   crash recovery, and rate-limits the whole account through a shared token
   bucket.
3. **Executor** (`qwen3-coder-plus` / `qwen-plus`) does exactly one task in a
   **clean context** — it only ever sees its own task card plus the
   structured handoffs of its dependencies, never the whole project's
   history. This is the direct fix for context rot.
4. **Verifier** (`qwen-plus`, JSON mode) runs the *real* build/test/lint
   command first — a deterministic gate, not an LLM's opinion — and only
   then scores each acceptance criterion. A single failing gate is an
   automatic reject regardless of what the LLM thinks of the code.
5. **Arbiter** (`qwen-max`) hears disputes: if an executor believes a
   rejection was wrong, it can appeal with file-level evidence, once, per
   task, per run. The arbiter reads the actual files (not the executor's
   description of them) and rules to overturn or uphold.
6. A durable **ledger** (SQLite) is the single source of truth for
   everything above — a crashed run resumes by reviving blocked tasks, no
   re-planning, no lost state.
7. A **local web console** (`serve.py`, stdlib-only `http.server`) renders
   the ledger live: a four-color status wall, a click-through drawer with
   verdict history, and an event feed that surfaces `DISPUTE` and
   `ARBITRATION` as visible orange badges — because in a multi-agent system,
   negotiation should be something you can *watch happen*, not an internal
   implementation detail.

## How we built it

Everything runs on **Qwen models via Alibaba Cloud's international DashScope
endpoint**, using the OpenAI-compatible client (`openai` Python SDK pointed
at `dashscope-intl`). Per-role model routing:

| Role | Model | Why |
|---|---|---|
| Planner | `qwen-max` | Needs the most reasoning depth: decomposing a checklist into a valid, acyclic task DAG with runnable tests is a planning problem, not a coding one. |
| Executor | `qwen3-coder-plus` or `qwen-plus` (env-switchable per role) | Code-specialized model for the tool-calling implementation loop. Our evaluation intentionally ran the *weaker* `qwen-plus` executor and still reached 5/5 — the orchestration compensates for model strength. |
| Verifier | `qwen-plus`, JSON mode | Objective gates (real pytest/build) run first and are final on failure; the LLM only scores criteria once the gate is green, so verifier model strength matters less than gate correctness. |
| Arbiter | `qwen-max` | Same tier as the planner on purpose — it must outrank both the executor that disputes and the verifier being disputed against. |

The orchestration core (ledger, dispatcher, state machine) is **deliberately
zero-LLM and stdlib-only** — deterministic scheduling shouldn't need a
framework, and it means the parts of the system that must never be wrong
(claiming, leasing, dependency resolution) are also the parts that are fully
unit-testable without any API key.

## Challenges we ran into

Three real incidents, hit live during development and evaluation — not
hypotheticals:

**1. The unwinnable gate.** Early in evaluation, the planner produced a task
whose `test_strategy` field silently defaulted to an empty string, meaning
the verifier's objective gate had nothing to run and no acceptance criteria
to check against. A task with no definition of "done" is unverifiable by
construction — the executor could never win, no matter how good the
implementation was. Fix: the planner's own output now goes through a
validation gate (`Planner._parse_and_validate`), and a plan that produces a
task with an empty `test_strategy` or missing `acceptance_criteria` is
rejected and re-asked with the specific error — the same reject-with-feedback
loop the executor already lives under, now applied one level up.

**2. The evidence gap.** During the dispute/arbitration rollout, we needed
the arbiter to rule on *actual* file contents, not the disputing executor's
paraphrase of them — an arbiter that trusts a description is not an
independent check, it's a rubber stamp. `Arbiter.rule` reads every
evidence file straight from the workspace (capped at 4000 chars) before
issuing a ruling, closing the gap between "here's what I claim I did" and
"here's what's actually on disk."

**3. The empty-arguments dialect.** Different Qwen model variants disagree
on how to represent a zero-argument tool call: some emit `"{}"`, others emit
`""` for `arguments`. DashScope's endpoint rejects an echoed conversation
history containing an empty-string `function.arguments` with a 400
`invalid_parameter_error` ("must be in JSON format") — which silently broke
the executor's multi-turn tool loop mid-run. Fix: the executor now
normalizes empty arguments to `"{}"` before echoing any assistant turn back
into history.

All three were caught because the system either had a real test gate to fail
against, or a live API error to surface — not because we anticipated them in
advance. That's the whole thesis: verify against reality, don't trust the
agent's self-report.

## Accomplishments that we're proud of

On a 5-item exam checklist, single run, real Qwen models, independent
referee (not self-graded):

| Condition | Referee-certified pass | Wall-clock | Executor attempts | Tokens |
|---|---|---|---|---|
| A — single-agent one-shot | **0/5** (its own tests passed; the referee's did not) | 136s | 1 | 75,253 |
| B — sequential feeding, no verification | 4/5 | 218s | 5 | 112,912 |
| C — Foreman (full loop) | **5/5**, including one legitimate rejection caught and healed mid-run | 461s | 6 | 267,193 |

Condition A is the headline finding: the one-shot agent's *own* tests passed
while the independent referee's tests failed on all five items — quantified
overconfidence, not a vibe. Foreman is the only condition an independent
referee actually certifies as complete, and it costs roughly 3.5x one-shot's
tokens for a 2.4x completion-rate jump. We also reran Condition C with a
deliberately weaker executor model and still reached 5/5 — evidence that the
orchestration, not raw model strength, is what's compensating for the
underlying failure modes.

All numbers above are single-run measurements, not averaged over repeats —
we say so explicitly rather than let a lucky run pass as a trend.

## What we learned

- Verification has to be **structurally prior** to LLM judgement, not just
  advisory — the moment a gate can be argued around, it stops being a gate.
- A planner is also a source of bugs, not just the executor — the
  "unwinnable gate" incident happened one layer above where we were
  originally watching.
- Giving the weaker party in a dispute (the executor) a bounded, evidence-
  based appeal — rather than either blind trust or blind distrust of the
  verifier — is what makes the negotiation loop feel like arbitration
  instead of either rubber-stamping or stonewalling.
- Cross-model-family quirks (JSON-mode string literals, empty-argument
  conventions) are exactly the kind of thing that silently corrupts a long
  agent loop hours in if you don't have a durable ledger to resume from.

## What's next

- Multi-repeat statistical evaluation (the current numbers are honestly
  single-run) across more and larger checklists.
- Alibaba Cloud Function Compute deployment of the dispatcher/verifier loop
  for durable, always-on orchestration instead of a local process.
- A richer negotiation surface: today the dispute is one-shot per task;
  a bounded multi-round negotiation (still with a hard ceiling) is a natural
  next step for genuinely ambiguous acceptance criteria.
- Expanding role diversity (frontend/backend/data/infra) already tagged on
  task cards into actually-routed model/tool specialization per role.
