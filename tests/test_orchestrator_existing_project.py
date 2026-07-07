"""Tests for existing-project mode (contract Addendum 4 §14): Orchestrator's
project_dir/force_dirty wiring, the commit-after-DONE hook, and resume_run's
self-derivation of project_dir/branch from project_mode.json.

Exercises the REAL Orchestrator.__init__ (not Orchestrator.__new__ + manual
attribute wiring like tests/test_orchestrator.py) so the actual git_safety
wiring code path runs, but with make_client/Planner/Executor/Verifier/Arbiter
monkeypatched to fakes so no network or API key is ever touched — this is the
same fake-client/mock pattern tests/test_orchestrator.py and foreman/mocks.py
already use, just injected at the construction seam instead of via __new__.

A real git repo is built under tmp_path (git init, one commit, one existing
test file) using the actual git binary via subprocess, matching
tests/test_git_safety.py's helpers.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import foreman.orchestrator as orchestrator_module
from foreman.models import AttemptOutcome, Handoff, Task, TaskStatus
from foreman.orchestrator import Orchestrator


def _git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True, encoding="utf-8", errors="replace", check=True,
    )


def _init_repo_with_commit(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.name", "Foreman Test")
    _git(path, "config", "user.email", "test@foreman.local")
    (path / "README.md").write_text("# Demo project\n", encoding="utf-8")
    (path / "test_existing.py").write_text(
        "def test_existing():\n    assert True\n", encoding="utf-8"
    )
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "initial commit")
    return path


def _current_branch(path):
    return _git(path, "branch", "--show-current").stdout.strip()


# ---- fakes (same pattern as tests/test_orchestrator.py) ----------------------


class FakePlanner:
    def __init__(self, client, model):
        self.client = client
        self.model = model
        self.seen_repo_context = None

    def plan(self, requirements, repo_context: str = ""):
        self.seen_repo_context = repo_context
        return [
            Task(
                task_id="T1", title="add a function", description="add foo() to util.py",
                acceptance_criteria=["foo() exists"], test_strategy="pytest -q",
            ),
        ]


class FakeExecutor:
    """Mimics foreman.executor.Executor's constructor signature (including
    existing_project) but always succeeds without touching any client."""

    def __init__(self, client, model, workspace, existing_project: bool = False, **kwargs):
        self.client = client
        self.model = model
        self.workspace = workspace
        self.existing_project = existing_project
        self.fallback_models = []

    def execute(self, task, dependency_handoffs):
        self.workspace.write_file(f"{task.task_id.lower()}.py", "def foo():\n    return 1\n")
        return Handoff(
            task_id=task.task_id,
            attempt_no=task.attempt_count,
            outcome=AttemptOutcome.SUCCESS.value,
            completed_work=[f"did {task.title}"],
            files_touched=[f"{task.task_id.lower()}.py"],
            handoff_reason=f"finished {task.task_id}",
        )


class FakeVerifier:
    def __init__(self, client, model, workspace):
        self.workspace = workspace

    def verify(self, task, handoff):
        from foreman.verifier import VerificationReport

        return VerificationReport(
            passed=True,
            coverage_rate=1.0,
            items=[{"criterion": "c1", "status": "satisfied", "detail": "ok"}],
            objective_gate={"command": "pytest", "exit_code": 0, "passed": True, "output_tail": ""},
            actionable_feedback=[],
            reason="1/1 criteria; gate exit=0",
        )


class FakeArbiter:
    def __init__(self, client, model, workspace):
        self.workspace = workspace


class FakeSettings:
    planner_model = "mock-planner"
    executor_model = "mock-executor"
    verifier_model = "mock-verifier"
    fallback_models = []


@pytest.fixture(autouse=True)
def patch_orchestrator_seams(monkeypatch):
    """Replace every network-touching construction seam Orchestrator.__init__
    uses with fakes, so the test exercises the REAL __init__/git-safety wiring
    without ever needing DASHSCOPE_API_KEY or the network."""
    monkeypatch.setattr(orchestrator_module, "make_client", lambda settings: "fake-client")
    monkeypatch.setattr(orchestrator_module, "Planner", FakePlanner)
    monkeypatch.setattr(orchestrator_module, "Executor", FakeExecutor)
    monkeypatch.setattr(
        orchestrator_module,
        "make_executor",
        lambda settings, workspace, client, computer_mode=False: FakeExecutor(client, settings.executor_model, workspace),
    )
    monkeypatch.setattr(orchestrator_module, "Verifier", FakeVerifier)
    monkeypatch.setattr(orchestrator_module, "Arbiter", FakeArbiter)


def _build_orchestrator(tmp_path, project_dir=None, force_dirty=False):
    return Orchestrator(
        FakeSettings(),
        run_root=str(tmp_path / "runs"),
        project_dir=project_dir,
        force_dirty=force_dirty,
    )


# ---- tests ------------------------------------------------------------------


def test_existing_project_run_creates_branch_and_commits_only_there(tmp_path):
    repo = _init_repo_with_commit(tmp_path / "repo")
    original_branch = _current_branch(repo)

    orch = _build_orchestrator(tmp_path, project_dir=str(repo))
    expected_branch = f"foreman/{orch.run_id}"

    summary = orch.run_checklist("Add foo() to util.py")

    assert summary["done"] == 1
    assert summary["complete"] is True

    # The orchestrator's own working branch is the isolated foreman/<run_id>
    # branch — it must have switched there and stayed there.
    assert _current_branch(repo) == expected_branch

    # A commit exists per completed task on the foreman branch.
    log = _git(repo, "log", "--oneline", expected_branch).stdout
    assert "Foreman: T1" in log

    # The ORIGINAL branch has zero new commits — only the foreman branch does.
    original_log = _git(repo, "log", "--oneline", original_branch).stdout.strip().splitlines()
    assert len(original_log) == 1  # just the initial commit, nothing added

    # project_mode.json was written under the run_dir.
    project_mode_path = orch.run_dir / "project_mode.json"
    assert project_mode_path.is_file()
    data = json.loads(project_mode_path.read_text(encoding="utf-8"))
    assert data["branch"] == expected_branch
    assert Path(data["project_dir"]) == repo.resolve()

    # A "project_mode" event was emitted.
    events = [json.loads(l) for l in orch.events_path.read_text(encoding="utf-8").strip().splitlines()]
    types_seen = {e["type"] for e in events}
    assert "project_mode" in types_seen


def test_planner_receives_repo_context_snapshot(tmp_path):
    repo = _init_repo_with_commit(tmp_path / "repo")
    orch = _build_orchestrator(tmp_path, project_dir=str(repo))
    orch.run_checklist("Add foo() to util.py")
    assert orch.planner.seen_repo_context
    assert "README.md" in orch.planner.seen_repo_context
    assert "Demo project" in orch.planner.seen_repo_context


def test_executor_constructed_with_existing_project_true(tmp_path):
    repo = _init_repo_with_commit(tmp_path / "repo")
    orch = _build_orchestrator(tmp_path, project_dir=str(repo))
    assert orch.executor.existing_project is True


def test_greenfield_mode_unaffected_no_project_dir(tmp_path):
    """Sanity check: project_dir=None must behave exactly like before —
    workspace under run_dir, no project_mode.json, no branch juggling."""
    orch = _build_orchestrator(tmp_path, project_dir=None)
    summary = orch.run_checklist("Add foo() to util.py")

    assert summary["done"] == 1
    assert orch.project_dir is None
    assert not (orch.run_dir / "project_mode.json").exists()
    assert orch.workspace.root == (orch.run_dir / "workspace").resolve()
    assert orch.executor.existing_project is False


def test_dirty_repo_raises_git_safety_error_without_force_dirty(tmp_path):
    from foreman.git_safety import GitSafetyError

    repo = _init_repo_with_commit(tmp_path / "repo")
    (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

    with pytest.raises(GitSafetyError):
        _build_orchestrator(tmp_path, project_dir=str(repo))


def test_force_dirty_snapshots_preexisting_work_as_a_separate_commit(tmp_path):
    """Under --force-dirty the user's uncommitted work must land in its own
    clearly-labeled snapshot commit at the foreman branch tip — NOT be folded
    into the first AI-authored task checkpoint, where git blame/log would
    attribute the user's own code to Foreman's task."""
    repo = _init_repo_with_commit(tmp_path / "repo")
    (repo / "users_wip.py").write_text("user_work = True\n", encoding="utf-8")

    orch = _build_orchestrator(tmp_path, project_dir=str(repo), force_dirty=True)
    orch.run_checklist("Add foo() to util.py")

    log = _git(repo, "log", "--format=%s", f"foreman/{orch.run_id}").stdout.splitlines()
    # newest first: [task checkpoint, snapshot, initial commit]
    assert len(log) == 3
    assert "pre-existing" in log[1]
    assert "Foreman: T1" in log[0]

    # The snapshot commit contains the user's file; the task commit does not.
    snapshot_files = _git(repo, "show", "--name-only", "--format=", "HEAD~1").stdout
    assert "users_wip.py" in snapshot_files
    task_files = _git(repo, "show", "--name-only", "--format=", "HEAD").stdout
    assert "users_wip.py" not in task_files


def test_preexisting_foreman_branch_makes_init_refuse(tmp_path):
    """__init__ must refuse (GitSafetyError) if a branch named exactly
    foreman/<this run_id> already exists — Foreman never commits on top of a
    branch it did not create. Simulated by pre-creating branches for every
    plausible id via monkeypatching new_id would be brittle; instead exercise
    git_safety's contract through a direct second orchestrator scenario is
    impossible (fresh run_id each time), so pin the underlying refusal at the
    git_safety layer here with the orchestrator's exact branch-name shape."""
    from foreman.git_safety import GitSafetyError, create_or_checkout_branch

    repo = _init_repo_with_commit(tmp_path / "repo")
    _git(repo, "branch", "foreman/run_deadbeef")
    with pytest.raises(GitSafetyError, match="already exists"):
        create_or_checkout_branch(repo, "foreman/run_deadbeef")


