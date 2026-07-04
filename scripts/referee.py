"""The Referee: the ONLY judge used to score the three-condition comparison.

Foreman's own Verifier is deliberately excluded from that score — a system
should not grade its own exam, especially when the whole point of the
experiment is asking whether Foreman's verification loop actually buys
anything over just letting a model work unsupervised. So the referee is a
second, independent instrument: for a given workspace and a frozen exam
(the planner's task list, saved as exam.json), it re-runs each task's own
test_strategy directly against the filesystem, plus one blanket
`python -m pytest -q` sweep, and reports plain pass/fail counts. No LLM
judgement, no coverage scoring — just exit codes, the same ground-truth bias
every objective gate in this codebase already leans on.

Importable (``score_workspace``) so scripts/evaluate.py can call it in-process
after each condition, and standalone:

    python scripts/referee.py --workspace runs/<run_id>/workspace --exam evals/exam.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foreman.workspace import Workspace  # noqa: E402

GATE_TIMEOUT_S = 180.0  # per contract: 180s per task test_strategy


@dataclass
class TaskVerdict:
    task_id: str
    title: str
    test_strategy: str
    passed: bool
    exit_code: int
    output_tail: str

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "test_strategy": self.test_strategy,
            "passed": self.passed,
            "exit_code": self.exit_code,
            "output_tail": self.output_tail,
        }


@dataclass
class RefereeReport:
    task_verdicts: list[TaskVerdict] = field(default_factory=list)
    n_tasks: int = 0
    n_passed: int = 0
    pytest_ran: bool = False
    pytest_passed: bool = False
    pytest_exit_code: int = 0
    pytest_output_tail: str = ""

    def as_dict(self) -> dict:
        return {
            "task_verdicts": [v.as_dict() for v in self.task_verdicts],
            "n_tasks": self.n_tasks,
            "n_passed": self.n_passed,
            "pytest_ran": self.pytest_ran,
            "pytest_passed": self.pytest_passed,
            "pytest_exit_code": self.pytest_exit_code,
            "pytest_output_tail": self.pytest_output_tail,
        }


def _run_one(workspace: Workspace, command: str, timeout: float = GATE_TIMEOUT_S):
    """Run one command in the workspace, tolerating a missing/empty command.

    A task with no test_strategy (should not happen post-planner, but the
    referee must never crash on a malformed exam) is scored as a failure —
    there is nothing to run, so there is nothing to credit.
    """
    if not command or not command.strip():
        return None
    return workspace.run(command, timeout=timeout)


def score_workspace(workspace: Workspace, exam_tasks: list[dict]) -> RefereeReport:
    """Score a workspace against a frozen exam's per-task test_strategy list.

    ``exam_tasks`` is the same shape as ``exam.json``'s "tasks" list — plain
    dicts (task_id, title, test_strategy, ...), not live foreman.models.Task
    objects, so the referee has zero coupling to whichever condition produced
    the workspace (single-agent, sequential, or full Foreman all end up as
    just "a directory on disk" by the time the referee runs).
    """
    verdicts: list[TaskVerdict] = []
    for t in exam_tasks:
        task_id = t.get("task_id") or t.get("id") or "?"
        title = t.get("title", task_id)
        strategy = t.get("test_strategy", "") or ""

        result = _run_one(workspace, strategy)
        if result is None:
            verdicts.append(
                TaskVerdict(
                    task_id=task_id, title=title, test_strategy=strategy,
                    passed=False, exit_code=-1, output_tail="(no test_strategy given)",
                )
            )
            continue

        tail = ((result.stdout or "") + (result.stderr or ""))[-2000:]
        verdicts.append(
            TaskVerdict(
                task_id=task_id, title=title, test_strategy=strategy,
                passed=(result.exit_code == 0 and not result.timed_out),
                exit_code=result.exit_code, output_tail=tail,
            )
        )

    report = RefereeReport(
        task_verdicts=verdicts,
        n_tasks=len(verdicts),
        n_passed=sum(1 for v in verdicts if v.passed),
    )

    # Overall regression sweep: a workspace that only passes each task's own
    # narrow test but has broken everything else around it should not look
    # identical to one where the whole suite is green.
    pytest_result = workspace.run("python -m pytest -q", timeout=GATE_TIMEOUT_S)
    report.pytest_ran = True
    report.pytest_passed = pytest_result.exit_code == 0
    report.pytest_exit_code = pytest_result.exit_code
    report.pytest_output_tail = ((pytest_result.stdout or "") + (pytest_result.stderr or ""))[-2000:]

    return report


def score_run(workspace_path: str | Path, exam_path: str | Path) -> RefereeReport:
    """Convenience wrapper: load exam.json from disk, build a Workspace, score it."""
    exam = json.loads(Path(exam_path).read_text(encoding="utf-8"))
    workspace = Workspace(workspace_path)
    return score_workspace(workspace, exam.get("tasks", []))


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Referee: independently score a workspace against a frozen exam's test_strategy list."
    )
    parser.add_argument("--workspace", required=True, help="path to the workspace directory to score")
    parser.add_argument("--exam", required=True, help="path to exam.json (the frozen planned task list)")
    args = parser.parse_args()

    report = score_run(args.workspace, args.exam)

    print(f"referee: {report.n_passed}/{report.n_tasks} task test_strategy(s) passed")
    for v in report.task_verdicts:
        mark = "PASS" if v.passed else "FAIL"
        print(f"  [{mark}] {v.task_id} {v.title}")
    print(f"overall pytest -q: {'PASS' if report.pytest_passed else 'FAIL'} (exit={report.pytest_exit_code})")

    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
