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
from foreman.git_safety import GitSafetyError
from foreman.orchestrator import Orchestrator, status_wall


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
    # Existing-project mode (contract Addendum 4 §14): resume_run() itself
    # self-derives project_dir/branch from run_dir/project_mode.json (and
    # repoints orch.workspace there) if that file exists — these attributes
    # just need to exist with a greenfield-safe default before resume_run
    # runs, since __new__ skips Orchestrator.__init__ entirely here.
    orch.project_dir = None
    orch.project_branch = None
    orch._repo_context = ""
    return orch


def main() -> int:
    parser = argparse.ArgumentParser(description="Foreman: autonomous task-execution orchestrator")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--checklist", help="path to a requirements checklist (markdown)")
    group.add_argument("--resume", metavar="RUN_ID", help="resume an existing run by id (no re-planning)")
    parser.add_argument("--mock", action="store_true", help="use scripted fake executor/verifier, no API key needed")
    parser.add_argument(
        "--mock-delay", type=float, default=0.0, metavar="SECONDS",
        help="only with --mock: sleep this long in each fake execute()/verify() call, "
        "so a demo run paces slowly enough to film instead of finishing instantly (default 0)",
    )
    parser.add_argument("--run-root", default="runs", help="directory under which run artifacts are stored")
    parser.add_argument(
        "--project-dir", metavar="PATH", default=None,
        help="existing-project mode: point Foreman at a real git repo instead of a "
        "fresh sandbox (only valid with --checklist; --resume self-derives this "
        "from the run's project_mode.json). Requires a clean repo unless "
        "--force-dirty is also passed.",
    )
    parser.add_argument(
        "--force-dirty", action="store_true",
        help="existing-project mode: proceed even if the repo has uncommitted "
        "changes (not recommended; only valid with --checklist).",
    )
    args = parser.parse_args()

    if args.resume and (args.project_dir or args.force_dirty):
        raise SystemExit(
            "--project-dir/--force-dirty are not valid with --resume — resume "
            "reads project_mode.json automatically."
        )

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

    if args.mock and (args.project_dir or args.force_dirty):
        raise SystemExit("--project-dir/--force-dirty are not supported with --mock")

    requirements = open(args.checklist, encoding="utf-8").read()

    if args.mock:
        # Bypass make_client()/real component construction entirely for mock
        # mode: foreman.mocks.build_mock_orchestrator() wires the same
        # attributes __init__ would, but with fakes (MockPlanner/Executor/
        # Verifier) so no Settings/API key/network is ever touched.
        from foreman.mocks import build_mock_orchestrator

        orch = build_mock_orchestrator(run_root=args.run_root, delay_s=args.mock_delay)
    else:
        settings = Settings.from_env()
        try:
            orch = Orchestrator(
                settings,
                run_root=args.run_root,
                project_dir=args.project_dir,
                force_dirty=args.force_dirty,
            )
        except GitSafetyError as e:
            # Clean one-line error, no traceback — the whole point of
            # git_safety's actionable messages is that the user reads them,
            # not a stack trace.
            print(f"error: {e}")
            return 1

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
