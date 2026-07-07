# Foreman

**Coding agents don't fail at hard problems — they fail at long lists. Give one a 20-item checklist and it will confidently declare victory after finishing three.**

That's the failure mode Foreman exists to kill. Everyone who has pointed an autonomous agent at a real backlog has seen it: the agent works for an hour, sounds completely sure of itself, and stops well short of the finish line, and nothing in the loop forces it to notice. Foreman is a foreman for coding agents: it plans the checklist into small verifiable tasks, hands each one to an executor with a clean context, and refuses to mark anything "done" until an independent verifier proves it with a real test run — not an LLM's opinion of its own work.

## What Foreman does

Foreman takes a requirements checklist (5 items, 20 items, whatever the backlog actually is) and drives it to completion the way a human tech lead would run a team: decompose the work, assign it, check output against acceptance criteria fixed before anyone started coding, and handle it when someone disagrees with a rejection. It is not another "generate an app from one prompt" tool — MetaGPT and ChatDev already do that well. Foreman targets the step after: making sure the 17th requirement on a long list actually got implemented and proven, not just claimed.

## How it works — the loop

```
requirements ─▶ Planner ─▶ [ Ledger ] ◀─ Dispatcher (pure code, zero LLM)
                              │                │ next task (CAS claim + TTL lease)
                              │                ▼
                              │           Executor  (clean context per task)
                              │                │ submit + structured handoff
                              │                ▼
                              └──────────  Verifier (objective gates first, then LLM scoring)
                                               │
                                        reject ─┼─▶ Executor may DISPUTE ─▶ Arbiter rules
                                               │
                                        pass ──┴─▶ DONE ─▶ unlock dependent tasks
```

The **Planner** turns requirements into a dependency-ordered task DAG; every task carries a runnable `test_strategy`, not just prose acceptance criteria. The **Dispatcher** is plain Python, zero LLM calls: atomic compare-and-swap claims so two workers can never grab the same task, plus a TTL lease so a crashed worker's task gets reclaimed instead of rotting `IN_PROGRESS` forever. Each **Executor** call gets a clean context, no accumulated conversation cruft, and works through an OpenAI-style tool-calling loop (`read_file`, `write_file`, `run_command`, `done`, etc.). The **Verifier** runs the task's own test plus a full `pytest -q` regression sweep *before* it ever asks an LLM for an opinion — real build/test signal gates the review, not the other way around. Everything lands in a durable SQLite (WAL-mode) ledger: an append-only `attempts` audit trail plus current `tasks` state, which is what lets `--resume run_xxx` reopen a run with no re-planning and what drives the status-wall UI.

## Track 3 evidence — division of labor, negotiation, measured gain

**Division of labor** is mechanical, not vibes: the Planner produces the DAG once; the Dispatcher's atomic CAS claims and TTL-lease crash recovery assign work to executors race-free, in pure code, with zero LLM involved in the assignment itself.

**Negotiation** is a real reject → dispute → arbitration pipeline, not a rubber-stamp loop. A rejection is only disputable when every objective gate was green (gate failures are machine-checked ground truth — no arguing with a failing test). The executor gets exactly one evidence-based appeal per task per run. The Arbiter runs on `qwen-max`, deliberately the same tier as the Planner and ranked above both the disputing executor and the verifier it's overruling, and rules `overturn` or `uphold` by reading the actual evidence files, not the conversation. Overturn passes the task; uphold folds the arbiter's clarification into the rejection reason for the next attempt. Three real incidents from live runs, all traceable in committed ledgers: a planner-emitted invalid gate the system refused to rubber-stamp (it burned its retry budget and blocked instead of guessing), an executor test that asserted the wrong error message and got corrected by name on retry, and a test-isolation bug (stale rows leaking into `/expenses`) caught and healed by the gate.

**Measured gain — the A/B/C table**, same frozen checklist (`demo/requirements_mini.md`, 5 tasks), same executor model, one independent referee that never touches Foreman's own verifier verdicts (`evals/results_20260704T080344Z.json`):

| Condition | Referee pass | Wall-clock | Attempts | Tokens |
|---|---|---|---|---|
| A — single-agent one-shot | 0/5 | 136s | 1 | 75,253 |
| B — sequential, no verification | 4/5 | 218s | 5 | 112,912 |
| C — full Foreman | 5/5 | 461s | 6 | 267,193 |

Full verification costs roughly **2.4x** the tokens of sequential-no-verify and **3.5x** the one-shot baseline, in exchange for going from 0–4 out of 5 referee-passed to 5/5. That's an honest tradeoff, not a free lunch, and the caveats matter: single run, not an average; Condition A's 0/5 is partly a test-filename mismatch a later fix addresses; and Condition C's executor was `qwen-plus` (not the strongest available tier) and still hit 5/5 — evidence the orchestration loop is doing real work, not just a stronger model.

## What's actually novel

MetaGPT and ChatDev optimize code-generation quality from a single prompt: good SOP roleplay, one shot at a whole app. Foreman optimizes a different axis — execution fidelity across a long checklist, on the assumption that most individual tasks are fine on a first draft but the *proof* that all N got done is what breaks. That means a verification loop instead of a single review pass, real negotiation when a verdict is disputed, and a durable ledger instead of conversation memory as the source of truth. It isn't a fork of either project; the task-DAG-plus-gate design was built against documented failure modes (METR's long-horizon ceiling, Toby Ord's error-compounding argument, Chroma's context-rot findings, ADaPT's decomposition results, CRITIC's finding that self-review without tool feedback is unreliable, Reflexion's retry-diminishing-returns, AutoGPT's runaway-retry incident, Cognition's writeup on multi-agent write conflicts).

## Try it in 60 seconds

No API key needed: check "Demo mode" in the web console (or pass `--mock`) and the entire product runs offline against a scripted fake planner/executor/verifier — parallel runs, dispute/arbitration, stop/resume, per-run cost accounting, all of it, finishing in under a second (slow it for filming with `FOREMAN_MOCK_DELAY`). For a real run with no coupon and no payment: DashScope's **free per-model quota** is enough. Confirmed working on free tier today: `qwen-max` (planner/verifier/arbiter) and `qwen3-coder-flash` (executor), with `qwen-turbo`/`qwen3-max` as fallbacks. The whole demo is reproducible by any judge, for free, with no coupon required.

## Tech stack

Qwen models via DashScope's OpenAI-compatible endpoint (no vendor SDK beyond `openai`), with per-role model routing overridable through environment variables so the DashScope catalog can drift without a code change. Deployment target is Alibaba Cloud Function Compute, free tier, custom runtime, Singapore region (`serve.py` behind an `fcapp.run` URL; a self-contained zip with vendored `openai` is already built). 231 tests pass with zero API key required, covering concurrency safety, the retry ladder, crash recovery, dispute/arbitration, resume, and the web API.

## Honest limitations

The objective gate is pytest-oriented today; `npm test`/`go test` support is roadmapped behind a pluggable gate-runner interface, not yet built. The ledger is single-node SQLite by design (a local double-click tool doesn't need distributed infra), sitting behind one interface so a Postgres/etcd swap is a backend change, not an orchestrator rewrite. The A/B/C evaluation is demo-scale — 5 tasks, one run, not an average — with the 20-item full checklist planned but pending a live quota-unblocked run; we're publishing the 5-item result now rather than waiting for a bigger number. And the FC deployment steps are written but not yet executed end-to-end against a live instance. We'd rather list these than paper over them — the evidence spine behind this submission tracks all of them as open items, not talking points to bury.
