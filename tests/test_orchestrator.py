"""Orchestrator e2e tests. Run: pytest -q (no API key, no network).

The Orchestrator normally builds a real OpenAI-compatible client plus
Planner/Executor/Verifier bound to it. For these tests we construct the
object with Orchestrator.__new__ and wire in the same fakes main.py's
--mock mode uses, so the full claim -> execute -> submit -> verify ->
record_verdict loop runs against a real Ledger/Dispatcher/Workspace (the
deterministic core) with zero network access.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foreman.dispatcher import Dispatcher
from foreman.ledger import Ledger
from foreman.models import AttemptOutcome, Handoff, Task, TaskStatus
from foreman.orchestrator import Orchestrator
from foreman.workspace import Workspace


class FakePlanner:
    """Turns a tiny fixed checklist into a 3-task linear chain, no LLM."""

    def __init__(self, tasks):
        self._tasks = tasks

    def plan(self, requirements: str):
        return list(self._tasks)


class FakeExecutor:
    """Always succeeds; records how many times each task was executed."""

    def __init__(self):
        self.calls_per_task = {}

    def execute(self, task: Task, dependency_handoffs):
        self.calls_per_task[task.task_id] = self.calls_per_task.get(task.task_id, 0) + 1
        return Handoff(
            task_id=task.task_id,
            attempt_no=task.attempt_count,
            outcome=AttemptOutcome.SUCCESS.value,
            completed_work=[f"did {task.title}"],
            files_touched=[f"{task.task_id}.py"],
            interface_contract=[f"{task.task_id} exposes foo()"],
            handoff_reason=f"finished {task.task_id}",
        )


class FakeVerifierRejectsOnce:
    """Rejects the FIRST attempt of one specific task_id, passes everything
    else immediately — enough to exercise the retry ladder + feedback path."""

    def __init__(self, reject_task_id: str):
        self.reject_task_id = reject_task_id
        self.seen_last_error_at_attempt = {}

    def verify(self, task: Task, handoff: Handoff):
        from foreman.verifier import VerificationReport

        # Record what last_error looked like when this task was (re)submitted,
        # so the test can assert feedback reached the *next* attempt's task
        # card. We stash it keyed by attempt_count at verify-time.
        self.seen_last_error_at_attempt[task.attempt_count] = task.last_error

        if task.task_id == self.reject_task_id and task.attempt_count == 1:
            return VerificationReport(
                passed=False,
                coverage_rate=0.0,
                items=[{"criterion": "c1", "status": "not_satisfied", "detail": "missing foo()"}],
                objective_gate={"command": "pytest", "exit_code": 1, "passed": False, "output_tail": "AssertionError: foo undefined"},
                actionable_feedback=[
                    "file T2.py: expected function foo(), found nothing",
                    "re-run the test_strategy command before calling done",
                    "check the acceptance criteria list again",
                ],
                reason="0/1 criteria; gate exit=1",
            )
        return VerificationReport(
            passed=True,
            coverage_rate=1.0,
            items=[{"criterion": "c1", "status": "satisfied", "detail": "ok"}],
            objective_gate={"command": "pytest", "exit_code": 0, "passed": True, "output_tail": ""},
            actionable_feedback=[],
            reason="1/1 criteria; gate exit=0",
        )


def make_chain_tasks():
    return [
        Task(
            task_id="T1", title="scaffold", description="create scaffold",
            acceptance_criteria=["scaffold exists"], test_strategy="pytest -q",
        ),
        Task(
            task_id="T2", title="add feature", description="add feature depending on scaffold",
            acceptance_criteria=["feature works"], test_strategy="pytest -q",
            parents=["T1"],
        ),
        Task(
            task_id="T3", title="wire it together", description="wire T1+T2 together",
            acceptance_criteria=["wired"], test_strategy="pytest -q",
            parents=["T1", "T2"],
        ),
    ]


def build_mock_orchestrator(tmp_path: Path, tasks, executor, verifier):
    """Construct an Orchestrator without touching config.make_client/network."""
    orch = Orchestrator.__new__(Orchestrator)
    orch.settings = None
    orch.client = None
    orch.run_root = tmp_path / "runs"
    orch.run_root.mkdir(parents=True, exist_ok=True)

    from foreman.models import new_id
    orch.run_id = new_id("run")
    run_dir = orch.run_root / orch.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    orch.run_dir = run_dir

    orch.ledger = Ledger(db_path=str(run_dir / "ledger.db"))
    orch.workspace = Workspace(run_dir / "workspace")
    orch.dispatcher = Dispatcher(orch.ledger)

    orch.planner = FakePlanner(tasks)
    orch.executor = executor
    orch.verifier = verifier
    orch.events_path = run_dir / "events.jsonl"
    return orch


# ---- tests ------------------------------------------------------------------


def test_all_tasks_reach_done_and_events_written(tmp_path):
    tasks = make_chain_tasks()
    executor = FakeExecutor()
    verifier = FakeVerifierRejectsOnce(reject_task_id="__none__")  # nothing rejected
    orch = build_mock_orchestrator(tmp_path, tasks, executor, verifier)

    summary = orch.run_checklist("fake requirements")

    assert summary["done"] == 3
    assert summary["blocked"] == 0
    assert summary["complete"] is True

    for t in orch.ledger.all_tasks():
        assert t.status == TaskStatus.DONE

    assert orch.events_path.exists()
    lines = orch.events_path.read_text(encoding="utf-8").strip().splitlines()
    assert lines, "events.jsonl should not be empty"
    events = [json.loads(l) for l in lines]
    types_seen = {e["type"] for e in events}
    assert "plan" in types_seen
    assert "claim" in types_seen
    assert "submit" in types_seen
    assert "verdict" in types_seen
    # every event has the required keys
    for e in events:
        assert "ts" in e and "type" in e and "task_id" in e and "detail" in e


def test_rejected_task_gets_feedback_in_last_error_before_second_attempt(tmp_path):
    tasks = make_chain_tasks()
    executor = FakeExecutor()
    verifier = FakeVerifierRejectsOnce(reject_task_id="T2")
    orch = build_mock_orchestrator(tmp_path, tasks, executor, verifier)

    summary = orch.run_checklist("fake requirements")

    # T2 should have been executed twice (rejected once, then passed).
    assert executor.calls_per_task["T2"] == 2
    assert summary["done"] == 3
    assert summary["attempts_per_task"]["T2"] == 2

    # At verify-time of T2's *second* attempt (attempt_count == 2), the task's
    # last_error must already carry the verifier's actionable feedback from
    # attempt 1 — this is the field the executor's task card renders as
    # "Verifier feedback from the previous attempt".
    last_error_seen_on_attempt_2 = verifier.seen_last_error_at_attempt.get(2)
    assert last_error_seen_on_attempt_2 is not None
    assert "0/1 criteria" in last_error_seen_on_attempt_2
    # first 3 actionable_feedback items should be folded into the reason
    assert "expected function foo()" in last_error_seen_on_attempt_2
    assert "re-run the test_strategy command" in last_error_seen_on_attempt_2

    # sanity: attempt 1 had no last_error yet (first attempt, nothing to carry)
    assert verifier.seen_last_error_at_attempt.get(1) in (None, "")


def test_ledger_db_and_workspace_created_under_run_dir(tmp_path):
    tasks = [Task(task_id="T1", title="solo", description="solo task",
                   acceptance_criteria=["done"], test_strategy="pytest -q")]
    executor = FakeExecutor()
    verifier = FakeVerifierRejectsOnce(reject_task_id="__none__")
    orch = build_mock_orchestrator(tmp_path, tasks, executor, verifier)

    orch.run_checklist("fake requirements")

    assert (orch.run_dir / "ledger.db").exists()
    assert (orch.run_dir / "workspace").is_dir()
    assert orch.events_path.parent == orch.run_dir
