"""The Ledger: Foreman's durable memory spine.

Context windows rot; an external ledger does not. Every task, every attempt,
and the plan itself live in SQLite so the run survives a crash and resumes from
exactly where it stopped. The ledger is also the single source of truth the UI
renders and the evaluation harness reads — one store, three consumers.

Concurrency is enforced at the storage layer:
  * claims use a conditional UPDATE (compare-and-swap) so two executors can
    never hold the same task;
  * leases carry a TTL so a crashed worker's task is reclaimed automatically.

Only standard-library ``sqlite3`` is used, so the whole core runs without any
third-party install.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from .models import (
    ALLOWED_TRANSITIONS,
    AttemptOutcome,
    Handoff,
    Task,
    TaskStatus,
    is_transition_allowed,
    new_id,
    now_ts,
)

DEFAULT_MAX_ATTEMPTS = 3        # retries beyond this escalate (Reflexion: <2% gain)
DEFAULT_FAILURE_CEILING = 2     # consecutive failures that trip the circuit breaker


class TransitionError(RuntimeError):
    """Raised when code attempts an illegal task state transition."""


class Ledger:
    def __init__(self, db_path: str = ":memory:", max_attempts: int = DEFAULT_MAX_ATTEMPTS):
        self.db_path = db_path
        self.max_attempts = max_attempts
        # A single shared connection is used for :memory: (each connect() would
        # otherwise get a *different* empty database). File-backed databases get
        # a fresh connection per call for thread safety.
        self._shared = sqlite3.connect(db_path) if db_path == ":memory:" else None
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._create_schema(conn)

    # ---- connection plumbing ------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = self._shared or sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                requirements TEXT,
                plan_version INTEGER DEFAULT 1,
                created_at REAL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                title TEXT, description TEXT,
                acceptance_criteria TEXT, test_strategy TEXT,
                role TEXT, priority INTEGER, complexity_score INTEGER,
                parents TEXT, files_touched TEXT,
                status TEXT,
                claim_lock TEXT, claim_expires_at REAL,
                attempt_count INTEGER, consecutive_failures INTEGER,
                last_error TEXT, created_at REAL, updated_at REAL
            );
            -- append-only audit trail: one row per attempt, never mutated
            CREATE TABLE IF NOT EXISTS attempts (
                attempt_id TEXT PRIMARY KEY,
                task_id TEXT, attempt_no INTEGER, worker_id TEXT,
                outcome TEXT, summary TEXT, verdict TEXT,
                started_at REAL, ended_at REAL,
                FOREIGN KEY (task_id) REFERENCES tasks(task_id)
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            """
        )
        conn.commit()

    # ---- run + task creation ------------------------------------------------

    def create_run(self, requirements: str) -> str:
        run_id = new_id("run")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, requirements, plan_version, created_at) "
                "VALUES (?, ?, 1, ?)",
                (run_id, requirements, now_ts()),
            )
            conn.commit()
        return run_id

    def add_task(self, task: Task) -> None:
        row = task.to_row()
        cols = ", ".join(row.keys())
        placeholders = ", ".join("?" for _ in row)
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO tasks ({cols}) VALUES ({placeholders})",
                tuple(row.values()),
            )
            conn.commit()

    def add_tasks(self, tasks: list[Task]) -> None:
        for t in tasks:
            self.add_task(t)

    # ---- reads --------------------------------------------------------------

    def get_task(self, task_id: str) -> Optional[Task]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return Task.from_row(dict(row)) if row else None

    def all_tasks(self) -> list[Task]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at"
            ).fetchall()
        return [Task.from_row(dict(r)) for r in rows]

    def tasks_by_status(self, status: TaskStatus) -> list[Task]:
        return [t for t in self.all_tasks() if t.status == status]

    def counts(self) -> dict[str, int]:
        """Status histogram — feeds the status-wall UI and the ledger summary."""
        out = {s.value: 0 for s in TaskStatus}
        with self._connect() as conn:
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
            ):
                out[row["status"]] = row["n"]
        return out

    def is_run_complete(self) -> bool:
        """True when every task is terminal (done/archived) or permanently blocked."""
        terminal = {TaskStatus.DONE, TaskStatus.ARCHIVED, TaskStatus.BLOCKED}
        tasks = self.all_tasks()
        return bool(tasks) and all(t.status in terminal for t in tasks)

    # ---- dependency resolution ---------------------------------------------

    def recompute_ready(self) -> list[str]:
        """Promote PENDING tasks whose parents are all DONE to READY.

        Returns the ids that were promoted. This is one of the three automatic
        transitions — the dispatcher calls it each tick.
        """
        tasks = {t.task_id: t for t in self.all_tasks()}
        promoted: list[str] = []
        for t in tasks.values():
            if t.status != TaskStatus.PENDING:
                continue
            parents_done = all(
                tasks.get(p) is not None and tasks[p].status == TaskStatus.DONE
                for p in t.parents
            )
            if parents_done:
                self._set_status(t.task_id, TaskStatus.READY)
                promoted.append(t.task_id)
        return promoted

    # ---- the claim: atomic compare-and-swap --------------------------------

    def claim_next(
        self,
        worker_id: str,
        lease_seconds: float = 900.0,
        role: Optional[str] = None,
    ) -> Optional[Task]:
        """Atomically hand exactly one READY task to ``worker_id``.

        Picks the highest-priority ready task (optionally filtered by role) and
        flips it to IN_PROGRESS via a guarded UPDATE. If a concurrent worker won
        the race for that row, the UPDATE touches zero rows and we try the next
        candidate. Returns the claimed task, or None if nothing is available.
        """
        while True:
            with self._connect() as conn:
                params: list = []
                role_clause = ""
                if role is not None:
                    role_clause = "AND role = ?"
                    params.append(role)
                candidate = conn.execute(
                    f"""SELECT task_id FROM tasks
                        WHERE status = 'ready' AND claim_lock IS NULL {role_clause}
                        ORDER BY priority ASC, created_at ASC
                        LIMIT 1""",
                    params,
                ).fetchone()
                if candidate is None:
                    return None
                task_id = candidate["task_id"]
                ts = now_ts()
                # CAS: only succeeds if the row is *still* ready and unlocked.
                cur = conn.execute(
                    """UPDATE tasks
                       SET status = 'in_progress', claim_lock = ?,
                           claim_expires_at = ?, attempt_count = attempt_count + 1,
                           updated_at = ?
                       WHERE task_id = ? AND status = 'ready' AND claim_lock IS NULL""",
                    (worker_id, ts + lease_seconds, ts, task_id),
                )
                conn.commit()
                if cur.rowcount == 1:
                    return self.get_task(task_id)
                # lost the race for this row; loop and try the next candidate

    # ---- submit + verdict ---------------------------------------------------

    def submit_for_review(
        self, task_id: str, worker_id: str, handoff: Handoff
    ) -> None:
        """Executor finished an attempt: IN_PROGRESS -> PENDING_REVIEW."""
        task = self._require(task_id)
        if task.claim_lock != worker_id:
            raise TransitionError(
                f"{worker_id} cannot submit {task_id}: held by {task.claim_lock}"
            )
        self._set_status(task_id, TaskStatus.PENDING_REVIEW, clear_lock=True)
        self._record_attempt(task, worker_id, AttemptOutcome.SUCCESS, handoff)

    def record_verdict(
        self,
        task_id: str,
        passed: bool,
        reason: str = "",
        worker_id: str = "verifier",
    ) -> TaskStatus:
        """Verifier ruled on a submitted task and drives the retry ladder.

        pass                          -> DONE
        reject, attempts < ceiling    -> READY (requeue, +1 consecutive failure)
        reject, attempts >= ceiling   -> BLOCKED (escalate to the replanner)
        """
        task = self._require(task_id)
        if task.status != TaskStatus.PENDING_REVIEW:
            raise TransitionError(
                f"cannot verify {task_id} in state {task.status.value}"
            )
        if passed:
            self._set_status(task_id, TaskStatus.DONE, reset_failures=True)
            self._annotate_last_attempt(task_id, AttemptOutcome.SUCCESS, reason)
            return TaskStatus.DONE

        exhausted = task.attempt_count >= self.max_attempts
        new_status = TaskStatus.BLOCKED if exhausted else TaskStatus.READY
        self._set_status(
            task_id, new_status, bump_failures=True, last_error=reason
        )
        self._annotate_last_attempt(task_id, AttemptOutcome.REJECTED, reason)
        return new_status

    # ---- crash recovery -----------------------------------------------------

    def reclaim_expired(self, now: Optional[float] = None) -> list[str]:
        """Return crashed tasks (expired lease) to the queue.

        A worker that dies mid-task leaves its lease to expire; the dispatcher
        calls this each tick to hand the task to someone else. Reclaimed tasks
        go back to READY, or to BLOCKED once the attempt ceiling is hit.
        """
        now = now if now is not None else now_ts()
        reclaimed: list[str] = []
        for t in self.tasks_by_status(TaskStatus.IN_PROGRESS):
            if t.claim_expires_at is not None and t.claim_expires_at < now:
                exhausted = t.attempt_count >= self.max_attempts
                target = TaskStatus.BLOCKED if exhausted else TaskStatus.READY
                self._set_status(
                    t.task_id, target, clear_lock=True, bump_failures=True,
                    last_error="lease expired (worker crash suspected)",
                )
                self._annotate_last_attempt(
                    t.task_id, AttemptOutcome.CRASHED, "lease expired"
                )
                reclaimed.append(t.task_id)
        return reclaimed

    # ---- replan hook --------------------------------------------------------

    def revive_blocked(self, task_id: str, reset_attempts: bool = True) -> None:
        """Replanner brings a BLOCKED task back for another go (BLOCKED -> PENDING)."""
        task = self._require(task_id)
        if task.status != TaskStatus.BLOCKED:
            raise TransitionError(f"{task_id} is not blocked")
        with self._connect() as conn:
            self._guard(task.status, TaskStatus.PENDING)
            conn.execute(
                """UPDATE tasks SET status='pending', claim_lock=NULL,
                   claim_expires_at=NULL, consecutive_failures=0,
                   attempt_count = CASE WHEN ? THEN 0 ELSE attempt_count END,
                   updated_at=? WHERE task_id=?""",
                (1 if reset_attempts else 0, now_ts(), task_id),
            )
            conn.commit()

    # ---- internals ----------------------------------------------------------

    def _require(self, task_id: str) -> Task:
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(f"unknown task {task_id}")
        return task

    def _guard(self, src: TaskStatus, dst: TaskStatus) -> None:
        if not is_transition_allowed(src, dst):
            raise TransitionError(f"illegal transition {src.value} -> {dst.value}")

    def _set_status(
        self,
        task_id: str,
        new_status: TaskStatus,
        *,
        clear_lock: bool = False,
        bump_failures: bool = False,
        reset_failures: bool = False,
        last_error: Optional[str] = None,
    ) -> None:
        task = self._require(task_id)
        self._guard(task.status, new_status)
        sets = ["status = ?", "updated_at = ?"]
        params: list = [new_status.value, now_ts()]
        if clear_lock:
            sets += ["claim_lock = NULL", "claim_expires_at = NULL"]
        if bump_failures:
            sets.append("consecutive_failures = consecutive_failures + 1")
        if reset_failures:
            sets.append("consecutive_failures = 0")
        if last_error is not None:
            sets.append("last_error = ?")
            params.append(last_error)
        params.append(task_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE task_id = ?", params
            )
            conn.commit()

    def _record_attempt(
        self,
        task: Task,
        worker_id: str,
        outcome: AttemptOutcome,
        handoff: Handoff,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO attempts
                   (attempt_id, task_id, attempt_no, worker_id, outcome,
                    summary, verdict, started_at, ended_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id("att"), task.task_id, task.attempt_count, worker_id,
                    outcome.value, handoff.to_json(), "", now_ts(), now_ts(),
                ),
            )
            conn.commit()

    def _annotate_last_attempt(
        self, task_id: str, outcome: AttemptOutcome, verdict: str
    ) -> None:
        """Stamp the verifier's ruling onto the most recent attempt row."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT attempt_id FROM attempts WHERE task_id = ? "
                "ORDER BY ended_at DESC, rowid DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE attempts SET outcome = ?, verdict = ? WHERE attempt_id = ?",
                    (outcome.value, verdict, row["attempt_id"]),
                )
                conn.commit()

    def attempt_history(self, task_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM attempts WHERE task_id = ? ORDER BY started_at",
                (task_id,),
            ).fetchall()
        return [dict(r) for r in rows]
