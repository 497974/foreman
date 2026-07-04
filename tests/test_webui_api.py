"""Web console data-access tests. Run: pytest -q (no HTTP server, no API key).

These exercise foreman.webui_data directly against a tiny ledger fixture built
with foreman.ledger + foreman.models, exactly the way the rest of the suite
builds ledgers (see tests/test_ledger.py). serve.py itself is a thin wiring
layer over these functions and is covered instead by the manual smoke test in
the task description (curl / and /api/runs against a live process).
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foreman import webui_data
from foreman.ledger import Ledger
from foreman.models import Handoff, Task, TaskStatus


def make_task(tid: str, parents=None, **kw) -> Task:
    return Task(
        task_id=tid,
        title=f"Task {tid}: do the thing",
        description="do the thing, fully",
        acceptance_criteria=["it works", "it is tested"],
        test_strategy="pytest -q",
        parents=parents or [],
        **kw,
    )


def build_run_dir(tmp_path: Path, run_id: str = "run_test1234") -> Path:
    """Build a run directory with a real ledger.db + events.jsonl, no sockets."""
    run_root = tmp_path / "runs"
    run_dir = run_root / run_id
    run_dir.mkdir(parents=True)

    ledger = Ledger(db_path=str(run_dir / "ledger.db"))
    ledger.create_run("1. build the thing\n2. test the thing\n")

    ledger.add_task(make_task("T1"))
    ledger.add_task(make_task("T2", parents=["T1"]))
    ledger.recompute_ready()

    # T1: rejected once, then passes.
    t = ledger.claim_next("w1")
    assert t.task_id == "T1"
    ledger.submit_for_review("T1", "w1", Handoff(task_id="T1", attempt_no=1, completed_work=["draft"]))
    ledger.record_verdict("T1", passed=False, reason="0/2 criteria; gate exit=1")

    t = ledger.claim_next("w1")
    assert t.attempt_count == 2
    ledger.submit_for_review("T1", "w1", Handoff(task_id="T1", attempt_no=2, completed_work=["final"]))
    ledger.record_verdict("T1", passed=True, reason="2/2 criteria; gate exit=0")

    ledger.recompute_ready()

    # T2: claimed but not yet submitted (in_progress) -- run "still going".
    ledger.claim_next("w1")

    events_path = run_dir / "events.jsonl"
    events = [
        {"ts": 1.0, "type": "plan", "task_id": "", "detail": {"n_tasks": 2, "task_ids": ["T1", "T2"]}},
        {"ts": 2.0, "type": "claim", "task_id": "T1", "detail": {"attempt": 1}},
        {"ts": 3.0, "type": "submit", "task_id": "T1", "detail": {"outcome": "rejected"}},
        {"ts": 4.0, "type": "verdict", "task_id": "T1", "detail": {"passed": False, "new_status": "ready"}},
        {"ts": 5.0, "type": "claim", "task_id": "T1", "detail": {"attempt": 2}},
        {"ts": 6.0, "type": "submit", "task_id": "T1", "detail": {"outcome": "success"}},
        {"ts": 7.0, "type": "verdict", "task_id": "T1", "detail": {"passed": True, "new_status": "done"}},
        {"ts": 8.0, "type": "claim", "task_id": "T2", "detail": {"attempt": 1}},
    ]
    with open(events_path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    return run_root


# ---- list_runs --------------------------------------------------------------


def test_list_runs_shape(tmp_path):
    run_root = build_run_dir(tmp_path)
    runs = webui_data.list_runs(run_root)

    assert len(runs) == 1
    r = runs[0]
    assert r["run_id"] == "run_test1234"
    assert r["total_tasks"] == 2
    assert r["counts"]["done"] == 1
    assert r["counts"]["in_progress"] == 1
    assert r["complete"] is False  # T2 still in_progress
    assert "created_at" in r
    assert isinstance(r["requirements_preview"], str)


def test_list_runs_empty_root_returns_empty_list(tmp_path):
    assert webui_data.list_runs(tmp_path / "nonexistent") == []


def test_list_runs_skips_dirs_without_ledger(tmp_path):
    run_root = tmp_path / "runs"
    (run_root / "not_a_run").mkdir(parents=True)
    assert webui_data.list_runs(run_root) == []


# ---- get_run_detail ----------------------------------------------------------


def test_run_detail_counts_and_tasks(tmp_path):
    run_root = build_run_dir(tmp_path)
    detail = webui_data.get_run_detail(run_root, "run_test1234")

    assert detail is not None
    assert detail["run_id"] == "run_test1234"
    assert detail["total_tasks"] == 2
    assert detail["counts"]["done"] == 1
    assert detail["counts"]["in_progress"] == 1
    assert detail["complete"] is False
    assert "build the thing" in detail["requirements"]

    by_id = {t["task_id"]: t for t in detail["tasks"]}
    assert by_id["T1"]["status"] == "done"
    assert by_id["T1"]["attempt_count"] == 2
    assert by_id["T2"]["status"] == "in_progress"
    assert by_id["T2"]["parents"] == ["T1"]
    assert by_id["T1"]["acceptance_criteria"] == ["it works", "it is tested"]


def test_run_detail_unknown_run_returns_none(tmp_path):
    run_root = build_run_dir(tmp_path)
    assert webui_data.get_run_detail(run_root, "run_does_not_exist") is None


# ---- events pagination --------------------------------------------------------


def test_events_pagination_after_zero_returns_all(tmp_path):
    run_root = build_run_dir(tmp_path)
    result = webui_data.read_events(run_root, "run_test1234", after=0)

    assert result is not None
    assert len(result["events"]) == 8
    assert result["next"] == 8
    assert result["events"][0]["type"] == "plan"
    assert result["events"][-1]["type"] == "claim"


def test_events_pagination_after_n_returns_only_new(tmp_path):
    run_root = build_run_dir(tmp_path)
    result = webui_data.read_events(run_root, "run_test1234", after=5)

    assert len(result["events"]) == 3
    assert result["next"] == 8
    assert [e["type"] for e in result["events"]] == ["submit", "verdict", "claim"]


def test_events_pagination_after_end_returns_empty(tmp_path):
    run_root = build_run_dir(tmp_path)
    result = webui_data.read_events(run_root, "run_test1234", after=8)

    assert result["events"] == []
    assert result["next"] == 8


def test_events_unknown_run_returns_none(tmp_path):
    run_root = build_run_dir(tmp_path)
    assert webui_data.read_events(run_root, "run_missing", after=0) is None


# ---- task detail with attempt history -----------------------------------------


def test_task_detail_includes_attempt_history(tmp_path):
    run_root = build_run_dir(tmp_path)
    task = webui_data.get_task_detail(run_root, "run_test1234", "T1")

    assert task is not None
    assert task["task_id"] == "T1"
    assert task["status"] == "done"
    assert len(task["attempts"]) == 2

    first, second = task["attempts"]
    assert first["outcome"] == "rejected_by_verifier"
    assert "0/2 criteria" in first["verdict"]
    assert first["handoff"]["completed_work"] == ["draft"]

    assert second["outcome"] == "success"
    assert "2/2 criteria" in second["verdict"]
    assert second["handoff"]["completed_work"] == ["final"]


def test_task_detail_unknown_task_returns_none(tmp_path):
    run_root = build_run_dir(tmp_path)
    assert webui_data.get_task_detail(run_root, "run_test1234", "T99") is None


def test_task_detail_no_attempts_yet(tmp_path):
    run_root = tmp_path / "runs"
    run_dir = run_root / "run_fresh"
    run_dir.mkdir(parents=True)
    ledger = Ledger(db_path=str(run_dir / "ledger.db"))
    ledger.create_run("do stuff")
    ledger.add_task(make_task("A"))

    task = webui_data.get_task_detail(run_root, "run_fresh", "A")
    assert task is not None
    assert task["status"] == "pending"
    assert task["attempts"] == []
