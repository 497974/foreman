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

from foreman.arbiter import Arbiter
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
    # No network client in these fakes; the Arbiter is wired with client=None
    # since the dispute flow is exercised by its own dedicated tests below
    # (with an explicit fake chat client), not by these base loop tests.
    from foreman.arbiter import Arbiter
    orch.arbiter = Arbiter(None, "mock-arbiter-model", orch.workspace)
    orch.disputed_task_ids = set()
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


# ---- dispute + arbitration (contract §6) ------------------------------------


class _Msg:
    def __init__(self, content: str):
        self.content = content


class _Choice:
    def __init__(self, content: str):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content: str):
        self.choices = [_Choice(content)]


class FakeChatCompletions:
    """Scripted client.chat.completions.create, canned responses in call order.

    Same pattern as tests/test_verifier.py's FakeClient. Used here to script
    the two chat_json calls the dispute flow makes per disputed rejection:
    first the executor's solicit_dispute, then the Arbiter's rule().
    """

    def __init__(self, canned: list[str]):
        self._canned = list(canned)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if not self._canned:
            raise AssertionError("FakeChatCompletions ran out of canned responses")
        return _Resp(self._canned.pop(0))


class FakeChat:
    def __init__(self, canned: list[str]):
        self.completions = FakeChatCompletions(canned)


class FakeNegotiationClient:
    def __init__(self, canned: list[str]):
        self.chat = FakeChat(canned)


class FakeSettings:
    """Just enough of foreman.config.Settings for _run_dispute_flow's read of
    settings.executor_model (the orchestrator's dispute flow does not need
    the other fields since the fake client ignores the `model` kwarg)."""

    executor_model = "mock-executor"
    planner_model = "mock-planner"


class FakeVerifierOneCriteriaRejection:
    """Rejects a task's first attempt on criteria ONLY (gate green) — this is
    the one rejection shape contract §6 calls disputable. Passes on the
    second attempt so the run can still complete after an uphold ruling."""

    def __init__(self, reject_task_id: str):
        self.reject_task_id = reject_task_id

    def verify(self, task: Task, handoff: Handoff):
        from foreman.verifier import VerificationReport

        if task.task_id == self.reject_task_id and task.attempt_count == 1:
            return VerificationReport(
                passed=False,
                coverage_rate=0.0,
                items=[{"criterion": "c1", "status": "not_satisfied", "detail": "missing foo()"}],
                objective_gate={"command": "pytest", "exit_code": 0, "passed": True, "output_tail": ""},
                actionable_feedback=["utils.py: expected foo(), found nothing"],
                reason="0/1 criteria; gate exit=0",
            )
        return VerificationReport(
            passed=True,
            coverage_rate=1.0,
            items=[{"criterion": "c1", "status": "satisfied", "detail": "ok"}],
            objective_gate={"command": "pytest", "exit_code": 0, "passed": True, "output_tail": ""},
            actionable_feedback=[],
            reason="1/1 criteria; gate exit=0",
        )


def build_mock_orchestrator_with_client(tmp_path: Path, tasks, executor, verifier, client, settings=None):
    """Same as build_mock_orchestrator but wires a real (fake) chat client and
    Arbiter so the dispute flow's chat_json calls actually go somewhere."""
    orch = build_mock_orchestrator(tmp_path, tasks, executor, verifier)
    orch.client = client
    orch.settings = settings or FakeSettings()
    orch.arbiter = Arbiter(client, orch.settings.planner_model, orch.workspace)
    return orch


def _one_task(task_id="T1"):
    return [Task(task_id=task_id, title="add foo()", description="expose foo()",
                  acceptance_criteria=["foo() exists"], test_strategy="pytest -q")]


def test_dispute_concede_leaves_reject_unchanged(tmp_path):
    """dispute=false: executor concedes, verdict stays REJECT unchanged."""
    tasks = _one_task("T1")
    executor = FakeExecutor()
    verifier = FakeVerifierOneCriteriaRejection(reject_task_id="T1")
    canned = ['{"dispute": false, "rebuttal": "", "evidence": []}']
    client = FakeNegotiationClient(canned)
    orch = build_mock_orchestrator_with_client(tmp_path, tasks, executor, verifier, client)

    orch.run_checklist("fake requirements")

    events = [json.loads(l) for l in orch.events_path.read_text(encoding="utf-8").strip().splitlines()]
    types_seen = {e["type"] for e in events}
    assert "dispute" not in types_seen  # concede never raises an actual dispute event
    assert "arbitration" not in types_seen
    # T1 still ends up DONE (verifier passes attempt 2), but attempt 1's
    # verdict was recorded as a straight reject with the original reason.
    verdict_events = [e for e in events if e["type"] == "verdict" and e["task_id"] == "T1"]
    assert verdict_events[0]["detail"]["passed"] is False
    # unchanged means "same as _feedback_reason(report) would have produced
    # without any dispute", not literally the verifier's bare one-liner.
    assert verdict_events[0]["detail"]["reason"].startswith("0/1 criteria; gate exit=0")
    assert "arbiter" not in verdict_events[0]["detail"]["reason"]


