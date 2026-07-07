# Foreman — Evidence Spine

Source-of-truth facts for all submission documents. Compact and factual; not
prose meant to be read standalone by judges. Every downstream doc (pitch,
demo script, technical writeup, etc.) should draw its claims from here rather
than re-deriving them.

## Fixed facts

- **Project:** Foreman, at `C:\Users\24973\Desktop\Foreman`.
- **Repo:** PUBLIC, Apache-2.0 license, at https://github.com/497974/foreman
- **Track:** 3 (Agent Society), Qwen Cloud Global AI Hackathon.

## (1) What Foreman is / the pain it solves

Foreman is an AI foreman that makes coding agents actually finish long
requirements checklists. The pain it solves: give a coding agent a 20-item
checklist that should take a full day, and after an hour it declares "all
done" — having actually finished three of them. Foreman automates the
alternative workflow instead: plan the checklist into small verifiable tasks,
hand each one to an executor with a clean context, and refuse to call
anything done until an independent verifier proves it.

## (2) How the loop works

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

- **Planner** (`qwen-max`): requirements → dependency-ordered task DAG; every
  task carries acceptance criteria plus a runnable `test_strategy`.
- **Dispatcher** (pure Python, zero LLM): dependency resolution, atomic
  compare-and-swap (CAS) claims, TTL-lease crash recovery, shared
  account-level rate limiter. Two workers can never hold the same task; a
  crashed worker's task is automatically reclaimed via the TTL lease instead
  of staying stuck `IN_PROGRESS` forever.
- **Executor** (`qwen3-coder-plus`/`qwen-plus` in the original design;
  `qwen3-coder-flash` confirmed free-tier — see below): one task per clean
  context via an OpenAI tool-calling loop. Tools: `read_file`, `write_file`,
  `list_dir`, `search_files`, `run_command`, `done`.
- **Verifier** (`qwen-plus`/`qwen-max`, JSON mode): objective gates FIRST —
  the task's own test plus a `pytest -q` regression sweep — then three-tier
  LLM scoring of each acceptance criterion. Real build/test/lint gate before
  any LLM opinion is formed.
- **Dispute/arbitration:** a rejection is disputable only when every
  objective gate was green (gate failures are machine-checked ground truth).
  Executor gets exactly one evidence-based appeal per task per run; the
  Arbiter (`qwen-max`) reads actual evidence files and rules overturn/uphold.
- **Durable ledger:** SQLite, WAL mode. `tasks` holds current state;
  `attempts` is an append-only audit trail (one row per attempt, never
  mutated, full handoff JSON + verdict text). Single source of truth for the
  status-wall UI, the evaluation harness, and `--resume`.
- **Resume:** `--resume run_xxxxxxxxxxxx` reopens the same ledger and
  continues with no re-planning.
- **Seven-state task machine:** `PENDING → READY → IN_PROGRESS →
  PENDING_REVIEW → DONE → ARCHIVED`, with `BLOCKED` as the escalation state.
  Only three transitions are automatic (dependency satisfied, atomic claim,
  crash-lease reclaim); every other move requires an explicit action, so the
  machine can never silently mark itself done.

## (3) Track 3 mapping — with evidence

Track 3 (Agent Society) asks for agents that divide labor and negotiate.

**Task division & role assignment.**
- Planner splits the requirements checklist into a dependency-ordered task
  DAG; each task carries acceptance criteria and a runnable `test_strategy`.
- Dispatcher (zero-LLM, pure code) assigns tasks to workers via atomic CAS
  claims plus TTL-lease crash recovery — role assignment is a mechanical,
  race-free operation, not an LLM guess.

**Negotiation / conflict resolution.**
- REJECT → DISPUTE → ARBITRATION pipeline. A rejection is disputable only
  when every objective gate was green. The executor gets exactly one
  evidence-based appeal per task per run (`solicit_dispute`). The Arbiter
  (`qwen-max` — same tier as the Planner, deliberately outranking both the
  disputing executor and the verifier being disputed) reads the actual
  evidence files and rules `overturn` or `uphold`. Overturn → task passes.
  Uphold → the arbiter's clarification folds into the rejection reason for
  the executor's next attempt.
- Execution-conflict resolution: dispatcher's CAS claim prevents two workers
  holding the same task; TTL lease reclaims crashed workers' tasks; a
  consecutive-failure circuit breaker escalates a task to `BLOCKED` instead
  of retrying indefinitely.

**Measurable efficiency gain — A/B/C evaluation table (verbatim from README).**

