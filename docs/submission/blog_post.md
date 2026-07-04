---
title: "Why your coding agent quits after an hour — and the foreman pattern that fixes it"
published: false
tags: ai, agents, qwen, hackathon
cover_image:
canonical_url:
---

Give a coding agent a 20-item requirements checklist — the kind of thing that
should take a solid day of focused engineering work — and ask it to do all
of it. Come back in an hour and it will tell you it's done.

It will have really finished about three of the twenty.

This isn't a complaint about one bad model or one bad prompt. It's a
measured, named failure mode, and once you know the numbers, you stop being
surprised by the demo videos that don't hold up under scrutiny.

## The failure science, briefly

Three separate lines of evidence point at the same wall:

- **METR's long-horizon evaluations** find that frontier models complete
  tasks requiring more than about four hours of equivalent human effort less
  than 10% of the time. The ceiling isn't "smarter model, higher ceiling" —
  it's structural.
- **Error compounding.** Toby Ord's simple half-life model: if a model is
  90% accurate on each step of a task, and the task needs 12 sequential
  steps, end-to-end correctness falls under 28%. Errors don't announce
  themselves — they get treated as established facts by every subsequent
  step.
- **Context rot.** Chroma's research across 18 frontier models (Qwen
  included) shows performance degrading *non-uniformly* as input context
  grows — not a clean linear decline, which makes it hard to predict when a
  given long session will start silently getting worse.

Stack those three on top of a fourth, more human problem — **early
termination**, i.e. the model's own overconfidence about what it finished —
and you get exactly the symptom every developer who has tried a long
autonomous agent run has seen: a confident "all done" that isn't.

Anthropic's own guidance for long-running agents is a tacit admission of all
of this: keep an external progress file, give the agent a completion
checklist it can flip but never rewrite, do one feature at a time, and kick
the work back when the agent claims done but hasn't actually verified it.
That's good advice. It's also just... a paragraph. Nothing stops the agent
from ignoring it eight hours in when its context is half rotted anyway.

We built **Foreman** to turn that paragraph of advice into an actual system,
for the Qwen Cloud Global AI Hackathon (Track 3: Agent Society).

## The architecture

```
requirements ─▶ Planner ─▶ [ Ledger ] ◀─ Dispatcher (pure code, zero LLM)
                              │                │ next task (CAS claim + TTL lease)
                              │                ▼
                              │           Executor  (clean context, qwen3-coder-plus)
                              │                │ submit + handoff
                              │                ▼
                              └──────────  Verifier (build/test/lint, then coverage)
                                               │ reject ─▶ Executor may DISPUTE ─▶ Arbiter
                                               │ pass   ─▶ done ─▶ unlock dependents
```

Five roles, each answering one specific measured failure mode above:

- **Planner** (`qwen-max`) turns the checklist into a task DAG. Every card
  gets acceptance criteria *and* a runnable, offline test command. This
  directly targets the long-horizon ceiling: instead of one enormous task, a
  planner produces many small, independently verifiable ones.
- **Dispatcher** is plain Python — no LLM calls at all. Dependency
  resolution, atomic compare-and-swap task claims, TTL-based lease recovery
  for crashed workers, a shared token bucket for the account-level rate
  limit. Scheduling correctness should not be probabilistic.
- **Executor** (`qwen3-coder-plus`, falling back to `qwen-plus`) does exactly
  one task, in a **fresh context** — it only ever sees its own task card and
  the structured handoffs from its dependencies. This is the direct fix for
  context rot: nothing rots if nothing accumulates.
- **Verifier** (`qwen-plus`, JSON mode) runs the real build/test/lint command
  *first*. A non-zero exit code is an automatic reject, full stop — no LLM
  opinion gets to override a failing test suite. Only after the objective
  gate is green does it score each acceptance criterion individually.
- **Arbiter** (`qwen-max`) hears disputes. If an executor believes a
  rejection was wrong, it gets one evidence-based appeal per task per run.
  The arbiter reads the *actual files* from the workspace — not the
  executor's summary of them — before ruling to overturn or uphold.

A durable **SQLite ledger** underlies all of it. Context windows rot; an
external ledger doesn't. It's also the only state a crashed-and-resumed run
needs — reviving blocked tasks and continuing is not a special code path,
it's just the loop continuing after a hiccup.

## Three incidents, live, not hypothetical

The interesting part of building this wasn't the happy path. It was three
things that actually broke during development and evaluation.

**Incident 1: the unwinnable exam.** Early on, a planner-generated task
carried a silently-defaulted empty `test_strategy` — the verifier had
nothing runnable to check it against, and no acceptance criteria to score
either. A task with no definition of "done" cannot be won by any executor,
no matter how good the code is. We hit this failure mode *twice*, live,
before fixing it. The fix mirrors the rest of the system's own philosophy:
the planner's output now goes through its own validation gate, and a plan
that produces an empty `test_strategy` or missing `acceptance_criteria` gets
rejected and re-asked with the specific error — the same reject-with-feedback
loop the executor already lives under, applied one level up, in
`Planner._parse_and_validate`.

**Incident 2: the evidence gap.** When we built the dispute/arbitration
layer, the first version had the arbiter rule based on the disputing
executor's *description* of its own files. That's not an independent check —
it's a rubber stamp with extra steps. The fix: `Arbiter.rule` now reads every
evidence file directly from the workspace (capped at 4000 characters per
file) before issuing a ruling. The arbiter outranks the argument by reading
the ground truth underneath it.

