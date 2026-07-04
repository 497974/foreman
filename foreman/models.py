"""Core data models and the task state machine for Foreman.

This module is intentionally dependency-free (standard library only) so the
deterministic orchestration core — ledger, dispatcher, state machine — can be
exercised by unit tests without any API keys or third-party packages.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class TaskStatus(str, Enum):
    """The seven states a task moves through.

    Only three transitions are automatic (see ``AUTOMATIC_TRANSITIONS``); every
    other move requires an explicit action so the machine can never advance by
    accident — a deliberate guard against the "agent silently marks itself done"
    failure mode.
    """

    PENDING = "pending"            # created; waiting on dependencies
    READY = "ready"               # dependencies satisfied; free to claim
    IN_PROGRESS = "in_progress"   # claimed by an executor, being worked on
    PENDING_REVIEW = "pending_review"  # executor submitted; awaiting the verifier
    DONE = "done"                 # verifier passed it
    BLOCKED = "blocked"           # retries exhausted; awaiting the replanner
    ARCHIVED = "archived"         # run finished


# Legal state transitions. A move not listed here is rejected by the ledger.
ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING: {TaskStatus.READY, TaskStatus.BLOCKED},
    TaskStatus.READY: {TaskStatus.IN_PROGRESS, TaskStatus.PENDING, TaskStatus.BLOCKED},
    TaskStatus.IN_PROGRESS: {
        TaskStatus.PENDING_REVIEW,  # executor submits work
        TaskStatus.READY,           # lease expired / crash -> requeue
        TaskStatus.BLOCKED,         # too many consecutive failures
    },
    TaskStatus.PENDING_REVIEW: {
        TaskStatus.DONE,     # verifier passed
        TaskStatus.READY,    # verifier rejected, retries remain -> requeue
        TaskStatus.BLOCKED,  # verifier rejected, retries exhausted
    },
    TaskStatus.BLOCKED: {TaskStatus.PENDING, TaskStatus.READY},  # replanner revives
    TaskStatus.DONE: {TaskStatus.ARCHIVED},
    TaskStatus.ARCHIVED: set(),
}

# Transitions the dispatcher performs on its own, without an explicit tool call.
AUTOMATIC_TRANSITIONS: set[tuple[TaskStatus, TaskStatus]] = {
    (TaskStatus.PENDING, TaskStatus.READY),        # dependencies became satisfied
    (TaskStatus.READY, TaskStatus.IN_PROGRESS),    # atomic claim
    (TaskStatus.IN_PROGRESS, TaskStatus.READY),    # crashed lease reclaimed
}


def is_transition_allowed(src: TaskStatus, dst: TaskStatus) -> bool:
    return dst in ALLOWED_TRANSITIONS.get(src, set())


class AttemptOutcome(str, Enum):
    """How a single execution attempt ended (recorded in the attempt history)."""

    SUCCESS = "success"                    # passed verification
    REJECTED = "rejected_by_verifier"      # failed verification, sent back
    DISPUTED = "disputed"                  # executor contested the rejection
    CRASHED = "crashed"                    # worker died / lease expired
    TIMEOUT = "timeout"                    # ran past its budget


class CriterionStatus(str, Enum):
    """Three-tier grade for a single acceptance criterion (0 / 0.5 / 1)."""

    SATISFIED = "satisfied"
    PARTIAL = "partially_satisfied"
    NOT_SATISFIED = "not_satisfied"

    @property
    def score(self) -> float:
        return {"satisfied": 1.0, "partially_satisfied": 0.5, "not_satisfied": 0.0}[
            self.value
        ]


def now_ts() -> float:
    """Wall-clock seconds. Isolated here so tests can monkeypatch time easily."""
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class Task:
    """One unit of work in the progress ledger.

    A task carries everything an executor needs to work with a *clean context*:
    its own spec, its acceptance criteria, and the concrete command that proves
    it done — never the conversation history of whoever planned or ran it.
    """

    task_id: str
    title: str
    description: str
    acceptance_criteria: list[str] = field(default_factory=list)
    test_strategy: str = ""                     # concrete, runnable verification
    role: str = "generalist"                    # frontend / backend / data / ...
    priority: int = 3                           # 1 (highest) .. 5 (lowest)
    complexity_score: int = 1                   # 1..10; >=5 => planner should split
    parents: list[str] = field(default_factory=list)  # dependency task_ids
    files_touched: list[str] = field(default_factory=list)

    status: TaskStatus = TaskStatus.PENDING
    claim_lock: Optional[str] = None            # worker id currently holding it
    claim_expires_at: Optional[float] = None    # lease TTL deadline (epoch secs)
    attempt_count: int = 0
    consecutive_failures: int = 0
    last_error: Optional[str] = None
    created_at: float = field(default_factory=now_ts)
    updated_at: float = field(default_factory=now_ts)

    @property
    def idempotency_key(self) -> str:
        """Stable across retries of the *same* attempt, unique across attempts.

        An executor stamps side effects with this so a crash-and-replay of the
        same attempt does not double-write (the Temporal durable-execution idea).
        """
        return f"{self.task_id}:{self.attempt_count}"

    def to_row(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        # list/JSON columns are serialized as text for SQLite
        for key in ("acceptance_criteria", "parents", "files_touched"):
            d[key] = json.dumps(d[key], ensure_ascii=False)
        return d

    @classmethod
    def from_row(cls, row: dict) -> "Task":
        data = dict(row)
        data["status"] = TaskStatus(data["status"])
        for key in ("acceptance_criteria", "parents", "files_touched"):
            data[key] = json.loads(data[key]) if data.get(key) else []
        # drop columns that are not constructor args
        allowed = set(cls.__dataclass_fields__.keys())
        data = {k: v for k, v in data.items() if k in allowed}
        return cls(**data)


@dataclass
class Handoff:
    """Structured note an executor leaves when it finishes an attempt.

    Fields are the intersection of what production coding agents preserve when
    they compact context: what changed, which files/contracts, what was learned
    (errors kept verbatim), and what to do next.
    """

    schema_version: int = 1
    task_id: str = ""
    attempt_no: int = 0
    outcome: str = AttemptOutcome.SUCCESS.value
    completed_work: list[str] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)
    interface_contract: list[str] = field(default_factory=list)
    gotchas: list[str] = field(default_factory=list)      # errors kept verbatim
    self_check: list[str] = field(default_factory=list)   # criterion -> pass/fail
    handoff_reason: str = "completed"
    facts_for_replanner: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)