def test_dispute_overturn_records_pass(tmp_path):
    tasks = _one_task("T1")
    executor = FakeExecutor()
    verifier = FakeVerifierOneCriteriaRejection(reject_task_id="T1")
    canned = [
        '{"dispute": true, "rebuttal": "foo() is right there", '
        '"evidence": [{"file": "T1.py", "claim": "defines foo()"}]}',
        '{"ruling": "overturn", "reasoning": "T1.py clearly defines foo()", "criteria_clarification": ""}',
    ]
    client = FakeNegotiationClient(canned)
    orch = build_mock_orchestrator_with_client(tmp_path, tasks, executor, verifier, client)
    orch.workspace.write_file("T1.py", "def foo():\n    return 1\n")

    summary = orch.run_checklist("fake requirements")

    assert summary["done"] == 1
    task = orch.ledger.get_task("T1")
    assert task.status == TaskStatus.DONE
    # only one attempt needed: attempt 1 was overturned to a pass directly.
    assert task.attempt_count == 1

    events = [json.loads(l) for l in orch.events_path.read_text(encoding="utf-8").strip().splitlines()]
    types_seen = {e["type"] for e in events}
    assert "dispute" in types_seen
    assert "arbitration" in types_seen
    verdict = next(e for e in events if e["type"] == "verdict" and e["task_id"] == "T1")
    assert verdict["detail"]["passed"] is True
    assert "arbiter overturned" in verdict["detail"]["reason"]


def test_dispute_uphold_appends_clarification_to_reason(tmp_path):
    tasks = _one_task("T1")
    executor = FakeExecutor()
    verifier = FakeVerifierOneCriteriaRejection(reject_task_id="T1")
    canned = [
        '{"dispute": true, "rebuttal": "foo() is right there", '
        '"evidence": [{"file": "T1.py", "claim": "defines foo()"}]}',
        '{"ruling": "uphold", "reasoning": "T1.py has no foo()", '
        '"criteria_clarification": "add a function literally named foo()"}',
    ]
    client = FakeNegotiationClient(canned)
    orch = build_mock_orchestrator_with_client(tmp_path, tasks, executor, verifier, client)
    orch.workspace.write_file("T1.py", "def bar():\n    return 1\n")

    orch.run_checklist("fake requirements")

    events = [json.loads(l) for l in orch.events_path.read_text(encoding="utf-8").strip().splitlines()]
    verdict = next(e for e in events if e["type"] == "verdict" and e["task_id"] == "T1")
    assert verdict["detail"]["passed"] is False
    assert "arbiter upheld: add a function literally named foo()" in verdict["detail"]["reason"]

    # the clarification must reach last_error so the next attempt's task card
    # sees it (contract §6 point 3) — task attempt 2 will have been executed.
    task = orch.ledger.get_task("T1")
    assert task.attempt_count == 2
    assert task.status == TaskStatus.DONE  # attempt 2 passes per FakeVerifierOneCriteriaRejection


def test_gate_failure_rejection_never_triggers_dispute(tmp_path):
    """A rejection where the objective gate failed must skip the negotiation
    layer entirely — no chat_json call is made at all (FakeNegotiationClient
    with zero canned responses will raise if anything tries to call it)."""
    tasks = _one_task("T1")
    executor = FakeExecutor()
    verifier = FakeVerifierRejectsOnce(reject_task_id="T1")  # gate exit=1 rejection
    client = FakeNegotiationClient([])  # no canned responses: any call fails the test
    orch = build_mock_orchestrator_with_client(tmp_path, tasks, executor, verifier, client)

    summary = orch.run_checklist("fake requirements")

    assert summary["done"] == 1
    events = [json.loads(l) for l in orch.events_path.read_text(encoding="utf-8").strip().splitlines()]
    types_seen = {e["type"] for e in events}
    assert "dispute" not in types_seen
    assert "arbitration" not in types_seen
    assert client.chat.completions.calls == 0


