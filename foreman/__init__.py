"""Foreman — an AI foreman that makes coding agents actually finish long tasks.

The deterministic orchestration core (models, ledger, dispatcher) is importable
without any API keys or third-party packages.
"""

from .models import Task, TaskStatus, Handoff, AttemptOutcome, CriterionStatus
from .ledger import Ledger, TransitionError
from .dispatcher import Dispatcher, TokenBucket, TickReport

__all__ = [
    "Task",
    "TaskStatus",
    "Handoff",
    "AttemptOutcome",
    "CriterionStatus",
    "Ledger",
    "TransitionError",
    "Dispatcher",
    "TokenBucket",
    "TickReport",
]
