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
