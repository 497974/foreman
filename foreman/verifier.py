"""The Verifier: decides whether an executor's handoff actually satisfies a task.

Objective signals come first, LLM judgement last — the same bias toward ground
truth that shows up everywhere else in Foreman (the ledger's state machine, the
planner's runnable test_strategy). Two deterministic gates run before a single
token of model output is trusted:

  1. the task's own ``test_strategy`` command, if the planner gave one;
  2. a blanket ``python -m pytest -q`` regression gate whenever the workspace
     already contains a test suite — so a fix for task N cannot silently break
     tests written for task N-1.

Only after both gates report their exit code does the verifier ask the model to
score each acceptance criterion. A non-zero gate always rejects the attempt
regardless of what the LLM thinks the criteria coverage looks like; the LLM
call still runs in that case purely so the executor gets useful feedback on the
*next* attempt (empty-handed rejections are useless to the retry ladder).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .llm import chat_json
from .models import CriterionStatus, Handoff, Task

if TYPE_CHECKING:
    # foreman/workspace.py is owned by another agent working in parallel; only
    # import it for type checkers so this module never fails to load if that
    # file hasn't landed yet. At runtime Verifier just duck-types `.run`,
    # `.read_file`, `.list_dir`.
    from .workspace import Workspace


_VALID_STATUSES = {s.value for s in CriterionStatus}

MAX_FILE_CHARS = 4000  # per-file truncation fed to the LLM (contract §3)

_SYSTEM = """You are the Verifier in an autonomous task-execution system, acting \
like a strict but fair code reviewer. You are given a task's acceptance \
criteria, the output of objective checks that already ran, a directory \
listing of the workspace, and the contents of the files the executor claims \
to have touched.

Score EVERY acceptance criterion independently as one of:
- "satisfied": clearly met, verifiable from the evidence given.
- "partially_satisfied": some but not all of the criterion is met, or it is
  met but with an obvious gap/edge case missed.
- "not_satisfied": not met, or there is no evidence it was met.

Be skeptical of claims not backed by the evidence shown to you. If the
objective gate command failed, that alone means the relevant criteria cannot
be "satisfied".

