"""End-to-end smoke test of the deterministic core — NO API key required.

This wires the ledger + dispatcher to a *fake* executor and verifier so you can
watch the whole orchestration loop run: tasks flow pending -> ready ->
in_progress -> pending_review -> done, a deliberately buggy task gets rejected
and retried, and the status wall fills in. It proves the plumbing works before
any Qwen call is added.

Run:  python demo/smoke_run.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foreman.dispatcher import Dispatcher
from foreman.ledger import Ledger
from foreman.models import Handoff, Task, TaskStatus

# A tiny 6-item "checklist" with one dependency and one task that fails twice
# before it passes — enough to exercise dependencies and the retry ladder.
CHECKLIST = [
    ("T1", "Create the project scaffold", []),
    ("T2", "Add the data model", ["T1"]),
    ("T3", "Implement the API endpoint", ["T2"]),
    ("T4", "Add input validation (flaky: fails twice)", ["T3"]),
    ("T5", "Write the integration test", ["T3"]),
    ("T6", "Wire it all together", ["T4", "T5"]),
]

# ASCII status wall — portable across Windows consoles and cloud log capture.
# (The colored grid version lives in the web UI.)
WALL = {
    TaskStatus.DONE: "[#]", TaskStatus.IN_PROGRESS: "[>]",
    TaskStatus.PENDING_REVIEW: "[?]", TaskStatus.BLOCKED: "[X]",
    TaskStatus.READY: "[ ]", TaskStatus.PENDING: "[.]", TaskStatus.ARCHIVED: "[#]",
}


def status_wall(led: Ledger) -> str:
    return " ".join(WALL[t.status] for t in led.all_tasks())


def fake_execute(task: Task) -> Handoff:
    """Stand-in for the real Qwen executor: pretends to do the work."""
    return Handoff(
        task_id=task.task_id,
        attempt_no=task.attempt_count,
        completed_work=[f"implemented: {task.title}"],
        files_touched=[f"src/{task.task_id.lower()}.py"],
    )


def fake_verify(task: Task) -> tuple[bool, str]:
    """Stand-in for the real verifier. T4 fails its first two attempts."""
    if task.task_id == "T4" and task.attempt_count < 3:
        return False, "validation rejects valid input on attempt %d" % task.attempt_count
    return True, "all acceptance criteria satisfied"


def main() -> None:
    led = Ledger()
    led.create_run("6-item smoke checklist")
    for tid, title, parents in CHECKLIST:
        led.add_task(Task(task_id=tid, title=title, description=title, parents=parents))

    disp = Dispatcher(led)
    print("Foreman smoke run - deterministic core, no LLM\n")
    print("  legend: [#]done [>]running [?]review [X]blocked [ ]ready [.]pending")
    print("  tasks in order: T1 T2 T3 T4 T5 T6\n")

    step = 0
    while not disp.tick().complete and step < 100:
        step += 1
        task = disp.claim("worker")
        if task is None:
            continue
        handoff = fake_execute(task)
        led.submit_for_review(task.task_id, "worker", handoff)
        passed, reason = fake_verify(task)
        outcome = led.record_verdict(task.task_id, passed=passed, reason=reason)
        mark = "PASS" if passed else f"REJECT (attempt {task.attempt_count})"
        print(f"  {status_wall(led)}   {task.task_id} {mark}")

    counts = led.counts()
    print(f"\ndone={counts['done']}/{len(CHECKLIST)}  "
          f"blocked={counts['blocked']}  steps={step}")
    print(f"T4 took {led.get_task('T4').attempt_count} attempts "
          f"(rejected twice, then passed) - the retry ladder at work.")


if __name__ == "__main__":
    main()