Protocol: same frozen checklist, same executor model, same tools, one
independent referee (`scripts/evaluate.py` + `scripts/referee.py`). Checklist
planned once with the real Planner into a frozen `exam.json` so every
condition is judged against literally the same task list. Referee re-runs
each task's own `test_strategy` directly against the filesystem plus one
blanket `pytest -q` sweep — no LLM judgement, and Foreman's own Verifier
verdicts are never used for the cross-condition score.

Run on `demo/requirements_mini.md` (5 tasks), `2026-07-04`
(`evals/results_20260704T080344Z.json`):

| Condition | Referee pass | Wall-clock (s) | Executor attempts | Total tokens |
|---|---|---|---|---|
| A — single-agent one-shot | 0/5 (overall `pytest -q`: PASS, but wrong test filenames) | 136 | 1 | 75,253 |
| B — sequential, no verification | 4/5 | 218 | 5 | 112,912 |
| C — full Foreman | 5/5 (T04 rejected once, healed on attempt 2) | 461 | 6 | 267,193 |

Condition definitions:
- **A — single-agent one-shot:** entire checklist as one task, one
  `Executor.execute` call, `max_iters=60`, no verifier, no retries.
  Reproduces "here are N requirements, go."
- **B — sequential feeding, no verification:** one executor call per exam
  task in dependency order, `max_iters=15` each; whatever comes back is
  accepted unconditionally. Isolates decomposition alone from decomposition +
  verification.
- **C — full Foreman:** same frozen exam tasks, normal
  verifier/retry/dispute loop.

Honest cost framing: full verification (C) costs roughly **2.4x** the tokens
of sequential-no-verify (B) and **3.5x** Condition A, in exchange for going
from 0-4/5 referee-passed to 5/5.

Threats to validity (carry forward, do not drop):
- Single runs, not averages — LLM sampling variance means a rerun can shift
  these numbers; treat as one data point, not a definitive ranking.
- Condition A's 0/5 is partly a naming artifact: the referee's
  `test_strategy` expects specific filenames (e.g. `test_health_check.py`);
  Condition A wrote its own tests to `test_app.py` instead. The
  per-requirement test-file naming law (`test_reqNN.py`) was added afterward
  specifically to make cross-condition filenames comparable — this run
  predates that fix being exercised end-to-end, so A's true single-shot
  completion rate is likely better than 0/5 but still clearly behind B and C
  on the harder items (auth, filtering, pagination).
- Training-data familiarity: Flask + SQLite CRUD is a common pretraining
  pattern; a less common stack could look different.
- The weaker executor still hit 5/5: Condition C used `qwen-plus` (not the
  stronger `qwen3-coder-plus` executor tier) and still reached 5/5
  referee-passed — evidence the orchestration loop, not raw model strength,
  is doing the compensating.

## (4) Three real negotiation incidents from live runs

All traceable in the ledgers/events committed with the development history.

1. **Defective machine-generated gate.** In the very first live run, the
   planner emitted a syntactically invalid verification command — an exam no
   executor could ever pass. The system neither rubber-stamped the (actually
   correct) work nor looped forever: it burned its 3-attempt budget and
   blocked. Produced two permanent fixes: invalid-gate detection in the
   verifier, and a validation gate on the planner's own output ("everyone
   gets verified, including the examiner").
2. **Executor's test asserted the wrong thing.** In a later run the executor
   wrote a test expecting `'Amount is required'` where the app's validation
   order actually returns `'No JSON data provided'` first. The gate ran the
   test for real, failed it, and the feedback named the exact assertion; the
   next attempt fixed it. A single agent grading itself would have shipped
   that test.
3. **Test-isolation bug caught by the gate.** In the evaluation run
   (Condition C, T04) the gate failed because stale rows from earlier test
   runs leaked into `/expenses` (12–16 rows where 2 were expected). The
   verifier's feedback prescribed the standard fix (fresh in-memory DB per
   test); the next attempt healed it.

## (5) Novelty vs MetaGPT / ChatDev

