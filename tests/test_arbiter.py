"""Arbiter/dispute unit tests. Run: pytest -q (no API key, no network).

Mirrors tests/test_verifier.py's FakeClient/FakeChatCompletions pattern: a
scripted client.chat.completions.create returns canned JSON strings in call
order, matching what foreman.llm.chat_json expects.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foreman.arbiter import Arbiter, solicit_dispute
from foreman.models import Handoff, Task
from foreman.verifier import VerificationReport


class _Msg:
    def __init__(self, content: str):
        self.content = content


class _Choice:
    def __init__(self, content: str):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content: str):
        self.choices = [_Choice(content)]


class FakeChatCompletions:
    def __init__(self, canned: list[str]):
        self._canned = list(canned)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if not self._canned:
            raise AssertionError("FakeChatCompletions ran out of canned responses")
        content = self._canned.pop(0)
        return _Resp(content)


class FakeChat:
    def __init__(self, canned: list[str]):
        self.completions = FakeChatCompletions(canned)


class FakeClient:
    def __init__(self, canned: list[str]):
        self.chat = FakeChat(canned)


class FakeWorkspace:
    """Duck-types Workspace.read_file for the Arbiter's evidence gathering."""

    def __init__(self, files: dict[str, str] | None = None):
        self.files = files or {}
        self.read_calls: list[str] = []

    def read_file(self, path: str) -> str:
        self.read_calls.append(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


def make_task(**kw) -> Task:
    defaults = dict(
        task_id="T02",
        title="add foo()",
        description="Expose foo() from utils.py",
        acceptance_criteria=["foo() exists and returns 42"],
        test_strategy="pytest -q",
    )
    defaults.update(kw)
    return Task(**defaults)


def make_handoff(**kw) -> Handoff:
    defaults = dict(
        task_id="T02", attempt_no=1, files_touched=["utils.py"],
        completed_work=["implemented foo()"],
    )
    defaults.update(kw)
    return Handoff(**defaults)


def make_report(**kw) -> VerificationReport:
    defaults = dict(
        passed=False,
        coverage_rate=0.0,
        items=[{"criterion": "foo() exists and returns 42", "status": "not_satisfied", "detail": "no foo() found"}],
        objective_gate={"command": "pytest -q", "exit_code": 0, "passed": True, "output_tail": ""},
        actionable_feedback=["utils.py: expected foo() returning 42, found nothing"],
        reason="0/1 criteria; gate exit=0",
    )
    defaults.update(kw)
    return VerificationReport(**defaults)


# ---- solicit_dispute ---------------------------------------------------------


def test_solicit_dispute_concede():
    client = FakeClient(['{"dispute": false, "rebuttal": "", "evidence": []}'])
    data = solicit_dispute(client, "mock-executor", make_task(), make_handoff(), make_report())
    assert data["dispute"] is False
    assert data["evidence"] == []


def test_solicit_dispute_with_evidence():
    canned = '{"dispute": true, "rebuttal": "foo() is right there", ' \
             '"evidence": [{"file": "utils.py", "claim": "defines foo() returning 42"}]}'
    client = FakeClient([canned])
    data = solicit_dispute(client, "mock-executor", make_task(), make_handoff(), make_report())
    assert data["dispute"] is True
    assert data["rebuttal"] == "foo() is right there"
    assert data["evidence"] == [{"file": "utils.py", "claim": "defines foo() returning 42"}]


# ---- Arbiter.rule -------------------------------------------------------------


def test_arbiter_overturn_reads_actual_file_contents():
    ws = FakeWorkspace(files={"utils.py": "def foo():\n    return 42\n"})
    canned = '{"ruling": "overturn", "reasoning": "utils.py clearly defines foo() returning 42", ' \
             '"criteria_clarification": ""}'
    client = FakeClient([canned])
    arbiter = Arbiter(client, "qwen-max", ws)

    ruling = arbiter.rule(
        make_task(), make_handoff(), make_report(), "foo() is right there",
        [{"file": "utils.py", "claim": "defines foo() returning 42"}],
    )

    assert ruling["ruling"] == "overturn"
    assert "utils.py" in ruling["reasoning"]
    # the arbiter must actually read the file itself, not trust the claim
    assert "utils.py" in ws.read_calls


def test_arbiter_uphold_returns_clarification():
    ws = FakeWorkspace(files={"utils.py": "def bar():\n    return 1\n"})
    canned = '{"ruling": "uphold", "reasoning": "utils.py has no foo() at all", ' \
             '"criteria_clarification": "add a function literally named foo() returning 42"}'
    client = FakeClient([canned])
    arbiter = Arbiter(client, "qwen-max", ws)

    ruling = arbiter.rule(
        make_task(), make_handoff(), make_report(), "foo() is right there",
        [{"file": "utils.py", "claim": "defines foo() returning 42"}],
    )

    assert ruling["ruling"] == "uphold"
    assert "add a function literally named foo()" in ruling["criteria_clarification"]


def test_arbiter_unparseable_ruling_fails_closed_to_uphold():
    ws = FakeWorkspace(files={"utils.py": "whatever"})
    canned = '{"ruling": "maybe", "reasoning": "unsure", "criteria_clarification": ""}'
    client = FakeClient([canned])
    arbiter = Arbiter(client, "qwen-max", ws)

    ruling = arbiter.rule(make_task(), make_handoff(), make_report(), "r", [{"file": "utils.py", "claim": "x"}])

    assert ruling["ruling"] == "uphold"


def test_arbiter_missing_evidence_file_reported_not_crashed():
    ws = FakeWorkspace(files={})  # no files at all
    canned = '{"ruling": "uphold", "reasoning": "no evidence could be verified", ' \
             '"criteria_clarification": "resubmit with a real file"}'
    client = FakeClient([canned])
    arbiter = Arbiter(client, "qwen-max", ws)

    ruling = arbiter.rule(make_task(), make_handoff(), make_report(), "r", [{"file": "missing.py", "claim": "x"}])

    assert ruling["ruling"] == "uphold"
