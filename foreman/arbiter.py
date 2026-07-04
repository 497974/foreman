"""The negotiation layer: one evidence-gated appeal against a verifier REJECT.

The verifier is an LLM judge — good, but not infallible, and criteria scoring
(unlike the objective gate) is exactly the kind of judgement call that can be
wrong in either direction. Rather than trust the judge unconditionally or let
the executor argue its way past every rejection with rhetoric, Foreman gives
the graded party ONE evidence-based appeal per task per run, arbitrated by a
different, stronger model (qwen-max, same tier as the planner) that is told
in no uncertain terms to check evidence, not eloquence.

Two calls, no tool loops:
  1. ``solicit_dispute`` — the executor model is shown the rejection and asked
     whether it wants to contest it. It either concedes (dispute=false) or
     names specific evidence files backing specific claims.
  2. ``Arbiter.rule`` — a stronger model reads the ACTUAL contents of those
     evidence files (via workspace.read_file, same 4000-char cap the verifier
     itself uses) and rules overturn/uphold. It is explicitly told gate
     results are not up for debate — only the verifier's criteria scoring is
     reviewable, and only against what the files actually contain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .llm import chat_json
from .models import Handoff, Task

if TYPE_CHECKING:
    from .verifier import VerificationReport
    from .workspace import Workspace


MAX_EVIDENCE_FILE_CHARS = 4000  # same cap the verifier uses (contract §3/§6)
MAX_EVIDENCE_FILES = 8  # keep the arbiter's prompt bounded regardless of what the executor names


_DISPUTE_SYSTEM = """You are the executor in an autonomous task-execution system. \
Your work was just REJECTED by the verifier. You may dispute this rejection \
ONLY with concrete evidence — a file, a line, a command output — that \
contradicts the verifier's specific claims. Do not argue tone, effort, or \
intent; argue facts a third party could check.

If you cannot point to specific evidence that the verifier's scoring is \
wrong, concede: set "dispute" to false. Disputing without evidence wastes \
an appeal you only get once per task, so do not dispute out of stubbornness.

Output JSON of exactly this shape:
{"dispute": true|false, "rebuttal": "why the verifier is wrong, if disputing else empty string",
 "evidence": [{"file": "path/relative/to/workspace", "claim": "what this file actually shows"}]}"""


_ARBITER_SYSTEM = """You are the Arbiter in an autonomous task-execution system — \
the final word when an executor disputes a verifier's rejection. You sit above \
both: the verifier's job was scoring acceptance criteria against evidence; \
your job is checking whether the verifier's criteria scoring actually matches \
the file contents you are given.

Gate results (the objective, deterministic pass/fail command) are NOT \
disputable — machines outrank rhetoric, and if any gate failed you must \
uphold. Your review is scoped ONLY to the verifier's per-criterion scoring \
(satisfied / partially_satisfied / not_satisfied) against the acceptance \
criteria, using the actual evidence file contents below — not the executor's \
rebuttal prose, not the verifier's prose. If the evidence does not clearly \
contradict the verifier, uphold: ties go to the verifier, since the executor \
is the party with an incentive to shade the truth.

