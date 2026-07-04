"""The three-condition evaluation harness: same checklist, same model, same
tools, independent referee — who actually finishes the work?

    python scripts/evaluate.py --checklist demo/requirements_mini.md \
        [--conditions ABC] [--out evals/]

Protocol (see docs/CONTRACTS.md addendum for the frozen contract this
implements):

  1. PLAN ONCE with the real Planner -> the frozen "exam" (tasks incl.
     test_strategy). Saved as exam.json so every condition is judged against
     literally the same task list, and so a rerun can be audited later.
  2. Condition A — single-agent one-shot: one Executor.execute call in a
     fresh workspace. Its "task" is a synthetic Task carrying the ENTIRE raw
     checklist text as description; acceptance_criteria is one item per
     checklist line; test_strategy is empty (it gets no exam access — this
     reproduces the real user experience of "here are N requirements, go").
     max_iters=60: a fair budget roughly equal to what Condition C spends in
     total across all tasks, since this condition has no retries to spend
     iterations on.
  3. Condition B — sequential feeding, no verification: fresh workspace; for
     each exam task in dependency order, one Executor.execute (max_iters=15)
     fed the task card plus the accumulated handoffs of whatever this
     condition itself has produced so far. Whatever comes back is accepted —
     no verifier, no retry. This isolates "does decomposition alone help"
     from "does verification on top of decomposition help".
  4. Condition C — full Foreman: Orchestrator.run_tasks with the SAME frozen
     exam tasks in a fresh run dir; normal verifier/retries/dispute.
  5. REFEREE (scripts/referee.py) scores every condition's resulting
     workspace against the SAME exam.json's test_strategy list, plus an
     overall `python -m pytest -q` sweep. The referee is the only judge used
     for the cross-condition score — Foreman's own verifier verdicts (used
     internally by Condition C to drive retries) are never used to grade the
     comparison; that would be the system grading its own exam.
  6. Each condition's wall-clock seconds, executor attempts, token totals
     (foreman.telemetry.METER reset before / snapshot after), and LLM call
     counts are recorded. Results are written to
     evals/results_<UTCtimestamp>.json and a markdown comparison table is
     printed.

Fairness is enforced three ways: (a) one frozen exam, planned once, shared by
every condition — no condition gets an easier or harder task list; (b) equal
total iteration budget — Condition A gets max_iters=60 in one call, Condition
B/C get max_iters=15 per task times however many tasks the exam has (usually
similarly sized); (c) one independent referee neither Condition C's own
verifier nor any condition's self-report can influence.

This script does NOT run automatically as part of the test suite or CI — it
spends real API tokens against live Qwen. Deliverable E: `--help` must work
and the unit-testable pieces (Condition A's synthetic task construction) are
covered by tests/test_eval_harness.py, but nobody should invoke a live run by
accident.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foreman.config import Settings, make_client  # noqa: E402
from foreman.executor import Executor  # noqa: E402
from foreman.models import Handoff, Task  # noqa: E402
from foreman.orchestrator import Orchestrator  # noqa: E402
from foreman.telemetry import METER  # noqa: E402
from foreman.workspace import Workspace  # noqa: E402
from scripts.referee import score_workspace  # noqa: E402

CONDITION_A_MAX_ITERS = 60
CONDITION_B_MAX_ITERS_PER_TASK = 15


# ---- exam construction -------------------------------------------------------


def plan_exam(requirements: str, settings: Settings) -> list[Task]:
    """Plan the checklist exactly once with the real Planner — the frozen exam
    every condition is judged against."""
    from foreman.planner import Planner

    client = make_client(settings)
    planner = Planner(client, settings.planner_model)
    return planner.plan(requirements)


def exam_to_dict(tasks: list[Task]) -> dict:
    return {"tasks": [_task_to_dict(t) for t in tasks]}


def _task_to_dict(t: Task) -> dict:
    return {
        "task_id": t.task_id,
        "title": t.title,
        "description": t.description,
        "acceptance_criteria": list(t.acceptance_criteria),
        "test_strategy": t.test_strategy,
        "role": t.role,
        "complexity_score": t.complexity_score,
        "parents": list(t.parents),
    }


def _checklist_lines(requirements: str) -> list[str]:
    """One acceptance criterion per non-empty, non-comment checklist line —
    same convention main.py's --mock MockPlanner uses for splitting a raw
    checklist, kept consistent so both code paths treat "a line" the same
    way. Leading list markers (1., -, *) are stripped for readability but do
    not change the line count."""
    lines = []
    for raw in requirements.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^(\d+[.)]|[-*])\s*", "", line)
        lines.append(line)
    return lines


def build_condition_a_task(requirements: str) -> Task:
    """Condition A's synthetic task: the entire raw checklist as one task,
    with no exam access (empty test_strategy) and one acceptance criterion
    per checklist line. This is deliberately the "no decomposition, no
    verification" baseline — the real-world pain of "here are 20
    requirements, go"."""
    criteria = _checklist_lines(requirements)
    return Task(
        task_id="A00",
        title="Implement the entire requirements checklist",
        description=requirements,
        acceptance_criteria=criteria,
        test_strategy="",
        role="generalist",
    )