MetaGPT, ChatDev, and similar frameworks optimize **code-generation quality
from one prompt** — good architecture, good SOP roleplay, one shot at a whole
app. Foreman optimizes something else: **execution fidelity of a long
checklist** — did the 17th requirement actually get implemented and proven,
not just claimed. That means verification loops instead of a single review
pass, negotiation when a verdict is disputed, and a durable ledger instead of
conversation memory. Foreman is not a fork of MetaGPT/ChatDev or any other
project; the task-DAG-plus-verifier-gate design was built directly against
measured failure modes (METR long-horizon ceiling, Toby Ord's error
compounding, Chroma's context-rot study, ADaPT decomposition results, CRITIC's
finding that self-review without tool feedback is unreliable, Reflexion's
retry-diminishing-returns, AutoGPT's documented runaway-retry incident,
Cognition's writeup on multi-agent write conflicts).

## (6) Honest limitations

- **Verification is pytest-oriented.** The objective gate assumes
  `test_strategy` commands are pytest/`python -c` runnable (per the frozen
  planner contract in `docs/CONTRACTS.md` §5). Roadmap: pluggable gate runner
  so `npm test`, `go test`, etc. qualify as first-class objective gates.
- **Single-node SQLite ledger.** Deliberate choice for a local, double-click
  tool — WAL mode gives concurrent reads for the console for free, zero
  infra to stand up. Roadmap: the ledger sits behind one interface
  (`foreman/ledger.py`), so scale-out is a backend swap (Postgres/etcd-backed
  claim semantics), not an orchestrator rewrite.
- **Evaluation is demo-scale.** Three-condition harness run end-to-end on a
  5-item checklist (5/5 referee-passed); the 20-item full checklist
  (`demo/requirements_full.md`) is planned but pending a live quota-unblocked
  run. Roadmap: run it and publish the full A/B/C table alongside the 5-item
  result rather than replacing it.
- **Executor model tier gap.** The evaluation's Condition C used `qwen-plus`
  rather than the stronger `qwen3-coder-plus` tier and still reached 5/5 —
  suggestive evidence for the orchestration loop over raw model strength, but
  not yet validated at the 20-item scale or across executor tiers.

## No-coupon reality (frame as a strength, not an excuse)

The hackathon credit coupon never arrived. Foreman runs entirely on DashScope
**FREE per-model quota** instead — this means the whole demo is reproducible
by any judge for free, no coupon needed.

- **Confirmed-working free-tier models:** `qwen-max` (planner/verifier/
  arbiter), `qwen3-coder-flash` (executor), plus `qwen-turbo` and
  `qwen3-max` as fallbacks.
- **EXHAUSTED — do NOT reference as usable:** `qwen-plus`, `qwen-flash`,
  `qwen3-coder-plus`. (Note: the README's role table and the A/B/C eval both
  predate exhaustion and cite `qwen-plus`/`qwen3-coder-plus` — those are
  historical records of what was run, not a claim that those models are
  currently available on free quota. Keep both facts straight in downstream
  docs: the eval table is accurate as run; the current reproducible path for
  a judge is the confirmed-working free-tier list above.)

## Zero-key mode

Foreman has a Demo/mock mode (`--mock` flag, or the "Demo mode" checkbox in
the web console) that runs the entire console — parallel runs, dispute/
arbitration, stop/resume, per-run cost accounting — with **no API key**,
fully offline, against a scripted fake planner/executor/verifier. This is the
judge quick-try path: the entire product experience in seconds, no DashScope
key required. A mock run normally finishes in well under a second; set
`FOREMAN_MOCK_DELAY=<seconds>` (clamped 0–30) or pass `"mock_delay_s"` in the
API body to slow it to a filmable pace.

## Deployment

Alibaba Cloud Function Compute (FC), free tier, custom runtime, Singapore
region, `serve.py` behind an `fcapp.run` URL. Self-contained zip already
built at `fc/foreman-fc.zip` (vendored `openai`). Deployment steps are
written in `docs/DEPLOY.md`; not yet executed end-to-end against a live FC
instance (tracked as an open item in Status).

## Alibaba API evidence files

- `foreman/config.py` — DashScope OpenAI-compatible client (`.env` loading,
  `Settings`, the DashScope client factory).
- `foreman/llm.py` — shared `chat_json` helper calling
  `client.chat.completions.create`; handles DashScope's JSON-mode and
  code-fence quirks, plus token metering.

Foreman talks to Qwen exclusively through the DashScope international
endpoint in OpenAI-compatible mode — no vendor SDK beyond `openai`. Model
routing is per-role and overridable via environment variables so the
DashScope catalog can drift without a code change.

## Quick reference — test suite / status

- 231 tests pass with no API key required (`python -m pytest -q`): concurrency
  safety, retry ladder, crash recovery, dispute/arbitration, resume, git
  safety rails, command policy, web API, Console v2 telemetry/pricing/
  stop-resume/config.
- Existing-project mode: Foreman can point the Executor at a real git repo
  instead of a fresh sandbox (`--project-dir`). Safety model: repo must be
  git-initialized and ideally clean; Foreman works on an isolated
  `foreman/<run_id>` branch, commits once per completed task; main branch
  never touched; Foreman never merges or pushes. Hardened by adversarial
  audit: non-overridable mid-merge/rebase rejection, pre-existing-branch
  refusal, per-repo run lock (`.git/foreman.lock`), force-dirty work
  snapshotted as its own labeled commit. Full guide:
  `docs/EXISTING_PROJECTS.md`.
