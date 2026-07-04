"""Scripted fakes for planner/executor/verifier — no LLM, no API key.

Originally lived inline in main.py's --mock branch; relocated here (contract
§9.5) so serve.py's Product Console can offer the exact same "demo mode" for
mock runs without importing the CLI module. main.py now imports from here —
its --mock behavior is unchanged, just the classes moved house.

MockPlanner splits a numbered checklist into a linear chain of Tasks (one per
non-empty, non-comment line), each with a trivially-offline test_strategy.
MockExecutor always "succeeds". MockVerifier optionally rejects a task's
first attempt (to exercise the retry ladder + feedback plumbing) with a gate
shape that is always red on reject, so the dispute flow (contract §6, "gate
failures are not disputable") never triggers for a mock rejection — it would
otherwise try to reach a real chat client mocks intentionally don't have.
"""

from __future__ import annotations

import time
from pathlib import Path

from .arbiter import Arbiter
from .config import Settings
from .dispatcher import Dispatcher
from .ledger import Ledger
from .models import AttemptOutcome, Handoff, Task, TaskStatus
from .workspace import Workspace


class MockPlanner:
    """Turns a checklist into one Task per non-empty, non-comment line.

    Each task depends on the previous one (a simple linear chain) — enough to
    exercise claim/execute/verify/requeue end to end without a real LLM.
    """

    def plan(self, requirements: str) -> list[Task]:
        lines = [
            line.strip()
            for line in requirements.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        tasks: list[Task] = []
        prev_id = None
        for i, line in enumerate(lines, start=1):
            tid = f"T{i:02d}"
            tasks.append(
                Task(
                    task_id=tid,
                    title=line[:80],
                    description=line,
                    acceptance_criteria=[f"{line} is implemented"],
                    test_strategy="python -c \"print('ok')\"",
                    parents=[prev_id] if prev_id else [],
                )
            )
            prev_id = tid
        return tasks


class MockExecutor:
    """Stand-in for foreman.executor.Executor: no LLM, deterministic output.

    Mirrors demo/smoke_run.py's fake_execute — pretends to do the work and
    returns a plausible Handoff. If ``reject_first_attempt`` is True, the
    handoff itself is unaffected (MockVerifier owns the actual rejection
    decision) — this flag exists only for callers that want a distinct
    Handoff shape on retries; the default (False) is what main.py --mock and
    serve.py's mock runs use.
    """

    def __init__(self, reject_first_attempt: bool = False, delay_s: float = 0.0):
        self.reject_first_attempt = reject_first_attempt
        self.delay_s = delay_s

    def execute(self, task: Task, dependency_handoffs: list[Handoff]) -> Handoff:
        # Demo-pacing hook only (contract-adjacent, not a frozen interface):
        # filming the video needs a mock run slow enough to see the status
        # wall fill in cell-by-cell rather than finishing before the screen
        # recorder's first frame. Zero by default so tests stay fast.
        if self.delay_s:
            time.sleep(self.delay_s)
        return Handoff(
            task_id=task.task_id,
            attempt_no=task.attempt_count,
            outcome=AttemptOutcome.SUCCESS.value,
            completed_work=[f"implemented: {task.title}"],
            files_touched=[f"src/{task.task_id.lower()}.py"],
            handoff_reason="mock execution complete",
        )


class MockVerifier:
    """Stand-in for foreman.verifier.Verifier: rejects the first attempt of
    every task once (to exercise the retry ladder + feedback plumbing), then
    passes on the second attempt. Set ``always_pass=True`` for a verifier that
    never rejects (useful for fast demo runs)."""

    class _Report:
        def __init__(self, passed: bool, reason: str):
            self.passed = passed
            self.reason = reason
            self.actionable_feedback = (
                [] if passed else [
                    "mock: fix the deliberately-failed first attempt",
                    "mock: check src/ output matches acceptance criteria",
                    "mock: re-run the verification command",
                ]
            )
            self.coverage_rate = 1.0 if passed else 0.0
            self.items = []
            # This mock rejection is standing in for the retry-ladder demo,
            # not a criteria-scoring judgement call — mark the gate red so
            # the orchestrator's dispute eligibility check (contract §6,
            # "gate failures are not disputable") correctly skips the
            # negotiation layer here without needing a real LLM client.
            self.objective_gate = {
                "command": "mock", "exit_code": 0 if passed else 1,
                "passed": passed, "output_tail": "",
            }

    def __init__(self, always_pass: bool = False, delay_s: float = 0.0):
        self.always_pass = always_pass
        self.delay_s = delay_s

    def verify(self, task: Task, handoff: Handoff):
        # Same demo-pacing hook as MockExecutor.execute — see there.
        if self.delay_s:
            time.sleep(self.delay_s)
        if not self.always_pass and task.attempt_count < 2:
            return self._Report(False, f"mock rejection on attempt {task.attempt_count}")
        return self._Report(True, "mock: all acceptance criteria satisfied")


def build_mock_orchestrator(run_root: str = "runs", delay_s: float = 0.0):
    """Factory used by BOTH main.py --mock and serve.py's mock runs.

    Constructs the same attribute set Orchestrator.__init__ would, but with
    fakes wired in for planner/executor/verifier/client, so no Settings and
    no DASHSCOPE_API_KEY are required. Returns a ready-to-drive Orchestrator
    (call .run_checklist(requirements) on it).

    ``delay_s`` (demo pacing, not a frozen contract addition): passed through
    to MockExecutor/MockVerifier so every execute()/verify() call sleeps that
    long before returning. Purely cosmetic — lets a mock run be slow enough
    to film (watch the status wall fill in) without touching any API. 0.0
    (the default) keeps the mock loop as fast as it always was, so existing
    callers and the test suite are unaffected.
    """
    from .orchestrator import Orchestrator

    settings = Settings(
        api_key="mock-key",
        base_url="http://mock.invalid",
        planner_model="mock-planner",
        executor_model="mock-executor",
        verifier_model="mock-verifier",
    )

    orch = Orchestrator.__new__(Orchestrator)
    orch.settings = settings
    orch.client = None
    orch.run_root = Path(run_root)
    orch.run_root.mkdir(parents=True, exist_ok=True)

    from .models import new_id
    orch.run_id = new_id("run")
    run_dir = orch.run_root / orch.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    orch.run_dir = run_dir

    orch.ledger = Ledger(db_path=str(run_dir / "ledger.db"))
    orch.workspace = Workspace(run_dir / "workspace")
    orch.dispatcher = Dispatcher(orch.ledger)

    orch.planner = MockPlanner()
    orch.executor = MockExecutor(delay_s=delay_s)
    orch.verifier = MockVerifier(delay_s=delay_s)
    # MockVerifier's rejections always report a red gate (see _Report), so
    # _dispute_eligible always short-circuits before touching the arbiter —
    # this Arbiter's client is never actually called, but the attribute must
    # exist since the orchestrator reads it unconditionally.
    orch.arbiter = Arbiter(None, settings.planner_model, orch.workspace)
    orch.disputed_task_ids = set()
    orch.events_path = run_dir / "events.jsonl"
    return orch
