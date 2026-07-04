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
from typing import Callable, Optional

from .models import Task, TaskStatus
from .pricing import estimate_usd
from .telemetry import METER

# Statuses that count as "the run is still going" for the `complete` flag
# mirrors Ledger.is_run_complete's own terminal set.
_TERMINAL_STATUSES = {TaskStatus.DONE.value, TaskStatus.ARCHIVED.value, TaskStatus.BLOCKED.value}

# Archived runs (contract §9.6 DELETE /api/runs/<id>) live under this
# subdirectory of run_root; list_runs must never surface them.
ARCHIVED_DIRNAME = "_archived"


def _read_config(run_dir: Path) -> Optional[dict]:
    """Read runs/<id>/config.json (contract §9.4), or None if absent/corrupt.

    Written by serve.py at run start and merged with usage_final/est_usd when
    the run finishes/stops (see serve.py's worker wrapper). A run started via
    the CLI (main.py, no serve.py involved) simply has no config.json — that
    is a normal, expected case, not an error.
    """
    config_path = run_dir / "config.json"
    if not config_path.exists():
        return None
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _usage_and_cost(run_id: str, run_dir: Path, config: Optional[dict]) -> tuple[dict, Optional[float]]:
    """Live METER usage if the run has recorded anything under this run_id,
    otherwise fall back to the persisted "usage_final" in config.json
    (contract §9.4/§9.6: ``usage`` = live totals if nonzero else usage_final).
    Returns (usage_dict, est_usd) — est_usd is None when there is no usage at
    all yet (fresh run, nothing recorded).
    """
    live = METER.run_totals(run_id)
    if live["totals"]["calls"] > 0:
        usage = live
    elif config and config.get("usage_final"):
        usage = config["usage_final"]
    else:
        usage = {"per_model": {}, "totals": {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}}

    per_model = usage.get("per_model", {})
    if per_model:
        est_usd = estimate_usd(per_model)
    elif config and config.get("est_usd") is not None:
        est_usd = config["est_usd"]
    else:
        est_usd = None
    return usage, est_usd


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


def list_runs(run_root: str | Path, is_active: Optional[Callable[[str], bool]] = None) -> list[dict]:
    """One summary row per run directory under ``run_root``, newest first.

    Directories without a ledger.db yet (a run whose Orchestrator is still
    inside __init__) are skipped rather than erroring — the UI's run picker
    should only list runs it can actually show data for. Archived runs
    (``_archived/<id>``, see the DELETE endpoint) are never surfaced here —
    that whole subdirectory is skipped like any other non-run directory.

    ``is_active`` is an optional callable (run_id -> bool) supplied by
    serve.py's ``_active_runs`` registry (contract §9.6: "active": thread
    alive). Kept optional / defaulting to "always False" so this module stays
    importable and testable with zero knowledge of serve.py's threading.
    """
    root = Path(run_root)
    if not root.exists():
        return []

    active_check = is_active or (lambda _run_id: False)

    out: list[dict] = []
    for run_dir in root.iterdir():
        if not run_dir.is_dir():
            continue
        if run_dir.name == ARCHIVED_DIRNAME:
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
        config = _read_config(run_dir)
        _usage, est_usd = _usage_and_cost(run_dir.name, run_dir, config)
        out.append(
            {
                "run_id": run_dir.name,
                "created_at": run_row["created_at"] if run_row else None,
                "requirements_preview": (run_row["requirements"][:200] if run_row and run_row["requirements"] else ""),
                "counts": counts,
                "total_tasks": len(task_rows),
                "complete": _is_complete(task_rows),
                "active": bool(active_check(run_dir.name)),
                "mock": bool(config.get("mock")) if config else False,
                "est_usd": est_usd,
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


def get_run_detail(
    run_root: str | Path, run_id: str, is_active: Optional[Callable[[str], bool]] = None
) -> Optional[dict]:
    """Full task list + counts for one run, or None if the run does not exist.

    Adds (contract §9.4): ``config`` (the parsed runs/<id>/config.json, or
    None if the run has none — e.g. a CLI-only run), ``usage`` (live
    METER.run_totals if the run has recorded anything under this run_id this
    process's lifetime, else the persisted usage_final), and ``est_usd``
    (estimated from whichever usage source was used).
    """
    run_dir = Path(run_root) / run_id
    db_path = run_dir / "ledger.db"
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
    config = _read_config(run_dir)
    usage, est_usd = _usage_and_cost(run_id, run_dir, config)

    active_check = is_active or (lambda _run_id: False)

    return {
        "run_id": run_id,
        "requirements": run_row["requirements"] if run_row else "",
        "created_at": run_row["created_at"] if run_row else None,
        "tasks": tasks,
        "counts": counts,
        "total_tasks": len(tasks),
        "complete": _is_complete(task_rows),
        "active": bool(active_check(run_id)),
        "config": config,
        "usage": usage,
        "est_usd": est_usd,
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
