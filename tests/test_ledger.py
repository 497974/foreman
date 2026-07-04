"""Ledger + state-machine tests. Run: pytest -q  (no API key, no pip installs)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from foreman.models import (
    Handoff,
    Task,
    TaskStatus,
    is_transition_allowed,
)
from foreman.ledger import Ledger, TransitionError


def make_task(tid: str, parents=None, **kw) -> Task:
    return Task(
        task_id=tid,
        title=f"task {tid}",
        description="do the thing",
        acceptance_criteria=["it works"],
        test_strategy="pytest",
        parents=parents or [],
        **kw,
    )


# ---- state machine ---------------------------------------------------------


def test_legal_and_illegal_transitions():
    assert is_transition_allowed(TaskStatus.PENDING, TaskStatus.READY)
    assert is_transition_allowed(TaskStatus.READY, TaskStatus.IN_PROGRESS)
    assert is_transition_allowed(TaskStatus.PENDING_REVIEW, TaskStatus.DONE)
    # you cannot leap from pending straight to done
    assert not is_transition_allowed(TaskStatus.PENDING, TaskStatus.DONE)
    # done is terminal except for archival
    assert not is_transition_allowed(TaskStatus.DONE, TaskStatus.READY)
    assert is_transition_allowed(TaskStatus.DONE, TaskStatus.ARCHIVED)


def test_idempotency_key_tracks_attempts():
    t = make_task("T1")
    assert t.idempotency_key == "T1:0"
    t.attempt_count = 2
    assert t.idempotency_key == "T1:2"


# ---- dependency resolution -------------------------------------------------


def test_recompute_ready_respects_dependencies():
    led = Ledger()
    led.add_task(make_task("A"))
    led.add_task(make_task("B", parents=["A"]))

    promoted = led.recompute_ready()
    assert promoted == ["A"]                       # B is still blocked on A
    assert led.get_task("A").status == TaskStatus.READY
    assert led.get_task("B").status == TaskStatus.PENDING

    # finish A, then B should unlock
    w = "worker-1"
    led.claim_next(w)
    led.submit_for_review("A", w, Handoff(task_id="A"))
    led.record_verdict("A", passed=True)
    led.recompute_ready()
    assert led.get_task("B").status == TaskStatus.READY


# ---- the happy path --------------------------------------------------------


def test_claim_submit_pass_flow():
    led = Ledger()
    led.add_task(make_task("A"))
    led.recompute_ready()

    task = led.claim_next("w1")
    assert task.task_id == "A"
    assert task.status == TaskStatus.IN_PROGRESS
    assert task.attempt_count == 1
    assert task.claim_lock == "w1"

    led.submit_for_review("A", "w1", Handoff(task_id="A"))
    assert led.get_task("A").status == TaskStatus.PENDING_REVIEW

    led.record_verdict("A", passed=True)
    assert led.get_task("A").status == TaskStatus.DONE
    assert led.is_run_complete()


def test_only_lock_holder_can_submit():
    led = Ledger()
    led.add_task(make_task("A"))
    led.recompute_ready()
    led.claim_next("w1")
    with pytest.raises(TransitionError):
        led.submit_for_review("A", "impostor", Handoff(task_id="A"))


# ---- the retry ladder ------------------------------------------------------


def test_retry_ladder_blocks_after_ceiling():
    led = Ledger(max_attempts=3)
    led.add_task(make_task("A"))
    led.recompute_ready()

    for expected_attempt in (1, 2):
        t = led.claim_next("w1")
        assert t.attempt_count == expected_attempt
        led.submit_for_review("A", "w1", Handoff(task_id="A"))
        status = led.record_verdict("A", passed=False, reason="tests fail")
        assert status == TaskStatus.READY            # requeued, retries remain

    # third attempt: rejection now escalates to BLOCKED
    t = led.claim_next("w1")
    assert t.attempt_count == 3
    led.submit_for_review("A", "w1", Handoff(task_id="A"))
    status = led.record_verdict("A", passed=False, reason="still failing")
    assert status == TaskStatus.BLOCKED
    assert led.get_task("A").consecutive_failures == 3


def test_pass_resets_failure_counter():
    led = Ledger()
    led.add_task(make_task("A"))
    led.recompute_ready()
    led.claim_next("w1")
    led.submit_for_review("A", "w1", Handoff(task_id="A"))
    led.record_verdict("A", passed=False, reason="oops")   # failures -> 1
    led.claim_next("w1")
    led.submit_for_review("A", "w1", Handoff(task_id="A"))
    led.record_verdict("A", passed=True)
    assert led.get_task("A").consecutive_failures == 0


# ---- crash recovery --------------------------------------------------------


def test_expired_lease_is_reclaimed():
    from foreman.models import now_ts

    led = Ledger()
    led.add_task(make_task("A"))
    led.recompute_ready()
    led.claim_next("w1", lease_seconds=10)

    # checking "now" before the lease deadline: nothing expired yet
    assert led.reclaim_expired(now=now_ts()) == []
    # well past the 10s lease: the task is stale and goes back to the queue
    reclaimed = led.reclaim_expired(now=now_ts() + 3600)
    assert reclaimed == ["A"]
    t = led.get_task("A")
    assert t.status == TaskStatus.READY
    assert t.claim_lock is None


def test_blocked_task_can_be_revived():
    led = Ledger(max_attempts=1)
    led.add_task(make_task("A"))
    led.recompute_ready()
    led.claim_next("w1")
    led.submit_for_review("A", "w1", Handoff(task_id="A"))
    led.record_verdict("A", passed=False, reason="nope")
    assert led.get_task("A").status == TaskStatus.BLOCKED

    led.revive_blocked("A", reset_attempts=True)
    t = led.get_task("A")
    assert t.status == TaskStatus.PENDING
    assert t.attempt_count == 0


# ---- audit trail -----------------------------------------------------------


def test_attempt_history_is_appended():
    led = Ledger()
    led.add_task(make_task("A"))
    led.recompute_ready()
    led.claim_next("w1")
    led.submit_for_review("A", "w1", Handoff(task_id="A", completed_work=["built X"]))
    led.record_verdict("A", passed=False, reason="missing Y")

    history = led.attempt_history("A")
    assert len(history) == 1
    assert history[0]["outcome"] == "rejected_by_verifier"
    assert history[0]["verdict"] == "missing Y"