def test_two_concurrent_runs_on_same_repo_second_is_refused(tmp_path):
    """Two live Orchestrators against one repo would race checkout/commit on
    a single shared working tree; the second construction must fail loudly."""
    from foreman.git_safety import GitSafetyError

    repo = _init_repo_with_commit(tmp_path / "repo")
    first = _build_orchestrator(tmp_path, project_dir=str(repo))
    assert first.project_dir is not None  # holds the lock now
    with pytest.raises(GitSafetyError, match="another Foreman run"):
        _build_orchestrator(tmp_path, project_dir=str(repo))


def test_run_root_inside_project_dir_is_refused_with_accurate_message(tmp_path):
    """--run-root inside the repo would sweep Foreman's ledger.db into the
    user's checkpoint commits via `git add -A`. Must fail naming the actual
    cause (run directory location), not the dirty-tree symptom that creating
    run_dir causes."""
    from foreman.git_safety import GitSafetyError

    repo = _init_repo_with_commit(tmp_path / "repo")
    with pytest.raises(GitSafetyError, match="run directory"):
        Orchestrator(
            FakeSettings(),
            run_root=str(repo / "runs"),
            project_dir=str(repo),
        )


def test_resume_run_rederives_project_dir_and_branch(tmp_path):
    """After a first pass with project_dir set, a resume_run() call on a
    freshly-constructed (greenfield-looking) Orchestrator must self-derive
    project_dir/branch from project_mode.json without being told again."""
    repo = _init_repo_with_commit(tmp_path / "repo")

    orch = _build_orchestrator(tmp_path, project_dir=str(repo))
    run_id = orch.run_id
    run_dir = orch.run_dir
    expected_branch = f"foreman/{run_id}"

    # Seed a BLOCKED task directly (mirrors tests/test_orchestrator.py's
    # resume fixture pattern) so resume_run has something to revive, without
    # running run_checklist() first (which would already complete T1).
    blocked_task = Task(
        task_id="T2", title="add bar()", description="add bar() to util.py",
        acceptance_criteria=["bar() exists"], test_strategy="pytest -q",
    )
    orch.ledger.create_run("Add bar() to util.py")
    orch.ledger.add_tasks([blocked_task])
    orch.ledger.recompute_ready()
    claimed = orch.ledger.claim_next("w1")
    for _ in range(orch.ledger.max_attempts):
        h = orch.executor.execute(claimed, [])
        orch.ledger.submit_for_review(claimed.task_id, "w1", h)
        status = orch.ledger.record_verdict(claimed.task_id, passed=False, reason="kept failing")
        if status == TaskStatus.BLOCKED:
            break
        orch.ledger.recompute_ready()
        claimed = orch.ledger.claim_next("w1")
    assert orch.ledger.get_task("T2").status == TaskStatus.BLOCKED

    # Simulate a brand new process reopening this run: construct a fresh
    # Orchestrator via __new__ (mirrors main.py's _build_resume_orchestrator)
    # WITHOUT telling it about project_dir at all, pointed at a plain
    # run_dir/workspace like a greenfield run would be.
    from foreman.dispatcher import Dispatcher
    from foreman.ledger import Ledger
    from foreman.workspace import Workspace

    fresh = Orchestrator.__new__(Orchestrator)
    fresh.settings = FakeSettings()
    fresh.client = "fake-client"
    fresh.run_root = run_dir.parent
    fresh.run_id = run_id
    fresh.run_dir = run_dir
    fresh.ledger = Ledger(db_path=str(run_dir / "ledger.db"))
    fresh.workspace = Workspace(run_dir / "workspace")  # greenfield-looking default
    fresh.dispatcher = Dispatcher(fresh.ledger)
    fresh.planner = FakePlanner("fake-client", "mock-planner")
    fresh.executor = FakeExecutor("fake-client", "mock-executor", fresh.workspace)
    fresh.verifier = FakeVerifier("fake-client", "mock-verifier", fresh.workspace)
    fresh.arbiter = FakeArbiter("fake-client", "mock-planner", fresh.workspace)
    fresh.events_path = run_dir / "events.jsonl"
    fresh.disputed_task_ids = set()
    fresh.project_dir = None
    fresh.project_branch = None
    fresh._repo_context = ""

    summary = fresh.resume_run(run_id)

    assert summary["complete"] is True
    assert fresh.project_dir == repo.resolve()
    assert fresh.project_branch == expected_branch
    # workspace got repointed at the real repo, not run_dir/workspace
    assert fresh.workspace.root == repo.resolve()
    assert fresh.executor.workspace is fresh.workspace
    assert _current_branch(repo) == expected_branch
    assert orch.ledger.get_task("T2").status == TaskStatus.DONE


