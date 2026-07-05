# Foreman internal contracts (frozen 2026-07-04)

Interfaces below are FROZEN. Implementations must match exactly so modules
integrate without rework. Existing code in `foreman/` (models, ledger,
dispatcher, config, llm, planner) is the source of truth for types referenced
here — read those files before implementing.

## Conventions
- Python 3.11, stdlib + `openai` + `pytest` only. No new dependencies.
- All new unit tests must run WITHOUT any API key (use fakes/mocks).
- Windows is the dev box: `shell=True` commands run under cmd.exe. Never rely
  on unix-only tools (grep/jq) in tests.
- Keep module docstrings in the same voice as existing files: explain *why*.

## 1. `foreman/workspace.py`

```python
@dataclass
class CommandResult:
    exit_code: int
    stdout: str          # truncated to MAX_OUTPUT (10_000) chars, note when truncated
    stderr: str          # same truncation
    duration_s: float
    timed_out: bool

class WorkspaceError(RuntimeError): ...

class Workspace:
    def __init__(self, root: str | Path):
        # creates root if missing; stores resolved absolute Path in self.root

    def _resolve(self, rel: str) -> Path:
        # jail check: resolved path MUST stay under self.root, else WorkspaceError

    def read_file(self, path: str) -> str            # WorkspaceError if missing
    def write_file(self, path: str, content: str) -> str   # mkdir -p parents; returns "wrote <path> (<n> bytes)"
    def list_dir(self, path: str = ".") -> list[str] # names, dirs suffixed "/"
    def run(self, command: str, timeout: float = 120.0) -> CommandResult
        # subprocess.run(shell=True, cwd=self.root, capture_output=True, text=True)
        # on TimeoutExpired -> timed_out=True, exit_code=-1
```

## 2. `foreman/executor.py`

```python
class Executor:
    def __init__(self, client, model: str, workspace: Workspace,
                 max_iters: int = 15, command_timeout: float = 120.0): ...

    def execute(self, task: Task, dependency_handoffs: list[Handoff]) -> Handoff:
        # OpenAI function-calling loop against DashScope (client from config.make_client)
```

Tools exposed to the model (OpenAI `tools` schema):
- `read_file(path)`, `write_file(path, content)`, `list_dir(path)`,
  `run_command(command)` -> mapped 1:1 to Workspace methods; tool results are
  strings (for run_command: "exit=N\nstdout:...\nstderr:..." truncated).
- `done(summary, completed_work: list[str], files_touched: list[str],
  gotchas: list[str], self_check: list[str])` -> ends the loop.

Rules:
- System prompt must include, verbatim in spirit (adapt wording):
  "Work ONLY inside the workspace via the tools." /
  "DO NOT implement placeholders or simplified implementations — full
  implementation required." / "Run the verification command for this task
  before calling done; you cannot claim completion without it passing." /
  "If a previous attempt was rejected, the feedback is authoritative — fix
  exactly what it says."
- User message contains: task card (title, description, acceptance_criteria,
  test_strategy), attempt number, verifier feedback from prior attempts (if
  any, passed in via task.last_error), and the dependency handoffs (their
  completed_work / files_touched / interface_contract / gotchas).
- Loop ends when the model calls `done` OR max_iters reached (then build a
  Handoff with outcome=timeout note in gotchas). Return a `Handoff` with
  task_id, attempt_no=task.attempt_count, fields from `done` args.
