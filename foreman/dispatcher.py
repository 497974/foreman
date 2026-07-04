"""The Dispatcher: deterministic scheduling, zero LLM calls.

Everything an LLM does in Foreman is a *judgement* (plan, execute, verify).
Everything about *who does what, when* is plain code — so the schedule is
reproducible and inspectable. Given the same ledger state, the dispatcher makes
the same decision every time.

Each ``tick`` does three things in order:
  1. reclaim tasks whose worker crashed (expired lease),
  2. promote tasks whose dependencies are now satisfied,
  3. report progress and detect a global stall.

Claiming work is delegated to the ledger's compare-and-swap.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

from .ledger import Ledger
from .models import Task, TaskStatus, now_ts


@dataclass
class TickReport:
    reclaimed: list[str]
    promoted: list[str]
    counts: dict[str, int]
    complete: bool
    stalled: bool


class TokenBucket:
    """Thread-safe rate limiter.

    DashScope enforces limits per *account* (extra API keys do not add capacity),
    so all executors must share one bucket or they will trip 429s in front of the
    judges. ``capacity`` is the burst size; ``refill_per_sec`` the steady rate.
    """

    def __init__(self, capacity: float, refill_per_sec: float):
        self.capacity = float(capacity)
        self.refill_per_sec = float(refill_per_sec)
        self._tokens = float(capacity)
        self._last = now_ts()
        self._lock = threading.Lock()

    def try_acquire(self, cost: float = 1.0) -> bool:
        with self._lock:
            now = now_ts()
            self._tokens = min(
                self.capacity, self._tokens + (now - self._last) * self.refill_per_sec
            )
            self._last = now
            if self._tokens >= cost:
                self._tokens -= cost
                return True
            return False


class Dispatcher:
    def __init__(
        self,
        ledger: Ledger,
        lease_seconds: float = 900.0,
        stall_ticks: int = 20,
    ):
        self.ledger = ledger
        self.lease_seconds = lease_seconds
        self.stall_ticks = stall_ticks          # ticks with no DONE gain => stalled
        self._last_done_count = 0
        self._ticks_without_progress = 0

    def tick(self, now: Optional[float] = None) -> TickReport:
        reclaimed = self.ledger.reclaim_expired(now=now)
        promoted = self.ledger.recompute_ready()
        counts = self.ledger.counts()

        done = counts.get(TaskStatus.DONE.value, 0)
        if done > self._last_done_count:
            self._last_done_count = done
            self._ticks_without_progress = 0
        else:
            self._ticks_without_progress += 1

        # A stall is real only when work is outstanding but nothing is moving.
        outstanding = (
            counts.get(TaskStatus.READY.value, 0)
            + counts.get(TaskStatus.IN_PROGRESS.value, 0)
            + counts.get(TaskStatus.PENDING.value, 0)
            + counts.get(TaskStatus.PENDING_REVIEW.value, 0)
        )
        stalled = (
            self._ticks_without_progress >= self.stall_ticks and outstanding > 0
        )
        return TickReport(
            reclaimed=reclaimed,
            promoted=promoted,
            counts=counts,
            complete=self.ledger.is_run_complete(),
            stalled=stalled,
        )

    def claim(self, worker_id: str, role: Optional[str] = None) -> Optional[Task]:
        """Claim the next task for a worker after refreshing the queue."""
        self.tick()
        return self.ledger.claim_next(
            worker_id, lease_seconds=self.lease_seconds, role=role
        )
