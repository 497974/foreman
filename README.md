# Foreman — an AI foreman that makes coding agents actually finish long tasks

> Give a coding agent a 20-item requirements checklist that should take a full
> day, and it will declare "all done" after an hour — having really finished
> three of them. Feed the *same* list one item at a time, and each gets done
> properly. **Foreman automates the second workflow**: it plans the list into
> verifiable tasks, hands each one to an executor with a clean context, and
> refuses to call anything done until an independent verifier proves it.

Built for the **Qwen Cloud Global AI Hackathon — Track 3: Agent Society**.
Foreman is a multi-agent system whose agents divide labor, negotiate over
rejected work, and resolve execution conflicts, with a **measurable completion-rate
gain over a single-agent baseline** on the same checklist.

Foreman optimizes **execution fidelity** — whether the work actually gets
finished — not code-generation quality. That is the deliberate difference from
"one prompt → a whole app" frameworks like MetaGPT or ChatDev.

---

## Why long tasks fail (and what Foreman does about it)

Every design choice below answers a *measured* failure mode, not a hunch:

| Failure mode | Evidence | Foreman's mechanism |
|---|---|---|
| Long-horizon ceiling | METR: frontier models finish tasks needing >4h of human work <10% of the time | Planner splits the list into short, independently verifiable tasks |
| Error compounding | 90% per-step accuracy over 12 steps → <28% end-to-end (Toby Ord's half-life model) | Each task is verified independently; errors don't propagate as "facts" |
| Context rot | Chroma: 18 frontier models (incl. Qwen) degrade non-uniformly as input grows | Every executor works in a **clean context** — only its task card + upstream handoffs |
| Premature completion | "early termination (overconfidence)" is a named, measured failure | An **independent verifier** rules; the executor cannot flip its own done-flag |
| Instruction overload | Multi-task prompts drop format compliance 2–21%; one-shot planning loses to as-needed decomposition (ADaPT: 17% vs 44% on WebShop) | One task card is dispatched at a time |
| Unreliable self-review | CRITIC: without external tool feedback, LLM self-correction is unreliable | Verifier runs real build/test/lint **before** any LLM judgement |
| Runaway retries | Reflexion gains <2% after the 3rd fix; AutoGPT once burned 300+ API calls to zero output | 3-attempt ceiling → escalation ladder, plus a consecutive-failure circuit breaker |

This mirrors — and productizes — Anthropic's own long-running-agent guidance
(external progress file, a completion checklist the agent may flip but not
rewrite, one feature at a time, "kick it back when it claims done").

## Architecture

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

- **Planner** (`qwen-max`) — turns the checklist into a task DAG. Every card
  carries acceptance criteria and a *runnable* verification command; complexity
  ≥5 gets split further.
- **Dispatcher** (pure Python) — deterministic scheduling: dependency
  resolution, atomic compare-and-swap claims, TTL-lease crash recovery, a shared
  token bucket for the account-level rate limit.
- **Executor** (`qwen3-coder-plus`) — does one task in a fresh context, then
  leaves a structured handoff (what changed, contracts, gotchas, next steps).
- **Verifier** (`qwen-plus`, JSON mode) — objective gate first (real test/lint),
  then three-tier scoring of each acceptance criterion.
- **Arbiter / Replanner** (`qwen-max`) — settle disputes and, after the retry
  ceiling, split a task finer or roll back to an upstream design flaw.

The **Ledger** (SQLite locally, OSS on Alibaba Cloud) is the durable spine:
context windows rot, an external ledger doesn't. It is also the single source of
truth the status-wall UI renders and the evaluation harness reads.

## Quickstart

The deterministic orchestration core runs with **no API key and no third-party
installs** — watch the whole loop drive a mock executor/verifier:

```bash
python demo/smoke_run.py
```

```
  [#] [#] [#] [ ] [ ] [.]   T4 REJECT (attempt 1)
  [#] [#] [#] [ ] [ ] [.]   T4 REJECT (attempt 2)
  [#] [#] [#] [#] [ ] [.]   T4 PASS
  ...
  done=6/6  blocked=0  steps=8
  T4 took 3 attempts (rejected twice, then passed) - the retry ladder at work.
```

Run the tests (concurrency safety, retry ladder, crash recovery):

```bash
pip install pytest && python -m pytest -q
```

Wiring the LLM agents (Planner / Executor / Verifier) to Qwen via Alibaba Cloud
DashScope is next — see [`docs/`](docs) once it lands.

## Status

- [x] Deterministic core: models, state machine, ledger, dispatcher (15 tests green)
- [x] Concurrency-safe claims (compare-and-swap) + TTL crash recovery
- [ ] Planner / Executor / Verifier on Qwen (DashScope)
- [ ] Dispute → arbitration negotiation loop
- [ ] Web status wall + Alibaba Cloud Function Compute deployment
- [ ] Three-baseline efficiency evaluation

## License

Apache-2.0 — see [LICENSE](LICENSE).