- If the model produces plain text with no tool call, nudge once ("use a tool
  or call done") then count toward max_iters.

Unit tests: drive `execute` with a **scripted fake client** (a class whose
`chat.completions.create` returns pre-canned tool_call responses) covering: a
write_file->run_command->done happy path; the done() Handoff mapping; jail
violation surfacing as tool error string (not exception); max_iters cutoff.

## 3. `foreman/verifier.py`

```python
@dataclass
class VerificationReport:
    passed: bool
    coverage_rate: float               # mean of item scores (1 / 0.5 / 0)
    items: list[dict]                  # {criterion, status, detail} status in
                                       # satisfied|partially_satisfied|not_satisfied
    objective_gate: dict               # {command, exit_code, passed, output_tail}
    actionable_feedback: list[str]
    reason: str                        # one-line verdict for ledger.record_verdict

class Verifier:
    def __init__(self, client, model: str, workspace: Workspace,
                 command_timeout: float = 180.0): ...
    def verify(self, task: Task, handoff: Handoff) -> VerificationReport
```

Verification order (deterministic first, LLM last):
1. **Objective gate**: if `task.test_strategy` is non-empty, run it via
   `workspace.run`. Non-zero exit => `passed=False` immediately; coverage still
   computed via LLM for feedback quality, but verdict is REJECT. Additionally,
   if a `tests/` dir or `test_*.py` exists in the workspace, run
   `python -m pytest -q` as a regression gate (failure => REJECT).
2. **Coverage scoring** via `chat_json` (foreman/llm.py) with the verifier
   model: give it the acceptance criteria, the gate outputs, `list_dir`
   recursive listing, and the contents of files in `handoff.files_touched`
   (each truncated to 4000 chars). It returns
   `{"items":[{"criterion","status","detail"}...], "feedback":["..."]}`.
   Guard: coerce unknown status values to "not_satisfied".
3. passed = objective gates all green AND every item status == "satisfied".
4. `reason`: "<n_satisfied>/<n> criteria; gate exit=<code>" style one-liner +
   first feedback item. actionable_feedback items must name files/expected vs
   actual where possible (comes from the LLM feedback list + gate output tail).

Unit tests with a fake client + real Workspace in tmp_path: gate failure short-
circuits to REJECT; all-satisfied + green gate => passed; unknown status string
coerced; pytest regression gate triggers when a failing test file exists.

## 4. `foreman/orchestrator.py` + `main.py` (repo root)

```python
class Orchestrator:
    def __init__(self, settings: Settings, run_root: str = "runs"):
        # builds client, Ledger(db at runs/<run_id>/ledger.db after create_run),
        # Workspace(runs/<run_id>/workspace), Planner/Executor/Verifier
    def run_checklist(self, requirements: str) -> dict:  # summary counts + run_id
```

Loop per task: `dispatcher.claim(worker_id)` -> executor.execute(task,
handoffs of its parents from this run) -> `ledger.submit_for_review` ->
verifier.verify -> `ledger.record_verdict(passed, reason)`. On REJECT the
ledger requeues automatically (retry ladder is already in the ledger). Stop
when `ledger.is_run_complete()` or dispatcher reports stalled.

Every state change appends one JSON line to `runs/<run_id>/events.jsonl`:
`{"ts": ..., "type": "claim|submit|verdict|promote|reclaim|plan", "task_id":
..., "detail": ...}` — this file is the future SSE/UI data source.

`main.py` CLI: `python main.py --checklist demo/requirements_mini.md [--mock]`
`--mock` wires the fake executor/verifier from demo/smoke_run.py style stubs
(no API) so the full loop is testable free. Without --mock it uses real Qwen.
Print the ASCII status wall after each verdict (reuse smoke_run's WALL idea).

## 5. Planner prompt amendment (already applied by the architect)
test_strategy must be offline-runnable: pytest / python -c assertions only —
no curl against a live server, no manual steps. (Flask apps are tested via
their test client.)

---

# Addendum (frozen 2026-07-04, after first all-green e2e)

## 6. Dispute & Arbitration (foreman/arbiter.py + orchestrator wiring)

The negotiation layer. Grounds: a verifier is ~80%-reliable LLM judgement when
gates can't decide; the graded party deserves one evidence-based appeal.

Eligibility: a REJECT where ALL objective gates were green (rejection came only
from criteria scoring). Gate failures are not disputable — machines outrank
rhetoric. ONE dispute per task per run (track disputed_task_ids in orchestrator).

Flow (inside orchestrator, BETWEEN verifier.verify and ledger.record_verdict):
1. Ask the executor model (one chat_json call, no tools):
   system: "You may dispute a rejection ONLY with concrete evidence..."
   user: task card + your handoff + the verification report items/feedback.
   -> {"dispute": bool, "rebuttal": str, "evidence": [{"file": str, "claim": str}]}
   If dispute=false (concede): proceed to record_verdict(passed=False) as usual.
2. If dispute=true: Arbiter = planner model (qwen-max). Read the ACTUAL contents
   of every evidence file (workspace.read_file, 4000-char cap). chat_json ->
   {"ruling": "overturn"|"uphold", "reasoning": str, "criteria_clarification": str}
3. ruling=overturn -> record_verdict(passed=True, reason="arbiter overturned:
   <reasoning>"). ruling=uphold -> record_verdict(passed=False, reason=
   verifier reason + " | arbiter upheld: " + criteria_clarification) so the
   clarification reaches the next attempt via last_error.
4. Events: append "dispute" and "arbitration" events (task_id, detail with
   rebuttal/ruling excerpts) to events.jsonl.

class Arbiter: __init__(client, model, workspace); rule(task, handoff, report,
rebuttal, evidence) -> dict. Executor-side dispute prompt lives in orchestrator
or arbiter module — implementer's choice, but NO tool loop, single JSON calls.

Unit tests (fake client): concede path records reject unchanged; overturn path
records pass; uphold path appends clarification to reason; gate-failure
rejections never trigger the dispute flow; one-dispute-per-task enforced.

## 7. Resume (--resume RUN_ID in main.py + Orchestrator.resume_run)

Resume is a headline feature (durable ledger), not a convenience. 
`python main.py --resume run_xxx`: reopen runs/run_xxx/ledger.db + workspace,
revive_blocked(reset_attempts=True) every BLOCKED task, then enter the normal
loop (no re-planning; the plan lives in the ledger). Events keep appending to
the same events.jsonl. Orchestrator.resume_run(run_id) -> same summary dict as
run_checklist. Unit test: build a ledger with a blocked task + done tasks via
mocks, resume, assert blocked task re-executes and run completes.

## 8. Local Web Console (serve.py + foreman/webui/)

The product face: double-click, watch the foreman work. STDLIB ONLY
(http.server.ThreadingHTTPServer) — no flask/fastapi; the .bat experience must
not depend on anything beyond requirements.txt.

serve.py at repo root:
- `python serve.py [--port 8787]` starts the server and opens the browser
  (webbrowser.open) at http://127.0.0.1:8787.
- Serves foreman/webui/index.html at "/" (single file: inline CSS/JS, no CDN).
- JSON API (all under /api/, read ledger.db + events.jsonl fresh per request —
  WAL mode allows concurrent reads while a run writes):
  GET  /api/runs                    -> [{run_id, created_at, counts, complete}]
  GET  /api/runs/<id>               -> {tasks: [...], counts, complete}
  GET  /api/runs/<id>/events?after=<n> -> {events: [...], next: <n+len>}
  GET  /api/runs/<id>/task/<tid>    -> task row + attempt history (verdict text)
  POST /api/runs {"requirements": "..."} -> {run_id} ; starts
       Orchestrator().run_checklist in a daemon thread. Live Qwen — requires
       .env; return HTTP 409 with a clear error if DASHSCOPE_API_KEY missing.
- UI (English, single dark-on-light page, no external fonts/CDN):
  left = textarea for the checklist + Start button + run picker;
  right = four-color status wall (green done / red blocked / amber
  in_progress|pending_review|disputing / grey pending|ready), one cell per
  task, click cell -> drawer with title, criteria, attempts, verdict reasons,
  actionable feedback timeline; bottom = live event feed (poll every 2s) with
  timestamps + elapsed clock + attempt counter. Show "DISPUTE" and
  "ARBITRATION" events prominently (amber badge) — negotiation must be VISIBLE.
- start_foreman.bat at repo root: @echo off; cd /d %~dp0; python -m pip install
  -r requirements.txt -q; python serve.py. Plus a first-run check that .env
  exists with a friendly message if not.

Tests: light — one test that the API layer's ledger-reading functions return
sane JSON shapes against a ledger fixture (no HTTP server needed; factor the
data-access functions so they're importable and testable without sockets).

---

# Addendum 2 (frozen 2026-07-04 evening): Product Console v2

Two agents build this in parallel: BACKEND owns python files, FRONTEND owns
foreman/webui/index.html only. The API shapes below are the integration
contract — code against them exactly.

## 9. Backend (telemetry, stop/resume, config, mocks, API v2)

### 9.1 Per-run telemetry (foreman/telemetry.py)
Thread-local run tagging: `set_current_run(run_id | None)`, `get_current_run()`.
`TokenMeter.record(...)` additionally accumulates under the current thread's
run tag when set. New: `METER.run_totals(run_id) -> {"per_model": {...},
"totals": {"prompt_tokens": int, "completion_tokens": int, "calls": int}}`.
Orchestrator sets the tag at the TOP of run_checklist / run_tasks / resume_run
(they execute on the worker thread) and clears it in a finally.

### 9.2 Pricing (foreman/pricing.py, new)
`PRICES: dict[str, tuple[float, float]]` USD per 1M input/output tokens —
approximations as of 2026-07 (comment says verify in console): qwen-max
(1.6, 6.4), qwen-plus (0.4, 1.2), qwen3-coder-plus (1.0, 5.0), qwen-turbo
(0.05, 0.2), qwen-flash (0.1, 0.4), DEFAULT (0.5, 1.5).
`estimate_usd(per_model: dict) -> float` (uses DEFAULT for unknown models).

### 9.3 Stop sentinel + resume
- Stop = create file `runs/<run_id>/STOP`. Orchestrator._drive_loop checks for
  it at the top of every iteration; when present: emit event "stopped", return
  summary with `"stopped": True`. Task-boundary granularity (an in-flight
  executor attempt finishes first) — document in docstring.
- resume_run: delete the STOP sentinel if present before entering the loop
  (it already revives BLOCKED tasks; READY/expired-lease tasks flow naturally).

### 9.4 Run config + persisted usage (runs/<run_id>/config.json)
serve.py writes at start: `{"models": {"planner","executor","verifier"},
"mock": bool, "created_at": iso, "requirements_preview": first 200 chars}`.
Orchestrator (or serve's worker wrapper) merges `"usage_final":
METER.run_totals(run_id)` + `"est_usd"` into it when the run finishes/stops.
webui_data.get_run_detail returns `"config"` (the file, or null) and
`"usage"` (live METER.run_totals if nonzero else usage_final) + `"est_usd"`.

### 9.5 Mocks module (foreman/mocks.py, new)
Move main.py's mock planner/executor/verifier classes here (MockPlanner splits
numbered checklist lines into Tasks with test_strategy `python -c "print('ok')"`;
MockExecutor optionally rejects each task's first attempt; MockVerifier real
objective_gate shape, gate red on reject so dispute flow is bypassed).
main.py imports from here — CLI behavior unchanged. serve.py uses them for
mock runs. Mock runs write the same ledger/events artifacts (UI identical) and
finish in seconds.

### 9.6 API v2 (serve.py + foreman/webui_data.py)
- POST /api/runs  body {"requirements": str, "models": {"planner"?, "executor"?,
  "verifier"?}, "mock"?: bool} -> {"run_id"}. Models override via
  dataclasses.replace(Settings...). mock:true requires NO key (skip key check).
- POST /api/runs/<id>/stop -> 200 {"stopped": true} (writes sentinel;
  idempotent; 404 unknown run).
- POST /api/runs/<id>/resume -> {"run_id"} (409 if that run's thread is still
  active; 400 for mock runs "mock runs are not resumable"; deletes sentinel,
  spawns resume_run on a daemon thread, registers in _active_runs).
- GET  /api/runs/<id>/download -> application/zip of workspace/ (in-memory
  zipfile; exclude __pycache__, *.db, *.db-*; filename foreman-<id>.zip).
- DELETE /api/runs/<id> -> archive: move runs/<id> into runs/_archived/<id>
  (409 if active). list_runs must skip _archived.
- GET /api/config -> {"key_present": bool, "key_masked": "sk-ws-…XQ" or null,
  "endpoint": base_url, "models_known": [qwen-max, qwen-plus, qwen3-coder-plus,
  qwen-turbo, qwen-flash], "defaults": {planner, executor, verifier}}.
  NEVER return the full key.
- GET /api/templates -> [{"name": file stem, "content": str}] from demo/*.md.
- runs list items gain: "active": bool (thread alive in _active_runs),
  "mock": bool (from config.json), "est_usd": float|null.

Error surfacing: the background worker already emits an "error" event on
exception; ADDITIONALLY, if the exception text contains "insufficient_quota"
emit detail {"friendly": "Model free quota exhausted — switch the executor
model in New Run, or add billing/coupon in the console."}.

Tests (no key): telemetry two-thread run tagging isolation; pricing math incl.
unknown model; STOP sentinel stops a mock run between tasks + resume completes
it; config.json write/read via get_run_detail; download zip helper excludes
db/__pycache__; archive moves dir and list_runs skips it. Full suite stays green.

## 10. Frontend (foreman/webui/index.html ONLY)
Single self-contained file, same palette (steel blue #1E4E79 / safety orange
#D9700A / green/red/amber/grey statuses), English, no CDN.
Layout: LEFT SIDEBAR = (a) "New Run" card: template dropdown (GET
/api/templates; selecting fills textarea), requirements textarea, three model
<select>s populated from /api/config (each with a "custom…" option revealing a
text input), "Demo mode — runs without an API key" checkbox (checked+locked
with a hint when key_present=false), Start button; (b) runs list (poll
/api/runs every 3s): per row status dot (green=complete, amber=active,
red=has blocked tasks, grey=idle incomplete), short id, HH:MM created, est_usd
when present, "MOCK" chip for mock runs; click = select run.
MAIN AREA (per selected run): header row = run id + config chips (models,
mock), live cost "≈$0.0123 · 45,678 tok" (from run detail poll), elapsed
clock, buttons: Stop (only while active), Resume (only when idle & incomplete
& not mock), Download (GET download), Archive (DELETE + confirm()).
Below: existing status wall + task drawer + event feed (keep; drawer
additionally renders each attempt's handoff summary JSON fields nicely:
completed_work, files_touched, gotchas — parse attempts[].summary as JSON,
fall back to raw text). Error events + quota-friendly messages render as a
dismissible red banner above the wall. Empty states for no-runs and no-key.
No frameworks; keep total file under ~60KB.

---
# Addendum 3 (frozen 2026-07-04 late): Judge-scored hardening
Three parallel tracks. Each agent owns disjoint files. All tests stay green
(currently 123). No new pip deps (stdlib+openai+pytest). English comments in
existing voice.

## 11. Command safety policy (foreman/workspace.py + tests)
The executor's run_command currently runs anything via shell=True with only a
path jail on file ops. Add a command-safety layer WITHOUT breaking existing
behavior/tests:
- New foreman/safety.py: `is_blocked_command(cmd: str) -> tuple[bool, str]`.
  Block, by conservative substring/regex, clearly destructive host-touching
  ops: `rm -rf /`, `rm -rf ~`, del/rmdir of drive roots, `format`, `shutdown`,
  `reboot`, `mkfs`, `dd if=`, `:(){` fork bomb, curl|bash / iwr|iex pipe-to-shell,
  `git push` (don't let sandbox work escape), writing outside cwd via absolute
  paths in rm/mv, registry edits (`reg delete`), `netsh`, `taskkill /f`.
  Return (True, human reason) if blocked, else (False, "").
- Workspace.run(): before executing, call is_blocked_command; if blocked,
  return a CommandResult(exit_code=126, stdout="", stderr="blocked by Foreman
  command safety policy: <reason>", duration_s=0, timed_out=False) instead of
  running it. Add a `allow_all: bool=False` Workspace ctor flag (default False)
  that bypasses the check for power users; document why default-deny.
- docs/SECURITY.md: threat model — sandbox scope (cwd jail + command policy),
  what it does/doesn't protect against (honest: not a container; --yolo agents
  run at process privilege), recommended hardening for production (Docker/FC
  ephemeral instance), and that this is a demo-grade guardrail.
- Tests tests/test_safety.py: blocked patterns caught, benign commands
  (pytest, python, echo, ls, mkdir within cwd) pass, Workspace.run returns 126
  on blocked, allow_all bypasses. Full suite green.

## 12. Model fallback chain (foreman/llm.py + foreman/config.py + tests)
We hit "insufficient_quota" three times and the whole run died. Make the client
resilient AND make it a feature:
- config: Settings gains `fallback_models: list[str]` (from env
  FOREMAN_FALLBACK_MODELS, comma-sep; default "qwen-turbo,qwen-flash,qwen3-coder-flash").
- foreman/llm.py: a thin wrapper used by chat_json AND the executor's raw
  completions — on a 403/insufficient_quota (or 429 after retries) for model M,
  transparently retry the SAME call with the next model in the chain not equal
  to M; emit nothing to stdout but expose the substitution via a module-level
  callback hook `on_model_fallback(original, used)` (default no-op) so the
  orchestrator can log a "model_fallback" event. Keep it minimal; don't change
  chat_json's signature.
- Wire executor.py's completions call through the same fallback helper (small
  refactor: extract the "create with fallback" into llm.py, call it from both).
- orchestrator: register on_model_fallback to emit a "model_fallback" event to
  events.jsonl (so the UI/README can show graceful degradation).
- Tests tests/test_fallback.py with a fake client that raises insufficient_quota
  on model A then succeeds on model B: assert the call succeeds via B, callback
  fired with (A,B), and that a non-quota error is NOT swallowed. Full suite green.

## 13. Executor wind-down + README visuals (foreman/executor.py + README + docs/)
(a) Executor: when the loop reaches its last 2 iterations without done(), inject
one system nudge: "You are almost out of steps — finish the current file, run
the verification command, and call done() now." Only once. Keep max_iters
behavior/tests intact (the existing timeout-handoff test must still pass).
(b) Create docs/architecture.svg — a clean architecture diagram (hand-written
SVG, no external tools): requirements -> Planner -> Ledger <- Dispatcher(pure
code) -> Executor(native | qwen-code) -> Verifier(gates) -> dispute/arbiter ->
done; show the durable ledger + resume + web console. Steel-blue #1E4E79 /
orange #D9700A palette. Embed it at the top of README (after the tagline) via
![Architecture](docs/architecture.svg), and add a second small SVG
docs/statuswall.svg mocking the four-color status wall so the README shows what
the console looks like without a screenshot dependency.
(c) README: add an honest "## Limitations & roadmap" section — greenfield-only
(builds new code in a sandbox, doesn't yet edit existing repos), pytest-oriented
verification, single-node SQLite ledger (by design; scale-out path noted),
demo-scale evaluation (5-item verified; 20-item pending). Frame each with the
roadmap direction. Keep it tight and confident, not apologetic.
Full suite green after (a).

---
# Addendum 4 (frozen 2026-07-04 night): Existing-project mode

Two agents build this in parallel, file-disjoint (a prior round collided on a
shared file — this time: Agent A owns orchestrator/planner/executor/main.py/
new git+repo modules; Agent B owns serve.py/webui/README/new docs file. Never
touch the other's files. Stage only your own files on commit.).

## 14. git_safety.py + repo_context.py + orchestrator/planner/CLI wiring (Agent A)

### foreman/git_safety.py (new)
Functions (all subprocess calls: list-form args, encoding="utf-8",
errors="replace" — the SAME fix backends.py needed for qwen-code; git also
emits non-ASCII, do not rediscover that bug; use cwd=path or -C path):

- class GitSafetyError(RuntimeError): ...
- is_git_repo(path) -> bool : `git -C <path> rev-parse --is-inside-work-tree`
- repo_root(path) -> Path | None : `git -C <path> rev-parse --show-toplevel`
- is_clean(path) -> bool : `git -C <path> status --porcelain` is empty?
- create_or_checkout_branch(path, branch: str) -> None :
  `git -C <path> branch --list <branch>`; empty output -> `checkout -b <branch>`,
  else -> `checkout <branch>` (idempotent — resume calls this again safely).
- commit_all(path, message: str) -> bool :
  `git -C <path> add -A` then check `git -C <path> diff --cached --quiet`
  (nonzero exit = something IS staged) before `git -C <path> commit -m <message>`.
  Returns True if a commit was made, False if there was nothing to commit
  (never make an --allow-empty commit).
- ensure_ready(path, force_dirty: bool = False) -> None :
  Raises GitSafetyError with an ACTIONABLE message (what to do) if:
  not is_git_repo(path) -> "run `git init` in that folder first";
  repo_root(path) != resolved(path) -> "point Foreman at the repo ROOT
  (<repo_root>), not a subdirectory — this avoids partial-repo commits";
  not force_dirty and not is_clean(path) -> "commit or stash your changes
  first, or pass force_dirty=True to proceed anyway (not recommended)".

### foreman/repo_context.py (new)
build_repo_snapshot(root: Path, max_depth=3, max_entries=200, max_preview_chars=2000) -> str
— a directory tree (skip .git/node_modules/__pycache__/venv/.venv/dist/build)
plus truncated previews of any of README.md/package.json/requirements.txt/
pyproject.toml/Cargo.toml/go.mod that exist at the root. Purely textual, no
git dependency. This orients the Planner; it is NOT a live source of truth
(the executor still uses list_dir/read_file for ground truth at execution time).

### foreman/orchestrator.py — Orchestrator.__init__ gains:
project_dir: str | Path | None = None, force_dirty: bool = False
When project_dir is given:
1. git_safety.ensure_ready(project_dir, force_dirty) — raises GitSafetyError
   (let it propagate; the CLI/API layer turns it into a clean user-facing error).
2. branch = f"foreman/{self.run_id}"; git_safety.create_or_checkout_branch(project_dir, branch).
3. self.workspace = Workspace(Path(project_dir)) INSTEAD of run_dir/"workspace".
   Ledger db / events.jsonl / config.json still live under run_dir as always —
   only the code workspace itself moves; Foreman's own bookkeeping never
   pollutes the user's repo.
4. Write run_dir / "project_mode.json": {"project_dir": str(resolved_path), "branch": branch}.
5. self._emit("project_mode", detail={"project_dir": ..., "branch": branch}).
6. Build repo_context = repo_context.build_repo_snapshot(project_dir) and pass
   it to self.planner.plan(requirements, repo_context=repo_context).
7. Construct Executor with existing_project=True (see below).

Commit-after-DONE: read the orchestrator's task-completion flow (both the
plain-verify-pass path and the dispute/arbitration-overturn path funnel into
one place before ledger.record_verdict(passed=True, ...) — find that single
point by reading the file, don't guess a line number). Right there, if
self.project_dir is set and passed is True, call
git_safety.commit_all(self.project_dir, f"Foreman: {task.task_id} {task.title}").
One commit per task, only on a real pass.

resume_run(run_id): BEFORE constructing Workspace, check for
run_dir / "project_mode.json". If present, read its project_dir/branch,
point Workspace there, and call create_or_checkout_branch again (idempotent —
this is what makes --resume work correctly for an existing-project run
without the caller re-specifying project_dir).

### foreman/planner.py
Planner.plan(self, requirements: str, repo_context: str = "") -> list[Task]
— when non-empty, append a "## Existing project context" section to the user
message with the snapshot, plus one line telling the model: tasks should
respect existing structure/conventions and avoid recreating files that already
serve the same purpose. Default "" preserves current greenfield behavior exactly.

### foreman/executor.py
Executor.__init__(..., existing_project: bool = False). When True, append one
line to SYSTEM_PROMPT (not replace it): "This is an EXISTING codebase, not a
fresh scaffold — read relevant files before editing them, and follow existing
conventions rather than rewriting things from scratch unless the task says to."
Default False = zero behavior change for greenfield mode / existing tests.

### main.py CLI
Add --project-dir PATH and --force-dirty (store_true) to the --checklist
branch (not to --resume — resume reads project_mode.json automatically, per
above). On a GitSafetyError, print its message and exit 1 (no traceback).

### Tests (new files only — do not edit existing test_orchestrator.py etc.)
tests/test_git_safety.py — use the REAL git binary via subprocess against a
tmp_path (git is present in this dev environment): is_git_repo True/False,
repo_root matches/mismatches a subdirectory, is_clean true/false after writing
a file, create_or_checkout_branch creates then re-checks-out idempotently,
commit_all returns False on no-op and True after a real change, ensure_ready
raises GitSafetyError with the right message for each of the three failure
modes and passes when everything is fine.
tests/test_repo_context.py — snapshot includes files at depth<=max_depth,
excludes .git/node_modules/etc., truncates a large README.
tests/test_orchestrator_existing_project.py — build a tmp_path git repo (git
init, one commit, one existing test file), point a mocked Orchestrator
(reuse foreman/mocks.py's fakes, per tests/test_orchestrator.py's existing
pattern) at it via project_dir, run a 1-2 task checklist, assert: a
foreman/<run_id> branch was created, the ORIGINAL branch (main/master) has
zero new commits (only the foreman branch does), a commit exists per
completed task, and project_mode.json was written. Also test resume_run
re-derives project_dir/branch from project_mode.json without being told again.

Full suite green (currently 165 + yours). Commit ONLY: foreman/git_safety.py,
foreman/repo_context.py, foreman/orchestrator.py, foreman/planner.py,
foreman/executor.py, main.py, tests/test_git_safety.py, tests/test_repo_context.py,
tests/test_orchestrator_existing_project.py. Message: "Existing-project mode:
git-safety-gated real-repo editing on an isolated branch" + trailer
"Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>".

## 15. API + web console + docs (Agent B)

### serve.py POST /api/runs
Accept optional "project_dir": str and "force_dirty": bool in the body,
threaded into Orchestrator(...). On GitSafetyError, respond 400 with its
message verbatim (it is already human-actionable per §14). Do not touch
foreman/orchestrator.py — only serve.py's own call site.

### foreman/webui/index.html (the ONLY frontend file — same one from Console v2/i18n)
In the "New Run" card, add an optional text input "Existing project folder
(optional)" below the requirements textarea, plus a checkbox "I understand
this will create a git branch and modify real files in that folder" that must
be checked for the field to be sent (client-side guard, not a security
boundary — the real guard is server-side git_safety). If project_dir is set,
show the branch name (foreman/<run_id>, once known from run detail) in the run
header chips area alongside the existing model/mock chips. Route the error
banner to show a GitSafetyError's message clearly (it already renders generic
"error" events — confirm the message text is not truncated). Respect the i18n
system already in this file (add EN+ZH strings for the new label/checkbox/hint
to both STRINGS.en and STRINGS.zh — do not leave new UI text untranslated).

### README.md
Add a short "## Working on an existing project" section (near Quickstart):
one paragraph explaining --project-dir / the web console field, the safety
model (git required, isolated branch, per-task commits, your main branch is
never touched), and a pointer to docs/EXISTING_PROJECTS.md for detail. Keep it
tight, same voice as the rest of the file.

### docs/EXISTING_PROJECTS.md (new)
A short guide: prerequisites (folder must be a git repo, ideally clean),
CLI usage (python main.py --checklist reqs.md --project-dir C:\path\to\repo),
web console usage, what happens (branch created, per-task commits, review with
git log foreman/<run_id> / git diff main...foreman/<run_id>, merge or delete
the branch yourself — Foreman never auto-merges), and the force-dirty escape
hatch with an explicit "not recommended" warning.

Verify python -m pytest -q stays green after your edits (you are not adding
Python logic, just wiring — but confirm nothing else broke). Commit ONLY:
serve.py, foreman/webui/index.html, README.md, docs/EXISTING_PROJECTS.md.
Message: "Existing-project mode: API + console UI + docs" + trailer
"Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>".
