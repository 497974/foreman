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