def test_second_rejection_of_same_task_cannot_dispute_again(tmp_path):
    """One dispute per task per run: once a task has used its appeal (uphold),
    a later rejection of the SAME task must not dispute again — it should
    fall straight through to a plain reject with no further chat_json calls."""

    class FakeVerifierTwoCriteriaRejections:
        """Rejects T1's first TWO attempts on criteria only (gate green),
        passes the third — enough to prove the second rejection doesn't
        dispute again after the first one already used its appeal."""

        def verify(self, task: Task, handoff: Handoff):
            from foreman.verifier import VerificationReport

            if task.attempt_count < 3:
                return VerificationReport(
                    passed=False, coverage_rate=0.0,
                    items=[{"criterion": "c1", "status": "not_satisfied", "detail": "still missing"}],
                    objective_gate={"command": "pytest", "exit_code": 0, "passed": True, "output_tail": ""},
                    actionable_feedback=["still missing foo()"],
                    reason=f"0/1 criteria; gate exit=0 (attempt {task.attempt_count})",
                )
            return VerificationReport(
                passed=True, coverage_rate=1.0,
                items=[{"criterion": "c1", "status": "satisfied", "detail": "ok"}],
                objective_gate={"command": "pytest", "exit_code": 0, "passed": True, "output_tail": ""},
                actionable_feedback=[], reason="1/1 criteria; gate exit=0",
            )

    tasks = _one_task("T1")
    executor = FakeExecutor()
    verifier = FakeVerifierTwoCriteriaRejections()
    # Only ONE dispute+ruling pair is canned. If the second rejection tried to
    # dispute again, FakeNegotiationClient would raise "ran out of canned
    # responses" and fail the test.
    canned = [
        '{"dispute": true, "rebuttal": "r1", "evidence": [{"file": "T1.py", "claim": "c"}]}',
        '{"ruling": "uphold", "reasoning": "still missing", "criteria_clarification": "add foo()"}',
    ]
    client = FakeNegotiationClient(canned)
    orch = build_mock_orchestrator_with_client(tmp_path, tasks, executor, verifier, client)
    orch.workspace.write_file("T1.py", "def bar(): pass\n")

    summary = orch.run_checklist("fake requirements")

    assert summary["done"] == 1
    assert client.chat.completions.calls == 2  # exactly one dispute + one ruling, never more
    assert "T1" in orch.disputed_task_ids


# ---- resume (contract §7) ----------------------------------------------------


def test_resume_run_revives_blocked_task_and_completes(tmp_path):
    """Build a ledger with one task already BLOCKED (retries exhausted) plus
    one already DONE, then resume_run() should revive the blocked task,
    re-execute it, and bring the run to completion — with no re-planning
    (FakePlanner is never consulted again after the initial add_tasks call
    that seeded this fixture)."""
    tasks = [
        Task(task_id="T1", title="scaffold", description="create scaffold",
             acceptance_criteria=["scaffold exists"], test_strategy="pytest -q"),
        Task(task_id="T2", title="add feature", description="add feature",
             acceptance_criteria=["feature works"], test_strategy="pytest -q", parents=["T1"]),
    ]
    executor = FakeExecutor()
    verifier = FakeVerifierRejectsOnce(reject_task_id="__none__")
    orch = build_mock_orchestrator(tmp_path, tasks, executor, verifier)

    # Seed the ledger directly to simulate a prior process that ran T1 to
    # DONE and then blocked T2 after exhausting its retries — rather than
    # replaying a full failing run just to reach that state. Tasks are added
    # via the ledger's own public API and driven through claim/submit/verdict
    # exactly like a real run would, so the fixture matches production state
    # transitions exactly.
    orch.ledger.add_tasks(tasks)
    orch.ledger.recompute_ready()

    t1 = orch.ledger.claim_next("w1")
    assert t1.task_id == "T1"
    h1 = executor.execute(t1, [])
    orch.ledger.submit_for_review(t1.task_id, "w1", h1)
    orch.ledger.record_verdict(t1.task_id, passed=True, reason="ok")

    orch.ledger.recompute_ready()
    t2 = orch.ledger.claim_next("w1")
    assert t2.task_id == "T2"
    # Exhaust T2's retries (max_attempts default is 3) so it lands BLOCKED.
    for _ in range(orch.ledger.max_attempts):
        h2 = executor.execute(t2, [])
        orch.ledger.submit_for_review(t2.task_id, "w1", h2)
        status = orch.ledger.record_verdict(t2.task_id, passed=False, reason="kept failing")
        if status == TaskStatus.BLOCKED:
            break
        orch.ledger.recompute_ready()
        t2 = orch.ledger.claim_next("w1")

    assert orch.ledger.get_task("T2").status == TaskStatus.BLOCKED

    # Now resume: T2 should be revived (BLOCKED -> PENDING -> READY) and this
    # time the always-passing verifier lets it complete the run.
    summary = orch.resume_run(orch.run_id)

    assert summary["complete"] is True
    assert orch.ledger.get_task("T2").status == TaskStatus.DONE
    assert orch.ledger.get_task("T2").attempt_count == 1  # reset_attempts=True on revive

    events = [json.loads(l) for l in orch.events_path.read_text(encoding="utf-8").strip().splitlines()]
    types_seen = {e["type"] for e in events}
    assert "revive" in types_seen
    # no second "plan" event: resume must not re-plan.
    assert sum(1 for e in events if e["type"] == "plan") == 0