Output JSON of exactly this shape:
{"items": [{"criterion": "...", "status": "satisfied", "detail": "..."}],
 "feedback": ["actionable note referencing a file or expected-vs-actual, ..."]}"""


@dataclass
class VerificationReport:
    passed: bool
    coverage_rate: float               # mean of item scores (1 / 0.5 / 0)
    items: list[dict] = field(default_factory=list)
    objective_gate: dict = field(default_factory=dict)
    actionable_feedback: list[str] = field(default_factory=list)
    reason: str = ""


# Signatures of a gate command that is itself broken (unrunnable), as opposed
# to a gate that ran and found the work wanting. Kept deliberately tight: an
# ordinary failing test, an AssertionError, or an ImportError inside the app
# under test must NOT match — those are real work failures the retry ladder
# should punish. What we catch here is the "unwinnable gate" failure mode the
# first live run exposed: the planner emitted a `python -c` one-liner that was
# a SyntaxError before a single line of the executor's work ever ran, so every
# retry burned an attempt against a test that could never pass.
_INVALID_GATE_EXIT_CODES = {9009}  # cmd.exe: command not found

_GATE_INVALID_WARNING = "gate command invalid — verdict based on coverage + regression"


def _gate_is_invalid(gate: dict) -> bool:
    """True when the gate command never actually exercised the work."""
    if gate.get("passed"):
        return False
    if gate.get("exit_code") in _INVALID_GATE_EXIT_CODES:
        return True
    tail = gate.get("output_tail", "") or ""
    # `python -c "<broken one-liner>"` — the SyntaxError points at <string>,
    # i.e. the command text itself, not any file of the executor's work.
    if 'File "<string>"' in tail and "SyntaxError" in tail:
        return True
    if "is not recognized as an internal or external command" in tail:
        return True
    if "command not found" in tail:
        return True
    return False


def _run_gate(workspace: "Workspace", command: str, timeout: float) -> dict:
    """Run one gate command and normalize it to the objective_gate dict shape."""
    result = workspace.run(command, timeout=timeout)
    tail = (result.stdout or "") + (result.stderr or "")
    return {
        "command": command,
        "exit_code": result.exit_code,
        "passed": result.exit_code == 0,
        "output_tail": tail[-2000:],  # keep the report small; enough to act on
    }


def _has_test_suite(workspace: "Workspace") -> bool:
    """Cheap heuristic: does the workspace already contain a pytest-style suite?

    Walks a couple of levels via list_dir rather than shelling out to `find`
    (Windows dev box; contract forbids relying on unix-only tools).
    """
    seen: set[str] = set()
    to_scan = ["."]
    while to_scan:
        rel = to_scan.pop()
        if rel in seen:
            continue
        seen.add(rel)
        try:
            entries = workspace.list_dir(rel)
        except Exception:
            continue
        for entry in entries:
            is_dir = entry.endswith("/")
            name = entry[:-1] if is_dir else entry
            child = name if rel == "." else f"{rel}/{name}"
            if is_dir:
                if name == "tests" or name.startswith("test_"):
                    return True
                # keep the walk shallow-ish to avoid wandering into venvs etc.
                if name not in (".git", "__pycache__", ".venv", "venv", "node_modules"):
                    to_scan.append(child)
            else:
                if name.startswith("test_") and name.endswith(".py"):
                    return True
    return False


def _list_dir_recursive(workspace: "Workspace", rel: str = ".", _depth: int = 0) -> list[str]:
    """Best-effort recursive directory listing to hand the LLM some context."""
    if _depth > 4:
        return []
    out: list[str] = []
    try:
        entries = workspace.list_dir(rel)
    except Exception:
        return out
    for entry in entries:
        is_dir = entry.endswith("/")
        name = entry[:-1] if is_dir else entry
        child = name if rel == "." else f"{rel}/{name}"
        out.append(child + ("/" if is_dir else ""))
        if is_dir and name not in (".git", "__pycache__", ".venv", "venv", "node_modules"):
            out.extend(_list_dir_recursive(workspace, child, _depth + 1))
    return out


def _coerce_status(raw: Any) -> str:
    """Unknown/garbled status strings from the model fail closed, not open."""
    if isinstance(raw, str) and raw in _VALID_STATUSES:
        return raw
    return CriterionStatus.NOT_SATISFIED.value


class Verifier:
    def __init__(
        self,
        client,
        model: str,
        workspace: "Workspace",
        command_timeout: float = 180.0,
    ):
        self.client = client
        self.model = model
        self.workspace = workspace
        self.command_timeout = command_timeout

    def verify(self, task: Task, handoff: Handoff) -> VerificationReport:
        # ---- 1. objective gate(s) ------------------------------------------
        gates: list[dict] = []
        strategy_gate = None
        if task.test_strategy.strip():
            strategy_gate = _run_gate(self.workspace, task.test_strategy, self.command_timeout)
            gates.append(strategy_gate)

        if _has_test_suite(self.workspace):
            gates.append(_run_gate(self.workspace, "python -m pytest -q", self.command_timeout))

        # A gate that is itself unrunnable (SyntaxError in the command text,
        # command not found) neither passes nor fails the attempt — otherwise
        # a planner typo dooms every retry of otherwise-correct work. The
        # verdict then rests on the regression pytest gate (if present) plus
        # criteria coverage, with a loud warning in reason/feedback.
        gate_invalid = strategy_gate is not None and _gate_is_invalid(strategy_gate)
        if gate_invalid:
            strategy_gate["invalid"] = True

        effective_gates = [g for g in gates if not g.get("invalid")]
        gates_green = all(g["passed"] for g in effective_gates) if effective_gates else True
        # objective_gate reports the first failing effective gate if any, else
        # the last effective gate, else the (invalid) strategy gate, else a
        # fully-green default — ledger callers always get exactly one dict.
        objective_gate = next(
            (g for g in effective_gates if not g["passed"]),
            effective_gates[-1] if effective_gates else (strategy_gate or {
                "command": "",
                "exit_code": 0,
                "passed": True,
                "output_tail": "",
            }),
        )

        # ---- 2. coverage scoring via the LLM --------------------------------
        items, feedback = self._score_coverage(task, handoff, gates)

        n = len(items)
        n_satisfied = sum(1 for it in items if it["status"] == CriterionStatus.SATISFIED.value)
        coverage_rate = (
            sum(CriterionStatus(it["status"]).score for it in items) / n if n else 0.0
        )

        # ---- 3. verdict ------------------------------------------------------
        all_satisfied = n > 0 and all(
            it["status"] == CriterionStatus.SATISFIED.value for it in items
        )
        passed = bool(gates_green and all_satisfied)

        # ---- 4. reason + actionable feedback ---------------------------------
        first_feedback = feedback[0] if feedback else ""
        reason = f"{n_satisfied}/{n} criteria; gate exit={objective_gate['exit_code']}"
        if first_feedback:
            reason = f"{reason}; {first_feedback}"

        actionable_feedback = list(feedback)
        if gate_invalid:
            reason = f"{_GATE_INVALID_WARNING} | {reason}"
            actionable_feedback.insert(
                0,
                f"{_GATE_INVALID_WARNING} (unrunnable gate: `{strategy_gate['command']}`)",
            )
        if not gates_green:
            tail = objective_gate.get("output_tail", "").strip()
            if tail:
                actionable_feedback.append(
                    f"gate `{objective_gate['command']}` exit={objective_gate['exit_code']}: {tail[-500:]}"
                )

        return VerificationReport(
            passed=passed,
            coverage_rate=coverage_rate,
            items=items,
            objective_gate=objective_gate,
            actionable_feedback=actionable_feedback,
            reason=reason,
        )

    # ---- internals -----------------------------------------------------------

    def _score_coverage(
        self, task: Task, handoff: Handoff, gates: list[dict]
    ) -> tuple[list[dict], list[str]]:
        listing = _list_dir_recursive(self.workspace)

        # Evidence gathering must not depend on the executor's self-reported
        # files_touched alone — the party being graded must not control what
        # the grader gets to see. (Learned live: an executor omitted app.py
        # from files_touched; the verifier, unable to see the route, correctly
        # failed closed and rejected work that was actually fine.) So read the
        # touched files first, then sweep the rest of the workspace up to a
        # budget.
        paths = list(handoff.files_touched)
        for entry in listing:
            if entry.endswith("/") or entry in paths:
                continue
            if entry.endswith((".db", ".sqlite", ".pyc", ".png", ".jpg", ".zip")):
                continue
            if len(paths) >= 12:  # evidence budget: plenty for small workspaces
                break
            paths.append(entry)

        file_blobs: list[str] = []
        for path in paths:
            try:
                content = self.workspace.read_file(path)
            except Exception as e:
                content = f"<could not read {path}: {e}>"
            truncated = content[:MAX_FILE_CHARS]
            if len(content) > MAX_FILE_CHARS:
                truncated += "\n...[truncated]"
            file_blobs.append(f"--- {path} ---\n{truncated}")

        gate_summary = "\n".join(
            f"gate `{g['command']}` exit={g['exit_code']} passed={g['passed']}\n"
            f"output tail:\n{g['output_tail']}"
            for g in gates
        ) or "(no objective gate configured for this task)"

        user = (
            f"Task: {task.title}\n\n"
            f"Description:\n{task.description}\n\n"
            f"Acceptance criteria:\n"
            + "\n".join(f"- {c}" for c in task.acceptance_criteria)
            + "\n\nObjective gate results:\n"
            + gate_summary
            + "\n\nWorkspace listing:\n"
            + "\n".join(listing)
            + "\n\nFiles touched by the executor:\n"
            + "\n\n".join(file_blobs)
        )

        data = chat_json(
            self.client,
            self.model,
            system=_SYSTEM,
            user=user,
            max_tokens=4096,
            temperature=0.0,
        )

        raw_items = data.get("items", [])
        items: list[dict] = []
        for it in raw_items:
            items.append(
                {
                    "criterion": it.get("criterion", ""),
                    "status": _coerce_status(it.get("status")),
                    "detail": it.get("detail", ""),
                }
            )
        # If the model dropped criteria entirely, fail closed per criterion
        # rather than silently reporting 0/0 (which would look like a pass).
        if not items and task.acceptance_criteria:
            items = [
                {"criterion": c, "status": CriterionStatus.NOT_SATISFIED.value,
                 "detail": "verifier model returned no scoring for this criterion"}
                for c in task.acceptance_criteria
            ]

        feedback = [str(f) for f in data.get("feedback", []) if str(f).strip()]
        return items, feedback