def test_resume_heals_task_stranded_in_pending_review(tmp_path):
    """Found live: a network error killed the verifier's LLM call AFTER
    submit_for_review but BEFORE record_verdict — the task froze in
    PENDING_REVIEW with no claim to expire, and a resumed run stalled forever
    with 0 claims. resume_run must finish the interrupted step: re-verify the
    durably-stored handoff and record a real verdict."""
    orch = _build_orchestrator(tmp_path, project_dir=None)

    stranded = Task(
        task_id="T9", title="stranded task", description="was mid-verification",
        acceptance_criteria=["it works"], test_strategy="pytest -q",
    )
    orch.ledger.create_run("heal test")
    orch.ledger.add_tasks([stranded])
    orch.ledger.recompute_ready()
    claimed = orch.ledger.claim_next("w1")
    assert claimed.task_id == "T9"
    handoff = orch.executor.execute(claimed, [])
    orch.ledger.submit_for_review("T9", "w1", handoff)
    # simulate the crash: NO record_verdict — task is now PENDING_REVIEW
    assert orch.ledger.get_task("T9").status == TaskStatus.PENDING_REVIEW

    summary = orch.resume_run(orch.run_id)

    # FakeVerifier passes everything, so the healed task must be DONE and
    # the run complete — without the healing, this stalls at 0 claims with
    # T9 still PENDING_REVIEW forever.
    assert orch.ledger.get_task("T9").status == TaskStatus.DONE
    assert summary["complete"] is True

    events = [json.loads(l) for l in orch.events_path.read_text(encoding="utf-8").strip().splitlines()]
    assert any(e["type"] == "reverify" and e["task_id"] == "T9" for e in events)