# ---- dependency ordering (shared by Condition B) -----------------------------


def topo_order(tasks: list[Task]) -> list[Task]:
    """Stable dependency-respecting order. The planner already emits tasks in
    dependency order (T01, T02, ... with parents referencing only earlier
    ids), but Condition B's fairness depends on this being actually true, not
    assumed — so it is (re)computed defensively via Kahn's algorithm rather
    than trusted as "the list is already sorted"."""
    by_id = {t.task_id: t for t in tasks}
    indegree = {t.task_id: 0 for t in tasks}
    children: dict[str, list[str]] = {t.task_id: [] for t in tasks}
    for t in tasks:
        for p in t.parents:
            if p in by_id:
                indegree[t.task_id] += 1
                children[p].append(t.task_id)

    ready = [tid for tid, deg in indegree.items() if deg == 0]
    ready.sort(key=lambda tid: tasks.index(by_id[tid]))  # stable: original order among ties
    ordered: list[Task] = []
    while ready:
        tid = ready.pop(0)
        ordered.append(by_id[tid])
        for child in children[tid]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    if len(ordered) != len(tasks):
        # Cycle or dangling dependency slipped past the planner's own
        # validation somehow — fail soft by appending whatever is left in
        # original order rather than dropping tasks from the exam.
        seen = {t.task_id for t in ordered}
        ordered.extend(t for t in tasks if t.task_id not in seen)
    return ordered


# ---- per-condition runners ----------------------------------------------------


def run_condition_a(requirements: str, settings: Settings, workspace_root: Path) -> dict:
    """Single-agent one-shot: one Executor.execute call, the whole checklist
    as one task, no verifier, no retries."""
    METER.reset()
    workspace = Workspace(workspace_root)
    client = make_client(settings)
    executor = Executor(
        client, settings.executor_model, workspace, max_iters=CONDITION_A_MAX_ITERS
    )

    task = build_condition_a_task(requirements)
    start = time.monotonic()
    handoff = executor.execute(task, dependency_handoffs=[])
    elapsed = time.monotonic() - start

    return {
        "workspace": str(workspace.root),
        "elapsed_s": elapsed,
        "executor_attempts": 1,
        "handoffs": [handoff.to_json()],
        "token_usage": METER.snapshot(),
    }


def run_condition_b(exam_tasks: list[Task], settings: Settings, workspace_root: Path) -> dict:
    """Sequential feeding, no verification: one Executor.execute per exam
    task in dependency order, each fed the accumulated handoffs THIS
    condition itself produced so far (not the ledger's — Condition B has no
    ledger). Whatever is handed back is accepted unconditionally."""
    METER.reset()
    workspace = Workspace(workspace_root)
    client = make_client(settings)
    executor = Executor(
        client, settings.executor_model, workspace, max_iters=CONDITION_B_MAX_ITERS_PER_TASK
    )

    ordered = topo_order(exam_tasks)
    handoffs_by_task: dict[str, Handoff] = {}
    handoffs: list[str] = []

    start = time.monotonic()
    for task in ordered:
        dep_handoffs = [handoffs_by_task[p] for p in task.parents if p in handoffs_by_task]
        handoff = executor.execute(task, dependency_handoffs=dep_handoffs)
        handoffs_by_task[task.task_id] = handoff
        handoffs.append(handoff.to_json())
    elapsed = time.monotonic() - start

    return {
        "workspace": str(workspace.root),
        "elapsed_s": elapsed,
        "executor_attempts": len(ordered),
        "handoffs": handoffs,
        "token_usage": METER.snapshot(),
    }