Output JSON of exactly this shape:
{"ruling": "overturn"|"uphold", "reasoning": "one or two sentences citing the evidence",
 "criteria_clarification": "if uphold, a precise note on what is still missing so the next attempt can fix it; empty string if overturn"}"""


def solicit_dispute(
    client,
    model: str,
    task: Task,
    handoff: Handoff,
    report: "VerificationReport",
) -> dict[str, Any]:
    """Ask the executor model whether it wants to contest a rejection.

    Single chat_json call, no tools — this is a judgement question ("do you
    have evidence"), not a work loop. Returns the raw dict with a hard-coded
    default should the model omit fields, so callers never need extra
    None-checks.
    """
    user = (
        f"Task: {task.title}\n\n"
        f"Description:\n{task.description}\n\n"
        f"Acceptance criteria:\n"
        + "\n".join(f"- {c}" for c in task.acceptance_criteria)
        + "\n\nYour handoff summary:\n"
        + "\n".join(f"- {c}" for c in handoff.completed_work)
        + f"\n\nFiles you reported touching: {', '.join(handoff.files_touched) or '(none)'}"
        + "\n\nVerifier's per-criterion scoring:\n"
        + "\n".join(
            f"- {it.get('criterion', '')}: {it.get('status', '')} — {it.get('detail', '')}"
            for it in report.items
        )
        + "\n\nVerifier's feedback:\n"
        + "\n".join(f"- {f}" for f in report.actionable_feedback)
        + f"\n\nVerifier's verdict reason: {report.reason}"
    )
    data = chat_json(client, model, system=_DISPUTE_SYSTEM, user=user, temperature=0.2)
    return {
        "dispute": bool(data.get("dispute", False)),
        "rebuttal": str(data.get("rebuttal", "") or ""),
        "evidence": data.get("evidence") or [],
    }


class Arbiter:
    """Reads the actual evidence files and rules on a disputed rejection.

    Deliberately uses the planner-tier model (qwen-max in Settings), not the
    executor or verifier models — the arbiter is meant to out-rank both.
    """

    def __init__(self, client, model: str, workspace: "Workspace"):
        self.client = client
        self.model = model
        self.workspace = workspace

    def rule(
        self,
        task: Task,
        handoff: Handoff,
        report: "VerificationReport",
        rebuttal: str,
        evidence: list[dict],
    ) -> dict[str, Any]:
        """Rule overturn/uphold after reading the evidence files from disk.

        The executor's ``evidence`` list only *names* files and claims; the
        arbiter must never trust the claim text alone (same "grader must not
        depend on the graded party's self-report" principle the verifier
        applies to files_touched). We re-read every named file ourselves,
        capped the same way the verifier caps them, and hand the LLM the
        actual bytes.
        """
        file_blobs: list[str] = []
        seen: set[str] = set()
        for item in evidence[:MAX_EVIDENCE_FILES]:
            path = str(item.get("file", "") or "").strip()
            if not path or path in seen:
                continue
            seen.add(path)
            try:
                content = self.workspace.read_file(path)
            except Exception as e:
                file_blobs.append(f"--- {path} ---\n<could not read {path}: {e}>")
                continue
            truncated = content[:MAX_EVIDENCE_FILE_CHARS]
            if len(content) > MAX_EVIDENCE_FILE_CHARS:
                truncated += "\n...[truncated]"
            file_blobs.append(f"--- {path} ---\n{truncated}")

        evidence_summary = "\n".join(
            f"- {item.get('file', '')}: claims \"{item.get('claim', '')}\""
            for item in evidence[:MAX_EVIDENCE_FILES]
        ) or "(no evidence files named)"

        user = (
            f"Task: {task.title}\n\n"
            f"Description:\n{task.description}\n\n"
            f"Acceptance criteria:\n"
            + "\n".join(f"- {c}" for c in task.acceptance_criteria)
            + "\n\nObjective gate result (NOT disputable):\n"
            + f"command=`{report.objective_gate.get('command', '')}` "
            + f"exit_code={report.objective_gate.get('exit_code')} "
            + f"passed={report.objective_gate.get('passed')}\n"
            + "\nVerifier's per-criterion scoring:\n"
            + "\n".join(
                f"- {it.get('criterion', '')}: {it.get('status', '')} — {it.get('detail', '')}"
                for it in report.items
            )
            + f"\n\nVerifier's verdict reason: {report.reason}"
            + f"\n\nExecutor's rebuttal: {rebuttal}"
            + "\n\nEvidence the executor pointed to (claims, not yet verified):\n"
            + evidence_summary
            + "\n\nActual contents of the named evidence files (ground truth):\n"
            + ("\n\n".join(file_blobs) if file_blobs else "(no evidence files could be read)")
        )

        data = chat_json(self.client, self.model, system=_ARBITER_SYSTEM, user=user, temperature=0.0)
        ruling = data.get("ruling")
        if ruling not in ("overturn", "uphold"):
            # Fail closed: an unparseable ruling is treated as uphold, same
            # "ties go to the verifier" principle spelled out in the prompt.
            ruling = "uphold"
        return {
            "ruling": ruling,
            "reasoning": str(data.get("reasoning", "") or ""),
            "criteria_clarification": str(data.get("criteria_clarification", "") or ""),
        }
