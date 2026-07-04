"""Dispatcher tests: concurrency safety, stall detection, rate limiting.

The concurrency test is the important one — if two workers can ever claim the
same task, the whole "clean context per task" guarantee falls apart.
"""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foreman.dispatcher import Dispatcher, TokenBucket
from foreman.ledger import Ledger
from foreman.models import Task, TaskStatus


def make_task(tid: str, parents=None) -> Task:
    return Task(task_id=tid, title=tid, description="x", parents=parents or [])


def test_no_double_claim_under_concurrency(tmp_path):
    """20 tasks, 8 threads hammering claim_next: every task claimed exactly once."""
    db = str(tmp_path / "race.db")
    led = Ledger(db)
    for i in range(20):
        led.add_task(make_task(f"T{i:02d}"))
    led.recompute_ready()

    claimed: list[str] = []
    lock = threading.Lock()

    def worker(name: str):
        while True:
            task = led.claim_next(name, lease_seconds=999)
            if task is None:
                return
            with lock:
                claimed.append(task.task_id)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # every task claimed, and none claimed twice
    assert sorted(claimed) == [f"T{i:02d}" for i in range(20)]
    assert len(claimed) == len(set(claimed))
    assert all(
        t.status == TaskStatus.IN_PROGRESS for t in led.all_tasks()
    )


def test_tick_reports_completion():
    led = Ledger()
    led.add_task(make_task("A"))
    disp = Dispatcher(led)

    report = disp.tick()
    assert "A" in report.promoted
    assert not report.complete

    from foreman.models import Handoff
    disp.claim("w1")
    led.submit_for_review("A", "w1", Handoff(task_id="A"))
    led.record_verdict("A", passed=True)
    assert disp.tick().complete


def test_stall_detection():
    led = Ledger()
    led.add_task(make_task("A"))
    disp = Dispatcher(led, stall_ticks=3)
    disp.claim("w1")   # A is now in_progress and will just sit there

    reports = [disp.tick() for _ in range(5)]
    assert not reports[0].stalled
    assert reports[-1].stalled   # outstanding work, no progress for 3+ ticks


def test_role_filtered_claim():
    led = Ledger()
    led.add_task(Task(task_id="F", title="f", description="x", role="frontend"))
    led.add_task(Task(task_id="B", title="b", description="x", role="backend"))
    led.recompute_ready()

    task = led.claim_next("w1", role="backend")
    assert task.task_id == "B"


def test_token_bucket_limits_burst():
    bucket = TokenBucket(capacity=3, refill_per_sec=0)  # no refill
    assert [bucket.try_acquire() for _ in range(5)] == [True, True, True, False, False]
