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
            self.items = []
            # This mock rejection is standing in for the retry-ladder demo,
            # not a criteria-scoring judgement call — mark the gate red so
            # the orchestrator's dispute eligibility check (contract §6,
            # "gate failures are not disputable") correctly skips the
            # negotiation layer here without needing a real LLM client.
            self.objective_gate = {
                "command": "mock", "exit_code": 0 if passed else 1,
                "passed": passed, "output_tail": "",
            }

    def verify(self, task: Task, handoff: Handoff):
        if task.attempt_count < 2:
            return self._Report(False, f"mock rejection on attempt {task.attempt_count}")
        return self._Report(True, "mock: all acceptance criteria satisfied")


def _build_resume_orchestrator(run_id: str, run_root: str) -> Orchestrator:
    """Reopen an existing run's ledger.db + workspace for --resume.

    Mirrors the fake-mode construction below (Orchestrator.__new__ + manual
    attribute wiring) because Orchestrator.__init__ always mints a *new*
    run_id/run_dir — resume must instead point at the run_id the caller named
    on the command line. No planner call happens here (contract §7: no
    re-planning): the plan already lives in the reopened ledger's tasks table.
    """
    from pathlib import Path

    from foreman.config import Settings
    from foreman.dispatcher import Dispatcher
    from foreman.ledger import Ledger
    from foreman.workspace import Workspace

    settings = Settings.from_env()

    orch = Orchestrator.__new__(Orchestrator)
    orch.settings = settings
    from foreman.config import make_client
    orch.client = make_client(settings)

    orch.run_root = Path(run_root)
    orch.run_id = run_id
    run_dir = orch.run_root / run_id
    if not run_dir.is_dir():
        raise SystemExit(f"no such run directory: {run_dir}")
    orch.run_dir = run_dir

    orch.ledger = Ledger(db_path=str(run_dir / "ledger.db"))
    orch.workspace = Workspace(run_dir / "workspace")
    orch.dispatcher = Dispatcher(orch.ledger)

    from foreman.arbiter import Arbiter
    from foreman.executor import Executor
    from foreman.planner import Planner
    from foreman.verifier import Verifier

    orch.planner = Planner(orch.client, settings.planner_model)
    orch.executor = Executor(orch.client, settings.executor_model, orch.workspace)
    orch.verifier = Verifier(orch.client, settings.verifier_model, orch.workspace)
    orch.arbiter = Arbiter(orch.client, settings.planner_model, orch.workspace)
    orch.events_path = run_dir / "events.jsonl"
    orch.disputed_task_ids = set()
    return orch


def main() -> int:
    parser = argparse.ArgumentParser(description="Foreman: autonomous task-execution orchestrator")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--checklist", help="path to a requirements checklist (markdown)")
    group.add_argument("--resume", metavar="RUN_ID", help="resume an existing run by id (no re-planning)")
    parser.add_argument("--mock", action="store_true", help="use scripted fake executor/verifier, no API key needed")
    parser.add_argument("--run-root", default="runs", help="directory under which run artifacts are stored")
    args = parser.parse_args()

    if args.resume:
        if args.mock:
            raise SystemExit("--resume does not support --mock (there is no fake ledger to reopen)")
        orch = _build_resume_orchestrator(args.resume, args.run_root)
        print(f"Foreman run resuming — run_id={orch.run_id}")
        print("  legend: [#]done [>]running [?]review [X]blocked [ ]ready [.]pending\n")

        summary = orch.resume_run(args.resume)

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
        # MockVerifier's rejections always report a red gate (see _Report),
        # so _dispute_eligible always short-circuits before touching the
        # arbiter — this Arbiter's client is never actually called, but the
        # attribute must exist since the orchestrator reads it unconditionally.
        from foreman.arbiter import Arbiter
        orch.arbiter = Arbiter(None, settings.planner_model, orch.workspace)
        orch.disputed_task_ids = set()
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
