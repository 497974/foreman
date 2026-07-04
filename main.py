"""Foreman CLI entry point.

    python main.py --checklist demo/requirements_mini.md [--mock]

Without --mock this drives the real Qwen planner/executor/verifier end to
end. --mock swaps the executor and verifier for scripted fakes (same spirit
as demo/smoke_run.py) so the full claim -> execute -> submit -> verify loop
can be exercised for free, without any API key or network access.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from foreman.config import Settings
from foreman.models import AttemptOutcome, Handoff, Task, TaskStatus
from foreman.orchestrator import Orchestrator, status_wall


class MockExecutor:
    """Stand-in for foreman.executor.Executor: no LLM, deterministic output.

    Mirrors demo/smoke_run.py's fake_execute — pretends to do the work and
    returns a plausible Handoff. Task "T4"-style "flaky" tasks are not
    special-cased here (the mock verifier below owns the retry-ladder demo
    instead, since it can see attempt_count without re-deriving task naming
    conventions from a live-planned checklist).
    """

    def execute(self, task: Task, dependency_handoffs: list[Handoff]) -> Handoff:
        return Handoff(
            task_id=task.task_id,
            attempt_no=task.attempt_count,
            outcome=AttemptOutcome.SUCCESS.value,
            completed_work=[f"implemented: {task.title}"],
            files_touched=[f"src/{task.task_id.lower()}.py"],
            handoff_reason="mock execution complete",
        )


class MockVerifier:
    """Stand-in for foreman.verifier.Verifier: rejects the first attempt of
    every task once (to exercise the retry ladder + feedback plumbing), then
    passes on the second attempt."""

    class _Report:
        def __init__(self, passed: bool, reason: str):
            self.passed = passed
            self.reason = reason
            self.actionable_feedback = (
                [] if passed else [
                    "mock: fix the deliberately-failed first attempt",
                    "mock: check src/ output matches acceptance criteria",
                    "mock: re-run the verification command",
                ]
            )
            self.coverage_rate = 1.0 if passed else 0.0

    def verify(self, task: Task, handoff: Handoff):
        if task.attempt_count < 2:
            return self._Report(False, f"mock rejection on attempt {task.attempt_count}")
        return self._Report(True, "mock: all acceptance criteria satisfied")


def main() -> int:
    parser = argparse.ArgumentParser(description="Foreman: autonomous task-execution orchestrator")
    parser.add_argument("--checklist", required=True, help="path to a requirements checklist (markdown)")
    parser.add_argument("--mock", action="store_true", help="use scripted fake executor/verifier, no API key needed")
    parser.add_argument("--run-root", default="runs", help="directory under which run artifacts are stored")
    args = parser.parse_args()

    requirements = open(args.checklist, encoding="utf-8").read()

    if args.mock:
        # Settings still needs *some* values but must not require a real key
        # or touch the network in --mock mode.
        settings = Settings(
            api_key="mock-key",
            base_url="http://mock.invalid",
            planner_model="mock-planner",
            executor_model="mock-executor",
            verifier_model="mock-verifier",
        )
        orch = Orchestrator.__new__(Orchestrator)
        # Bypass make_client()/real component construction entirely for mock
        # mode: build the same attributes __init__ would, but with fakes.
        from pathlib import Path
        from foreman.dispatcher import Dispatcher
        from foreman.ledger import Ledger
        from foreman.workspace import Workspace
        from foreman.models import new_id

        orch.settings = settings
        orch.client = None
        orch.run_root = Path(args.run_root)
        orch.run_root.mkdir(parents=True, exist_ok=True)
        orch.run_id = new_id("run")
        run_dir = orch.run_root / orch.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        orch.run_dir = run_dir
        orch.ledger = Ledger(db_path=str(run_dir / "ledger.db"))
        orch.workspace = Workspace(run_dir / "workspace")
        orch.dispatcher = Dispatcher(orch.ledger)
        orch.planner = None  # plan() is monkeypatched onto the instance below
        orch.executor = MockExecutor()
        orch.verifier = MockVerifier()
        orch.events_path = run_dir / "events.jsonl"

        # Mock mode still needs *some* planner: rather than reaching the
        # network, split the checklist into one task per non-empty line,
        # each depending on the previous one (a simple linear chain), which
        # is enough to exercise claim/execute/verify/requeue end to end.
        class _MockPlanner:
            def plan(self, requirements: str) -> list[Task]:
                lines = [l.strip() for l in requirements.splitlines() if l.strip() and not l.strip().startswith("#")]
                tasks = []
                prev_id = None
                for i, line in enumerate(lines, start=1):
                    tid = f"T{i:02d}"
                    tasks.append(
                        Task(
                            task_id=tid,
                            title=line[:80],
                            description=line,
                            acceptance_criteria=[f"{line} is implemented"],
                            test_strategy="python -c \"assert True\"",
                            parents=[prev_id] if prev_id else [],
                        )
                    )
                    prev_id = tid
                return tasks

        orch.planner = _MockPlanner()
    else:
        settings = Settings.from_env()
        orch = Orchestrator(settings, run_root=args.run_root)

    print(f"Foreman run starting (mock={args.mock}) — run_id={orch.run_id}")
    print("  legend: [#]done [>]running [?]review [X]blocked [ ]ready [.]pending\n")

    summary = orch.run_checklist(requirements)

    print("\n--- final summary ---")
    print(f"run_id: {summary['run_id']}")
    print(f"run_dir: {summary['run_dir']}")
    print(f"done: {summary['done']}/{summary['total_tasks']}   blocked: {summary['blocked']}")
    print(f"claims used: {summary['claims']}   elapsed: {summary['elapsed_s']:.1f}s")
    print("attempts per task:")
    for tid, n in summary["attempts_per_task"].items():
        print(f"  {tid}: {n}")
    print(f"complete: {summary['complete']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
