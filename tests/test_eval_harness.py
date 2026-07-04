"""Evaluation harness tests. Run: pytest -q (no API key, no network).

Covers the four pieces the contract calls out as unit-testable without an
API key: TokenMeter accumulation math, Orchestrator.run_tasks skipping the
planner, the referee scoring a tmp workspace with one passing and one
failing test_strategy, and scripts/evaluate.py's Condition A synthetic task
construction (criteria = checklist lines).
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foreman.dispatcher import Dispatcher
from foreman.ledger import Ledger
from foreman.models import AttemptOutcome, Handoff, Task, TaskStatus
from foreman.orchestrator import Orchestrator
from foreman.telemetry import METER, TokenMeter
from foreman.workspace import Workspace


# ---- A. TokenMeter accumulation math -----------------------------------------


def test_token_meter_accumulates_per_model_and_grand_total():
    meter = TokenMeter()
    meter.record("qwen-max", 100, 50)
    meter.record("qwen-max", 20, 10)
    meter.record("qwen-plus", 5, 5)

    snap = meter.snapshot()
    assert snap["per_model"]["qwen-max"]["prompt_tokens"] == 120
    assert snap["per_model"]["qwen-max"]["completion_tokens"] == 60
    assert snap["per_model"]["qwen-max"]["total_tokens"] == 180
    assert snap["per_model"]["qwen-max"]["calls"] == 2

    assert snap["per_model"]["qwen-plus"]["total_tokens"] == 10
    assert snap["per_model"]["qwen-plus"]["calls"] == 1

    assert snap["grand_total"]["prompt_tokens"] == 125
    assert snap["grand_total"]["completion_tokens"] == 65
    assert snap["grand_total"]["total_tokens"] == 190
    assert snap["grand_total"]["calls"] == 3


def test_token_meter_reset_clears_everything():
    meter = TokenMeter()
    meter.record("qwen-max", 10, 10)
    meter.reset()
    snap = meter.snapshot()
    assert snap["per_model"] == {}
    assert snap["grand_total"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}


def test_token_meter_default_calls_is_one():
    meter = TokenMeter()
    meter.record("m", 1, 2)
    assert meter.snapshot()["per_model"]["m"]["calls"] == 1


def test_token_meter_handles_missing_usage_fields_gracefully():
    meter = TokenMeter()
    # None-ish inputs (mirrors calling code's getattr(..., 0) defaults) must
    # not raise or corrupt the totals.
    meter.record("m", 0, 0, calls=1)
    snap = meter.snapshot()
    assert snap["per_model"]["m"]["total_tokens"] == 0


def test_module_level_singleton_is_importable_and_usable():
    METER.reset()
    METER.record("qwen-plus", 3, 4)
    snap = METER.snapshot()
    assert snap["grand_total"]["total_tokens"] == 7
    METER.reset()  # leave global state clean for any other test module


# ---- B. Orchestrator.run_tasks skips the planner -----------------------------


class PlannerCallDetector:
    """A fake planner whose .plan() failing the test is the whole point:
    run_tasks must never call it."""

    def __init__(self):
        self.called = False

    def plan(self, requirements: str):
        self.called = True
        raise AssertionError("run_tasks must not call the planner")


class FakeExecutorAlwaysSucceeds:
    def __init__(self):
        self.calls = 0

    def execute(self, task: Task, dependency_handoffs):
        self.calls += 1
        return Handoff(
            task_id=task.task_id, attempt_no=task.attempt_count,
            outcome=AttemptOutcome.SUCCESS.value,
            completed_work=[f"did {task.title}"], files_touched=[f"{task.task_id}.py"],
            handoff_reason="done",
        )


class FakeVerifierAlwaysPasses:
    class _Report:
        passed = True
        reason = "1/1 criteria; gate exit=0"
        actionable_feedback = []
        coverage_rate = 1.0
        items = []
        objective_gate = {"command": "pytest", "exit_code": 0, "passed": True, "output_tail": ""}

    def verify(self, task, handoff):
        return self._Report()


def _build_bare_orchestrator(tmp_path: Path, executor, verifier, planner=None):
    """Same __new__ + manual wiring pattern tests/test_orchestrator.py uses,
    so run_tasks can be exercised without touching config.make_client."""
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

    orch.planner = planner
    orch.executor = executor
    orch.verifier = verifier

    from foreman.arbiter import Arbiter
    orch.arbiter = Arbiter(None, "mock-arbiter-model", orch.workspace)
    orch.disputed_task_ids = set()
    orch.events_path = run_dir / "events.jsonl"
    return orch


def _two_task_chain():
    return [
        Task(task_id="T01", title="scaffold", description="create scaffold",
             acceptance_criteria=["scaffold exists"], test_strategy="pytest -q"),
        Task(task_id="T02", title="add feature", description="add feature",
             acceptance_criteria=["feature works"], test_strategy="pytest -q", parents=["T01"]),
    ]


def test_run_tasks_queues_given_tasks_without_calling_planner(tmp_path):
    tasks = _two_task_chain()
    executor = FakeExecutorAlwaysSucceeds()
    verifier = FakeVerifierAlwaysPasses()
    planner = PlannerCallDetector()
    orch = _build_bare_orchestrator(tmp_path, executor, verifier, planner=planner)

    summary = orch.run_tasks("fake requirements", tasks)

    assert planner.called is False
    assert summary["done"] == 2
    assert summary["complete"] is True
    assert executor.calls == 2


def test_run_tasks_behaves_identically_to_run_checklist_tail(tmp_path):
    """run_checklist delegates to run_tasks after planning; both should
    produce the same shape of summary dict and drive tasks to DONE."""
    tasks = _two_task_chain()

    class FakePlannerReturnsFixed:
        def plan(self, requirements):
            return list(tasks)

    executor = FakeExecutorAlwaysSucceeds()
    verifier = FakeVerifierAlwaysPasses()
    orch = _build_bare_orchestrator(tmp_path, executor, verifier, planner=FakePlannerReturnsFixed())

    summary = orch.run_checklist("fake requirements")

    assert summary["done"] == 2
    assert summary["complete"] is True
    assert set(summary.keys()) >= {"run_id", "run_dir", "counts", "done", "blocked", "total_tasks", "claims", "elapsed_s", "complete"}

    events = orch.events_path.read_text(encoding="utf-8").strip().splitlines()
    assert any('"type": "plan"' in e for e in events)


def test_run_tasks_emits_no_plan_event(tmp_path):
    """A direct run_tasks() call (no planning happened) should not emit a
    'plan' event — only run_checklist emits that, before delegating."""
    tasks = _two_task_chain()
    executor = FakeExecutorAlwaysSucceeds()
    verifier = FakeVerifierAlwaysPasses()
    orch = _build_bare_orchestrator(tmp_path, executor, verifier, planner=PlannerCallDetector())

    orch.run_tasks("fake requirements", tasks)

    import json
    events = [json.loads(l) for l in orch.events_path.read_text(encoding="utf-8").strip().splitlines()]
    assert sum(1 for e in events if e["type"] == "plan") == 0


# ---- C. Referee scoring -------------------------------------------------------


def test_referee_scores_passing_and_failing_test_strategy(tmp_path):
    from scripts.referee import score_workspace

    ws_root = tmp_path / "ws"
    workspace = Workspace(ws_root)
    workspace.write_file("good.py", "def ok():\n    return 1\n")
    workspace.write_file("bad.py", "def broken():\n    return 1\n")

    exam_tasks = [
        {
            "task_id": "T01",
            "title": "passing task",
            "test_strategy": f'{sys.executable} -c "import good; assert good.ok() == 1"',
        },
        {
            "task_id": "T02",
            "title": "failing task",
            "test_strategy": f'{sys.executable} -c "import bad; assert bad.broken() == 2"',
        },
    ]

    report = score_workspace(workspace, exam_tasks)

    assert report.n_tasks == 2
    assert report.n_passed == 1
    verdicts = {v.task_id: v for v in report.task_verdicts}
    assert verdicts["T01"].passed is True
    assert verdicts["T02"].passed is False
    assert report.pytest_ran is True  # overall sweep always attempted


def test_referee_scores_missing_test_strategy_as_failure(tmp_path):
    from scripts.referee import score_workspace

    workspace = Workspace(tmp_path / "ws")
    exam_tasks = [{"task_id": "T01", "title": "no strategy", "test_strategy": ""}]

    report = score_workspace(workspace, exam_tasks)

    assert report.n_tasks == 1
    assert report.n_passed == 0
    assert report.task_verdicts[0].passed is False


def test_score_run_loads_exam_json_from_disk(tmp_path):
    import json as _json

    from scripts.referee import score_run

    ws_root = tmp_path / "ws"
    workspace = Workspace(ws_root)
    workspace.write_file("m.py", "def f():\n    return 42\n")

    exam = {"tasks": [{
        "task_id": "T01", "title": "t",
        "test_strategy": f'{sys.executable} -c "import m; assert m.f() == 42"',
    }]}
    exam_path = tmp_path / "exam.json"
    exam_path.write_text(_json.dumps(exam), encoding="utf-8")

    report = score_run(ws_root, exam_path)
    assert report.n_passed == 1


# ---- D. Condition A synthetic task construction -------------------------------


def test_condition_a_task_uses_full_checklist_as_description():
    from scripts.evaluate import build_condition_a_task

    requirements = (
        "# Expense tracker\n"
        "1. Create a Flask app skeleton.\n"
        "2. Add a SQLite Expense model.\n"
        "\n"
        "3. Add POST /expenses.\n"
    )
    task = build_condition_a_task(requirements)

    assert task.description == requirements
    assert task.test_strategy == ""  # no exam access for Condition A
    # comment lines and blank lines are excluded; list-marker prefix stripped
    assert task.acceptance_criteria == [
        "Create a Flask app skeleton.",
        "Add a SQLite Expense model.",
        "Add POST /expenses.",
    ]


def test_condition_a_task_criteria_count_matches_nonblank_noncomment_lines():
    from scripts.evaluate import build_condition_a_task

    requirements = "line one\nline two\n\n# a comment\nline three\n"
    task = build_condition_a_task(requirements)
    assert len(task.acceptance_criteria) == 3


def test_topo_order_respects_dependencies():
    from scripts.evaluate import topo_order

    tasks = [
        Task(task_id="T03", title="c", description="c", parents=["T01", "T02"]),
        Task(task_id="T01", title="a", description="a", parents=[]),
        Task(task_id="T02", title="b", description="b", parents=["T01"]),
    ]
    ordered = [t.task_id for t in topo_order(tasks)]
    assert ordered.index("T01") < ordered.index("T02") < ordered.index("T03")
