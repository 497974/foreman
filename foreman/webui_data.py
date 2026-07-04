"""Read-only data-access layer for the local web console (serve.py).

Everything here is a plain function that takes a run directory (or a runs
root) and returns JSON-serializable dicts/lists. Nothing in this module opens
a socket, so it can be imported and unit-tested directly (see
tests/test_webui_api.py) without ever starting the HTTP server.

Reads are always "fresh": every call opens its own sqlite3 connection and
re-reads events.jsonl from disk. The ledger runs in WAL mode (see
foreman/ledger.py), which is exactly what makes it safe to read concurrently
while an Orchestrator thread is still writing to the same ledger.db.

Nothing here mutates the ledger or events.jsonl — this module is intentionally
one-directional (run artifacts -> UI-shaped JSON).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from .models import Task, TaskStatus

# Statuses that count as "the run is still going" for the `complete` flag
# mirrors Ledger.is_run_complete's own terminal set.
_TERMINAL_STATUSES = {TaskStatus.DONE.value, TaskStatus.ARCHIVED.value, TaskStatus.BLOCKED.value}


def _ledger_connect(ledger_db_path: Path) -> sqlite3.Connection:
    """Open a short-lived read connection to a run's ledger.db.

    A run in progress may not have written the db file yet (Orchestrator
    creates it a few lines into __init__), so callers must handle a missing
    file themselves; this helper assumes the path already exists.
    """
    conn = sqlite3.connect(f"file:{ledger_db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _counts_from_rows(rows: list[sqlite3.Row]) -> dict[str, int]:
    out = {s.value: 0 for s in TaskStatus}
    for row in rows:
        out[row["status"]] = out.get(row["status"], 0) + 1
    return out


def _is_complete(rows: list[sqlite3.Row]) -> bool:
    if not rows:
        return False
    return all(r["status"] in _TERMINAL_STATUSES for r in rows)


def list_runs(run_root: str | Path) -> list[dict]:
    """One summary row per run directory under ``run_root``, newest first.

    Directories without a ledger.db yet (a run whose Orchestrator is still
    inside __init__) are skipped rather than erroring — the UI's run picker
    should only list runs it can actually show data for.
    """
    root = Path(run_root)
    if not root.exists():
        return []

    out: list[dict] = []
    for run_dir in root.iterdir():
        if not run_dir.is_dir():
            continue
        db_path = run_dir / "ledger.db"
        if not db_path.exists():
            continue
        try:
            conn = _ledger_connect(db_path)
            try:
                run_row = conn.execute(
                    "SELECT run_id, requirements, created_at FROM runs ORDER BY created_at LIMIT 1"
                ).fetchone()
                task_rows = conn.execute("SELECT status FROM tasks").fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            # Ledger mid-write / not yet initialized — skip this run for now.
            continue

        counts = _counts_from_rows(task_rows)
        out.append(
            {
                "run_id": run_dir.name,
                "created_at": run_row["created_at"] if run_row else None,
                "requirements_preview": (run_row["requirements"][:200] if run_row and run_row["requirements"] else ""),
                "counts": counts,
                "total_tasks": len(task_rows),
                "complete": _is_complete(task_rows),
            }
        )

    out.sort(key=lambda r: (r["created_at"] or 0), reverse=True)
    return out


def _task_row_to_dict(row: sqlite3.Row) -> dict:
    task = Task.from_row(dict(row))
    return {
        "task_id": task.task_id,
        "title": task.title,
        "description": task.description,
        "acceptance_criteria": task.acceptance_criteria,
        "test_strategy": task.test_strategy,
        "role": task.role,
        "priority": task.priority,
        "complexity_score": task.complexity_score,
        "parents": task.parents,
        "files_touched": task.files_touched,
        "status": task.status.value,
        "attempt_count": task.attempt_count,
        "consecutive_failures": task.consecutive_failures,
        "last_error": task.last_error,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def get_run_detail(run_root: str | Path, run_id: str) -> Optional[dict]:
    """Full task list + counts for one run, or None if the run does not exist."""
    db_path = Path(run_root) / run_id / "ledger.db"
    if not db_path.exists():
        return None

    conn = _ledger_connect(db_path)
    try:
        task_rows = conn.execute("SELECT * FROM tasks ORDER BY created_at").fetchall()
        run_row = conn.execute(
            "SELECT run_id, requirements, created_at FROM runs ORDER BY created_at LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    tasks = [_task_row_to_dict(r) for r in task_rows]
    counts = _counts_from_rows(task_rows)
    return {
        "run_id": run_id,
        "requirements": run_row["requirements"] if run_row else "",
        "created_at": run_row["created_at"] if run_row else None,
        "tasks": tasks,
        "counts": counts,
        "total_tasks": len(tasks),
        "complete": _is_complete(task_rows),
    }


def get_task_detail(run_root: str | Path, run_id: str, task_id: str) -> Optional[dict]:
    """Task row plus its full attempt history (each with the verifier's verdict text)."""
    db_path = Path(run_root) / run_id / "ledger.db"
    if not db_path.exists():
        return None

    conn = _ledger_connect(db_path)
    try:
        task_row = conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if task_row is None:
            return None
        attempt_rows = conn.execute(
            "SELECT * FROM attempts WHERE task_id = ? ORDER BY started_at", (task_id,)
        ).fetchall()
    finally:
        conn.close()

    task = _task_row_to_dict(task_row)

    attempts = []
    for r in attempt_rows:
        summary = {}
        if r["summary"]:
            try:
                summary = json.loads(r["summary"])
            except (json.JSONDecodeError, TypeError):
                summary = {}
        attempts.append(
            {
                "attempt_id": r["attempt_id"],
                "attempt_no": r["attempt_no"],
                "worker_id": r["worker_id"],
                "outcome": r["outcome"],
                "verdict": r["verdict"],
                "started_at": r["started_at"],
                "ended_at": r["ended_at"],
                "handoff": summary,
            }
        )

    task["attempts"] = attempts
    return task


def read_events(run_root: str | Path, run_id: str, after: int = 0) -> Optional[dict]:
    """Events strictly after index ``after`` (0-based line count already delivered).

    Returns {"events": [...], "next": after + len(events)} so the caller's next
    poll simply passes back ``next``. Returns None if the run directory does
    not exist at all (distinct from "exists but no events yet", which returns
    an empty events list with next == after).
    """
    run_dir = Path(run_root) / run_id
    events_path = run_dir / "events.jsonl"
    if not run_dir.exists():
        return None
    if not events_path.exists():
        return {"events": [], "next": after}

    events: list[dict] = []
    with open(events_path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i < after:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    return {"events": events, "next": after + len(events)}
