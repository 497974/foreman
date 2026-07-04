"""Verifier tests. Run: pytest -q  (no API key, no network, no pip installs).

A minimal FakeWorkspace stands in for foreman/workspace.py (built by another
agent in parallel): it implements the same method signatures (run/read_file/
list_dir) the Verifier duck-types against, so these tests run whether or not
that module has landed yet.
"""

import json
import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from foreman.models import Handoff, Task
from foreman.verifier import Verifier, VerificationReport


# ---- fakes ------------------------------------------------------------------


@dataclass
class FakeCommandResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.01
    timed_out: bool = False


class FakeWorkspace:
    """Duck-types foreman.workspace.Workspace's run/read_file/list_dir.

    `run` is scripted: callers register an exit code (and optional
    stdout/stderr) per exact command string; unregistered commands default to
    a clean exit_code=0 pass so tests only need to script what they care about.
    """

    def __init__(self, files: dict[str, str] | None = None, dirs: dict[str, list[str]] | None = None):
        self.files = files or {}
        # dirs maps a relative dir path -> list of entries (dirs suffixed "/")
        self.dirs = dirs or {".": []}
        self.command_results: dict[str, FakeCommandResult] = {}
        self.calls: list[str] = []

    def script_command(self, command: str, exit_code: int, stdout: str = "", stderr: str = "") -> None:
        self.command_results[command] = FakeCommandResult(exit_code, stdout, stderr)

    def run(self, command: str, timeout: float = 120.0) -> FakeCommandResult:
        self.calls.append(command)
        return self.command_results.get(command, FakeCommandResult(exit_code=0, stdout="ok"))

    def read_file(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def list_dir(self, path: str = ".") -> list[str]:
        return self.dirs.get(path, [])


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
    """Scripted client.chat.completions.create — returns canned JSON strings
    in order, one per call, mirroring the shape chat_json() expects
    (resp.choices[0].message.content)."""

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


def make_task(**kw) -> Task:
    defaults = dict(
        task_id="T01",
        title="add health endpoint",
        description="Add GET /health returning 200.",
        acceptance_criteria=["GET /health returns 200", "response body is JSON"],
        test_strategy="pytest -q tests/test_health.py",
    )
    defaults.update(kw)
    return Task(**defaults)


def make_handoff(**kw) -> Handoff:
    defaults = dict(task_id="T01", files_touched=["app.py"])
    defaults.update(kw)
    return Handoff(**defaults)


def all_satisfied_json(criteria: list[str]) -> str:
    return json.dumps(
        {
            "items": [
                {"criterion": c, "status": "satisfied", "detail": "looks good"}
                for c in criteria
            ],
            "feedback": [],
        }
    )


# ---- tests ------------------------------------------------------------------


def test_gate_failure_short_circuits_to_reject_even_if_llm_says_all_satisfied():
    ws = FakeWorkspace(files={"app.py": "x = 1"})
    ws.script_command(
        "pytest -q tests/test_health.py",
        exit_code=1,
        stdout="",
        stderr="1 failed",
    )
    task = make_task()
    handoff = make_handoff()

    # LLM claims everything is satisfied; the gate must still win.
    client = FakeClient([all_satisfied_json(task.acceptance_criteria)])
    verifier = Verifier(client, model="test-model", workspace=ws)

    report = verifier.verify(task, handoff)

    assert isinstance(report, VerificationReport)
    assert report.passed is False
    assert report.objective_gate["exit_code"] == 1
    assert report.objective_gate["passed"] is False
    assert "gate exit=1" in report.reason


def test_all_satisfied_and_green_gate_passes():
    ws = FakeWorkspace(files={"app.py": "x = 1"})
    ws.script_command("pytest -q tests/test_health.py", exit_code=0, stdout="1 passed")
    task = make_task()
    handoff = make_handoff()

    client = FakeClient([all_satisfied_json(task.acceptance_criteria)])
    verifier = Verifier(client, model="test-model", workspace=ws)

    report = verifier.verify(task, handoff)

    assert report.passed is True
    assert report.coverage_rate == 1.0
    assert report.objective_gate["passed"] is True
    assert all(it["status"] == "satisfied" for it in report.items)
    assert "2/2 criteria" in report.reason


def test_partially_satisfied_lowers_coverage_and_blocks_pass():
    ws = FakeWorkspace(files={"app.py": "x = 1"})
    ws.script_command("pytest -q tests/test_health.py", exit_code=0, stdout="1 passed")
    task = make_task()
    handoff = make_handoff()

    canned = json.dumps(
        {
            "items": [
                {"criterion": task.acceptance_criteria[0], "status": "satisfied", "detail": "ok"},
                {
                    "criterion": task.acceptance_criteria[1],
                    "status": "partially_satisfied",
                    "detail": "body is JSON but missing a required field",
                },
            ],
            "feedback": ["app.py: response is missing the 'status' field"],
        }
    )
    client = FakeClient([canned])
    verifier = Verifier(client, model="test-model", workspace=ws)

    report = verifier.verify(task, handoff)

    assert report.passed is False
    assert report.coverage_rate == pytest.approx(0.75)
    assert report.actionable_feedback[0] == "app.py: response is missing the 'status' field"


def test_unknown_status_is_coerced_to_not_satisfied():
    ws = FakeWorkspace(files={"app.py": "x = 1"})
    ws.script_command("pytest -q tests/test_health.py", exit_code=0, stdout="1 passed")
    task = make_task()
    handoff = make_handoff()

    canned = json.dumps(
        {
            "items": [
                {"criterion": task.acceptance_criteria[0], "status": "satisfied", "detail": "ok"},
                {"criterion": task.acceptance_criteria[1], "status": "kinda_maybe", "detail": "??"},
            ],
            "feedback": [],
        }
    )
    client = FakeClient([canned])
    verifier = Verifier(client, model="test-model", workspace=ws)

    report = verifier.verify(task, handoff)

    assert report.items[1]["status"] == "not_satisfied"
    assert report.passed is False
    assert report.coverage_rate == pytest.approx(0.5)


def test_regression_pytest_gate_triggers_reject_when_test_suite_present():
    # Workspace has a tests/ dir -> Verifier must additionally run the
    # blanket regression gate `python -m pytest -q`, independent of
    # task.test_strategy. Script that command to fail.
    ws = FakeWorkspace(
        files={"app.py": "x = 1"},
        dirs={
            ".": ["app.py", "tests/"],
            "tests": ["test_health.py"],
        },
    )
    ws.script_command("pytest -q tests/test_health.py", exit_code=0, stdout="1 passed")
    ws.script_command("python -m pytest -q", exit_code=1, stdout="", stderr="1 failed, regression")

    task = make_task()
    handoff = make_handoff()

    client = FakeClient([all_satisfied_json(task.acceptance_criteria)])
    verifier = Verifier(client, model="test-model", workspace=ws)

    report = verifier.verify(task, handoff)

    assert report.passed is False
    # the failing regression gate should be surfaced as the objective_gate
    assert report.objective_gate["command"] == "python -m pytest -q"
    assert report.objective_gate["exit_code"] == 1
    assert "python -m pytest -q" in ws.calls


def test_no_test_strategy_and_no_test_suite_skips_gate_but_still_scores():
    ws = FakeWorkspace(files={"app.py": "x = 1"}, dirs={".": ["app.py"]})
    task = make_task(test_strategy="")
    handoff = make_handoff()

    client = FakeClient([all_satisfied_json(task.acceptance_criteria)])
    verifier = Verifier(client, model="test-model", workspace=ws)

    report = verifier.verify(task, handoff)

    assert report.objective_gate["passed"] is True
    assert report.objective_gate["exit_code"] == 0
    assert report.passed is True


def test_missing_file_touched_does_not_crash_verification():
    ws = FakeWorkspace(files={})  # app.py listed in handoff but not present
    ws.script_command("pytest -q tests/test_health.py", exit_code=0, stdout="1 passed")
    task = make_task()
    handoff = make_handoff(files_touched=["missing.py"])

    client = FakeClient([all_satisfied_json(task.acceptance_criteria)])
    verifier = Verifier(client, model="test-model", workspace=ws)

    report = verifier.verify(task, handoff)

    assert report.passed is True  # LLM still saw a placeholder for the missing file


# ---- invalid-gate detection (unwinnable gate must not doom correct work) ----
#
# First live run failure mode: the planner emitted a `python -c` one-liner
# that was a SyntaxError before any of the executor's work ran, so a correct
# implementation burned all three attempts against a test that could never
# pass. An invalid gate neither passes nor fails the attempt; the verdict
# falls back to the regression pytest gate (if present) + criteria coverage.

_SYNTAX_ERROR_TAIL = (
    '  File "<string>", line 1\n'
    "    import app; with app.test_client() as c: pass\n"
    "                ^^^^\n"
    "SyntaxError: invalid syntax\n"
)


def test_syntax_error_gate_with_all_satisfied_and_green_regression_passes_with_warning():
    ws = FakeWorkspace(
        files={"app.py": "x = 1"},
        dirs={".": ["app.py", "tests/"], "tests": ["test_health.py"]},
    )
    bad_gate = 'python -c "import app; with app.test_client() as c: pass"'
    ws.script_command(bad_gate, exit_code=1, stderr=_SYNTAX_ERROR_TAIL)
    ws.script_command("python -m pytest -q", exit_code=0, stdout="2 passed")

    task = make_task(test_strategy=bad_gate)
    handoff = make_handoff()

    client = FakeClient([all_satisfied_json(task.acceptance_criteria)])
    verifier = Verifier(client, model="test-model", workspace=ws)

    report = verifier.verify(task, handoff)

    assert report.passed is True
    assert "gate command invalid" in report.reason
    assert any("gate command invalid" in f for f in report.actionable_feedback)
    # the surfaced objective_gate must be the green regression gate, not the
    # invalid strategy gate
    assert report.objective_gate["command"] == "python -m pytest -q"
    assert report.objective_gate["passed"] is True


def test_syntax_error_gate_with_unsatisfied_criteria_still_rejects():
    ws = FakeWorkspace(
        files={"app.py": "x = 1"},
        dirs={".": ["app.py", "tests/"], "tests": ["test_health.py"]},
    )
    bad_gate = 'python -c "import app; with app.test_client() as c: pass"'
    ws.script_command(bad_gate, exit_code=1, stderr=_SYNTAX_ERROR_TAIL)
    ws.script_command("python -m pytest -q", exit_code=0, stdout="2 passed")

    task = make_task(test_strategy=bad_gate)
    handoff = make_handoff()

    canned = json.dumps(
        {
            "items": [
                {"criterion": task.acceptance_criteria[0], "status": "satisfied", "detail": "ok"},
                {"criterion": task.acceptance_criteria[1], "status": "not_satisfied", "detail": "missing"},
            ],
            "feedback": ["app.py: JSON body missing"],
        }
    )
    client = FakeClient([canned])
    verifier = Verifier(client, model="test-model", workspace=ws)

    report = verifier.verify(task, handoff)

    assert report.passed is False
    assert "gate command invalid" in report.reason  # warning still surfaced


def test_syntax_error_gate_with_failing_regression_rejects():
    ws = FakeWorkspace(
        files={"app.py": "x = 1"},
        dirs={".": ["app.py", "tests/"], "tests": ["test_health.py"]},
    )
    bad_gate = 'python -c "import app; with app.test_client() as c: pass"'
    ws.script_command(bad_gate, exit_code=1, stderr=_SYNTAX_ERROR_TAIL)
    ws.script_command("python -m pytest -q", exit_code=1, stderr="1 failed")

    task = make_task(test_strategy=bad_gate)
    handoff = make_handoff()

    client = FakeClient([all_satisfied_json(task.acceptance_criteria)])
    verifier = Verifier(client, model="test-model", workspace=ws)

    report = verifier.verify(task, handoff)

    assert report.passed is False
    assert report.objective_gate["command"] == "python -m pytest -q"
    assert report.objective_gate["exit_code"] == 1


def test_command_not_found_gate_is_invalid_but_ordinary_test_failure_is_not():
    # 9009 / "is not recognized" (cmd.exe command-not-found) => invalid gate
    ws = FakeWorkspace(files={"app.py": "x = 1"}, dirs={".": ["app.py"]})
    ws.script_command(
        "pyest -q tests/test_health.py",  # typo'd binary
        exit_code=9009,
        stderr="'pyest' is not recognized as an internal or external command,\noperable program or batch file.",
    )
    task = make_task(test_strategy="pyest -q tests/test_health.py")
    handoff = make_handoff()

    client = FakeClient([all_satisfied_json(task.acceptance_criteria)])
    verifier = Verifier(client, model="test-model", workspace=ws)
    report = verifier.verify(task, handoff)

    # no regression suite present -> verdict rests on coverage alone
    assert report.passed is True
    assert "gate command invalid" in report.reason

    # ...whereas an ordinary failing pytest gate (AssertionError inside the
    # work) must remain a hard REJECT even when the LLM says all satisfied.
    ws2 = FakeWorkspace(files={"app.py": "x = 1"}, dirs={".": ["app.py"]})
    ws2.script_command(
        "pytest -q tests/test_health.py",
        exit_code=1,
        stdout="F\nE   AssertionError: expected 200 got 500\n1 failed",
    )
    task2 = make_task()
    client2 = FakeClient([all_satisfied_json(task2.acceptance_criteria)])
    verifier2 = Verifier(client2, model="test-model", workspace=ws2)
    report2 = verifier2.verify(task2, make_handoff())

    assert report2.passed is False
    assert "gate command invalid" not in report2.reason