def run_condition_c(requirements: str, exam_tasks: list[Task], settings: Settings, run_root: Path) -> dict:
    """Full Foreman: Orchestrator.run_tasks with the SAME frozen exam tasks,
    normal verifier/retries/dispute, in a fresh run directory."""
    METER.reset()
    orch = Orchestrator(settings, run_root=str(run_root))

    start = time.monotonic()
    # run_tasks takes the tasks as given -- pass fresh Task copies so no
    # mutable state (attempt_count etc.) leaks across conditions if the exam
    # list is reused elsewhere in the same process.
    fresh_tasks = [Task(**{**_task_to_dict(t), "parents": list(t.parents)}) for t in exam_tasks]
    summary = orch.run_tasks(requirements, fresh_tasks)
    elapsed = time.monotonic() - start

    return {
        "workspace": str(orch.workspace.root),
        "run_dir": str(orch.run_dir),
        "elapsed_s": elapsed,
        "executor_attempts": summary["claims"],
        "orchestrator_summary": summary,
        "token_usage": METER.snapshot(),
    }


# ---- referee + reporting -----------------------------------------------------


def _llm_calls(token_usage: dict) -> int:
    return token_usage.get("grand_total", {}).get("calls", 0)


def score_condition(workspace_root: Path, exam: list[dict]) -> dict:
    workspace = Workspace(workspace_root)
    report = score_workspace(workspace, exam)
    return report.as_dict()


def markdown_table(results: dict) -> str:
    header = (
        "| Condition | Referee pass | Wall-clock (s) | Executor attempts | "
        "LLM calls | Total tokens |\n"
        "|---|---|---|---|---|---|\n"
    )
    rows = []
    for cond, data in results["conditions"].items():
        referee = data["referee"]
        rows.append(
            f"| {cond} | {referee['n_passed']}/{referee['n_tasks']} "
            f"(pytest {'PASS' if referee['pytest_passed'] else 'FAIL'}) "
            f"| {data['elapsed_s']:.1f} | {data['executor_attempts']} "
            f"| {_llm_calls(data['token_usage'])} "
            f"| {data['token_usage']['grand_total']['total_tokens']} |\n"
        )
    note = (
        "\nNote: single-run measurements, not averaged over repeats — LLM "
        "sampling variance means a rerun can shift these numbers; treat as "
        "one data point, not a definitive ranking.\n"
    )
    return header + "".join(rows) + note


# ---- CLI ----------------------------------------------------------------------


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate Foreman: same checklist, same model, same tools, three "
            "conditions (A: single-agent one-shot, B: sequential no-verify, "
            "C: full Foreman), one independent referee."
        )
    )
    parser.add_argument("--checklist", required=True, help="path to a requirements checklist (markdown)")
    parser.add_argument(
        "--conditions", default="ABC",
        help="which conditions to run, any subset of the letters A/B/C (default: ABC)",
    )
    parser.add_argument("--out", default="evals", help="directory to write exam.json and results_*.json into")
    args = parser.parse_args(argv)

    conditions = "".join(sorted(set(args.conditions.upper()) & {"A", "B", "C"}))
    if not conditions:
        parser.error("--conditions must include at least one of A, B, C")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    requirements = Path(args.checklist).read_text(encoding="utf-8")
    settings = Settings.from_env()

    # ---- 1. plan once -----------------------------------------------------
    print(f"Planning exam once with {settings.planner_model} ...")
    exam_tasks = plan_exam(requirements, settings)
    exam_dict = exam_to_dict(exam_tasks)
    exam_path = out_dir / "exam.json"
    exam_path.write_text(json.dumps(exam_dict, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  {len(exam_tasks)} tasks planned; frozen exam saved to {exam_path}")

    ts = _utc_timestamp()
    results: dict = {"checklist": args.checklist, "timestamp_utc": ts, "n_exam_tasks": len(exam_tasks), "conditions": {}}

    workspaces_root = out_dir / f"workspaces_{ts}"
    workspaces_root.mkdir(parents=True, exist_ok=True)

    if "A" in conditions:
        print("\n=== Condition A: single-agent one-shot ===")
        data = run_condition_a(requirements, settings, workspaces_root / "condition_a")
        data["referee"] = score_condition(Path(data["workspace"]), exam_dict["tasks"])
        results["conditions"]["A"] = data

    if "B" in conditions:
        print("\n=== Condition B: sequential feeding, no verification ===")
        data = run_condition_b(exam_tasks, settings, workspaces_root / "condition_b")
        data["referee"] = score_condition(Path(data["workspace"]), exam_dict["tasks"])
        results["conditions"]["B"] = data

    if "C" in conditions:
        print("\n=== Condition C: full Foreman ===")
        data = run_condition_c(requirements, exam_tasks, settings, workspaces_root / "condition_c_runs")
        data["referee"] = score_condition(Path(data["workspace"]), exam_dict["tasks"])
        results["conditions"]["C"] = data

    results_path = out_dir / f"results_{ts}.json"
    results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nResults written to {results_path}\n")
    print(markdown_table(results))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