**Incident 3: the empty-arguments dialect.** This one was a genuine
DashScope/Qwen integration gotcha. Different Qwen model variants disagree on
how to serialize a zero-argument tool call: some emit `"{}"` for the
function's `arguments` field, others emit `""`. That's fine on the *way in*
— but once the executor's multi-turn tool loop echoes that assistant turn
back into the conversation history (as the OpenAI tool-calling protocol
requires), DashScope's endpoint rejects the echoed history outright:

```
openai.BadRequestError: Error code: 400 - {'error': {'code':
'invalid_parameter_error', 'param': None, 'message': 'The
"function.arguments" parameter of the code model must be in JSON format.',
'type': 'invalid_request_error'}}
```

This silently killed a multi-turn executor run mid-loop. The fix is a small
one-line normalization: patch any empty-string `arguments` to `"{}"` before
re-appending the assistant turn to history. Small fix, but the kind of thing
that costs you an entire evaluation run if you don't have logging that
surfaces the real exception (which, credit to it, `openai`'s Python client
does cleanly).

All three incidents were caught *because the system had something real to
fail against* — a test gate, or a live API error — not because we
anticipated them. That's the actual thesis of the whole project: verify
against reality, don't trust self-report, whether the thing self-reporting
is the coding agent or your own planner.

## The numbers

Same 5-item exam checklist, three conditions, single run each, real Qwen
models, an independent referee (not self-graded) running the actual pytest
suite:

| Condition | Referee-certified pass | Wall-clock | Executor attempts | Total tokens |
|---|---|---|---|---|
| A — single-agent one-shot | **0/5** | 136s | 1 | 75,253 |
| B — sequential feeding, no verification | 4/5 | 218s | 5 | 112,912 |
| C — Foreman (full loop, disputes enabled) | **5/5** | 461s | 6 | 267,193 |

Condition A is the number that matters most: the one-shot agent's *own*
tests passed. The independent referee's tests — checking the same
requirements — failed on all five items. That's quantified overconfidence,
not an anecdote about one bad run. Condition C includes one legitimate
rejection that got caught and healed mid-run (an executor attempt failed the
objective gate, retried, passed).

Foreman costs about 3.5x the tokens of one-shot for a 2.4x jump in
independently-certified completion. We also reran condition C with a
deliberately weaker executor model and still landed 5/5 — evidence pointing
at the orchestration, not raw model strength, as the thing compensating for
the underlying failure modes.

Every number above is a **single run**, not averaged over repeats. LLM
sampling variance means a rerun could shift these — we're saying that
explicitly rather than letting one lucky run pass as a trend.

## Building on Qwen Cloud — the practical gotchas

For anyone else building on Qwen through Alibaba Cloud's international
DashScope endpoint (OpenAI-compatible mode), a few things that cost us real
debugging time:

- **Per-model free quota is separate and can exhaust independently.** We hit
  this directly: `qwen3-coder-plus`'s free bucket ran out mid-evaluation
  (`openai.PermissionDeniedError: ... "The free quota has been exhausted"`),
  while `qwen-plus`'s free quota was untouched. If your executor and
  verifier use different models, don't assume they share a budget — check
  the console per model, and have a same-tier fallback model name ready to
  swap in via an environment variable rather than a code change.
- **JSON mode's `"json"` keyword quirk.** DashScope's JSON-mode enforcement
  is picky about the literal presence of the word "json" appearing
  somewhere in your prompt — omit it and you can get inconsistent or
  rejected structured-output requests depending on the model. Bake the word
  into your system prompt template, not just your intent.
- **The empty-arguments dialect between Qwen variants**, described above —
  normalize `function.arguments` to `"{}"` before ever echoing an assistant
  tool-call turn back into conversation history. This is the single
  highest-value defensive line if you're building any multi-turn
  tool-calling loop against DashScope.
- **Route model tier to role, not role to whatever's cheapest.** We put
  `qwen-max` on both the planner and the arbiter deliberately — the arbiter
  needs to outrank both the disputing executor and the verifier being
  disputed, and that only works if it's actually a stronger model, not just
  a differently-prompted one.

## Honest limitations

- All evaluation numbers are single-run. We have not yet done a proper
  multi-repeat statistical comparison, and we say so in the results
  themselves rather than dress up one data point as a trend.
- The dispute/arbitration loop is currently one appeal per task per run —
  bounded on purpose to avoid infinite litigation, but that also means a
  genuinely ambiguous acceptance criterion only gets one shot at
  clarification.
- The system optimizes **execution fidelity** — did the work actually get
  finished and verified — not code-generation quality. That's a deliberate
  scope cut, not an oversight: it's a different problem than "one prompt →
  a whole app" frameworks like MetaGPT or ChatDev are solving, and we didn't
  want to blur the comparison by trying to be both.
- Foreman costs meaningfully more tokens than a one-shot attempt. We think
  that's the correct trade for work that's independently verified rather
  than merely claimed, but it is a real cost, not a rounding error, and
  we're reporting it as such rather than burying it.

The whole point of this project was to stop trusting an agent's word for
whether something is done, and start trusting a build log, a test suite, and
an independent arbiter reading the actual files. That constraint made the
system slower and more expensive than just asking nicely — and also the only
one of the three conditions we tested that an independent referee actually
certified as complete.
