"""Pluggable executor backend tests — no real qwen-code, no API key.

The QwenCodeBackend is exercised with a fake subprocess.run so the logic
(env wiring, workspace change detection, exit-code -> outcome mapping, Handoff
translation) is covered without needing the Node.js CLI or a live model.
"""

import os
import subprocess
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from foreman import backends
from foreman.backends import (
    QwenCodeBackend,
    _build_handoff,
    _changed_files,
    _snapshot,
    make_executor,
)
from foreman.executor import Executor
from foreman.models import AttemptOutcome, Task
from foreman.workspace import Workspace


def _task(tid="T01"):
    return Task(task_id=tid, title="t", description="do it",
                acceptance_criteria=["works"], test_strategy="python -m pytest -q")


class _FakeSettings:
    api_key = "sk-fake"
    base_url = "https://example/compatible-mode/v1"
    executor_model = "qwen-plus"
    executor_backend = "qwen-code"


# ---- factory ---------------------------------------------------------------


def test_make_executor_defaults_to_native(tmp_path):
    class S:
        executor_model = "qwen-plus"
        # no executor_backend attr -> default native
    ex = make_executor(S(), Workspace(tmp_path), client=object())
    assert isinstance(ex, Executor)


def test_make_executor_native_explicit(tmp_path):
    class S:
        executor_model = "qwen-plus"
        executor_backend = "native"
    ex = make_executor(S(), Workspace(tmp_path), client=object())
    assert isinstance(ex, Executor)


def test_make_executor_qwen_code(tmp_path, monkeypatch):
    monkeypatch.setattr(backends, "find_qwen_bin", lambda: "qwen")
    ex = make_executor(_FakeSettings(), Workspace(tmp_path), client=object())
    assert isinstance(ex, QwenCodeBackend)


def test_qwen_backend_missing_binary_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(backends, "find_qwen_bin", lambda: None)
    with pytest.raises(RuntimeError, match="qwen-code CLI not found"):
        QwenCodeBackend(_FakeSettings(), Workspace(tmp_path))


# ---- change detection ------------------------------------------------------


def test_snapshot_and_changed_files(tmp_path):
    (tmp_path / "a.py").write_text("one", encoding="utf-8")
    before = _snapshot(tmp_path)
    (tmp_path / "a.py").write_text("one longer now", encoding="utf-8")  # size change
    (tmp_path / "b.py").write_text("new file", encoding="utf-8")
    after = _snapshot(tmp_path)
    changed = _changed_files(before, after)
    assert "a.py" in changed and "b.py" in changed


def test_snapshot_skips_junk(tmp_path):
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.pyc").write_text("junk", encoding="utf-8")
    (tmp_path / "ledger.db").write_text("db", encoding="utf-8")
    (tmp_path / "real.py").write_text("code", encoding="utf-8")
    snap = _snapshot(tmp_path)
    assert "real.py" in snap
    assert not any("pycache" in k or k.endswith(".db") for k in snap)


# ---- exit-code -> outcome mapping -----------------------------------------


@pytest.mark.parametrize("code,timed_out,expected", [
    (0, False, AttemptOutcome.SUCCESS),
    (55, False, AttemptOutcome.TIMEOUT),   # budget exceeded
    (53, False, AttemptOutcome.TIMEOUT),   # turn limit
    (-1, True, AttemptOutcome.TIMEOUT),    # wall-clock timeout
    (1, False, AttemptOutcome.CRASHED),
])
def test_build_handoff_outcome_map(code, timed_out, expected):
    h = _build_handoff(_task(), code, timed_out, "did the thing", "", ["hello.py"])
    assert h.outcome == expected.value
    assert h.files_touched == ["hello.py"]


def test_build_handoff_records_error_on_failure():
    h = _build_handoff(_task(), 1, False, "", "boom traceback", [])
    assert h.outcome == AttemptOutcome.CRASHED.value
    assert any("boom traceback" in g for g in h.gotchas)


# ---- full execute() with a fake subprocess --------------------------------


def test_qwen_backend_execute_success(tmp_path, monkeypatch):
    monkeypatch.setattr(backends, "find_qwen_bin", lambda: "qwen")
    ws = Workspace(tmp_path)
    captured = {}

    def fake_capture(cmd, cwd, env, timeout_s):
        captured["cmd"] = cmd
        captured["env"] = env
        # simulate qwen-code writing a file into the workspace
        (tmp_path / "hello.py").write_text("print('hi')", encoding="utf-8")
        return 0, False, "Created hello.py", ""

    monkeypatch.setattr(backends, "_run_cli_capture", fake_capture)
    backend = QwenCodeBackend(_FakeSettings(), ws)
    handoff = backend.execute(_task(), [])

    assert handoff.outcome == AttemptOutcome.SUCCESS.value
    assert "hello.py" in handoff.files_touched
    # DashScope provider env was wired for the CLI
    assert captured["env"]["OPENAI_API_KEY"] == "sk-fake"
    assert captured["env"]["OPENAI_MODEL"] == "qwen-plus"
    # -y/--yolo is what auto-executes tool calls in headless mode
    assert "--yolo" in captured["cmd"]
    assert "-p" in captured["cmd"]


def test_qwen_backend_execute_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(backends, "find_qwen_bin", lambda: "qwen")
    ws = Workspace(tmp_path)

    def fake_capture(cmd, cwd, env, timeout_s):
        return -1, True, "", ""  # (exit, timed_out, stdout, stderr)

    monkeypatch.setattr(backends, "_run_cli_capture", fake_capture)
    handoff = QwenCodeBackend(_FakeSettings(), ws, timeout_s=1).execute(_task(), [])
    assert handoff.outcome == AttemptOutcome.TIMEOUT.value


# ---- HermesBackend ---------------------------------------------------------


def test_make_executor_hermes(tmp_path, monkeypatch):
    monkeypatch.setattr(backends, "find_hermes_bin", lambda: "hermes")
    s = _FakeSettings()
    s.executor_backend = "hermes"
    ex = backends.make_executor(s, Workspace(tmp_path / "ws"), client=None)
    assert isinstance(ex, backends.HermesBackend)


def test_hermes_backend_missing_binary_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(backends, "find_hermes_bin", lambda: None)
    import pytest
    with pytest.raises(RuntimeError, match="hermes-agent CLI not found"):
        backends.HermesBackend(_FakeSettings(), Workspace(tmp_path / "ws"))


def test_hermes_backend_execute_success(tmp_path, monkeypatch):
    """Scripted subprocess: 'hermes' run exits 0 and writes a file; the
    handoff must be SUCCESS with the changed file detected by snapshot diff,
    and the invocation must use headless flags (-z --yolo --quiet) with cwd
    at the workspace root and the model passed via HERMES_INFERENCE_MODEL."""
    monkeypatch.setattr(backends, "find_hermes_bin", lambda: "hermes")
    ws = Workspace(tmp_path / "ws")
    seen = {}

    def fake_capture(cmd, cwd, env, timeout_s):
        seen["cmd"], seen["cwd"], seen["env"] = cmd, cwd, env
        (ws.root / "made_by_hermes.py").write_text("x = 1\n", encoding="utf-8")
        return 0, False, "did the thing\n", ""

    monkeypatch.setattr(backends, "_run_cli_capture", fake_capture)
    backend = backends.HermesBackend(_FakeSettings(), ws)
    task = _task()
    h = backend.execute(task, [])

    assert h.outcome == AttemptOutcome.SUCCESS.value
    assert "made_by_hermes.py" in h.files_touched
    assert seen["cmd"][0] == "hermes" and "-z" in seen["cmd"]
    assert "--yolo" in seen["cmd"] and "--quiet" not in seen["cmd"]
    assert str(seen["cwd"]) == str(ws.root)
    assert seen["env"]["HERMES_INFERENCE_MODEL"] == _FakeSettings().executor_model
    assert "hermes backend" in h.handoff_reason


def test_hermes_backend_execute_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(backends, "find_hermes_bin", lambda: "hermes")
    ws = Workspace(tmp_path / "ws")

    def fake_capture(cmd, cwd, env, timeout_s):
        return -1, True, "", ""

    monkeypatch.setattr(backends, "_run_cli_capture", fake_capture)
    h = backends.HermesBackend(_FakeSettings(), ws).execute(_task(), [])
    assert h.outcome == AttemptOutcome.TIMEOUT.value


def test_hermes_backend_api_error_with_no_changes_is_crashed(tmp_path, monkeypatch):
    """Caught live: on a 403 (quota exhausted) hermes prints the HTTP error
    as its final answer and exits 0 — a false SUCCESS that burned a verifier
    call and a retry slot per attempt. Exit 0 + zero changed files + an
    HTTP/quota error in stdout must be classified CRASHED."""
    monkeypatch.setattr(backends, "find_hermes_bin", lambda: "hermes")
    ws = Workspace(tmp_path / "ws")

    def fake_capture(cmd, cwd, env, timeout_s):
        return 0, False, "HTTP 403: The free quota has been exhausted. To continue...", ""

    monkeypatch.setattr(backends, "_run_cli_capture", fake_capture)
    h = backends.HermesBackend(_FakeSettings(), ws).execute(_task(), [])
    assert h.outcome == AttemptOutcome.CRASHED.value
    assert h.files_touched == []
    assert "API error" in h.gotchas[0]


def test_hermes_backend_exit0_with_real_changes_stays_success(tmp_path, monkeypatch):
    """The inverse guard: a run that actually wrote files stays SUCCESS even
    if its narration happens to mention an HTTP error string."""
    monkeypatch.setattr(backends, "find_hermes_bin", lambda: "hermes")
    ws = Workspace(tmp_path / "ws")

    def fake_capture(cmd, cwd, env, timeout_s):
        (ws.root / "real_work.py").write_text("x = 1\n", encoding="utf-8")
        return 0, False, "retried after HTTP 429, then finished", ""

    monkeypatch.setattr(backends, "_run_cli_capture", fake_capture)
    h = backends.HermesBackend(_FakeSettings(), ws).execute(_task(), [])
    assert h.outcome == AttemptOutcome.SUCCESS.value
    assert "real_work.py" in h.files_touched


# ---- review-audit regression tests -----------------------------------------


def test_changed_files_reports_deletions(tmp_path):
    """A delete-only run must not report zero changes (that both understates
    files_touched and can misfire the exit-0-no-change false-success guard)."""
    from foreman.backends import _changed_files
    before = {"a.py": (10, 1.0), "gone.py": (5, 1.0)}
    after = {"a.py": (10, 1.0)}  # gone.py deleted
    assert _changed_files(before, after) == ["gone.py"]


def test_run_cli_capture_launch_failure_is_crashed(tmp_path):
    """A binary that cannot be launched (OSError) must come back as a normal
    crash tuple, not raise out of execute() and abort the whole run."""
    from foreman.backends import _run_cli_capture
    exit_code, timed_out, stdout, stderr = _run_cli_capture(
        ["this_binary_does_not_exist_xyz"], str(tmp_path), {}, 5.0
    )
    assert exit_code == -1 and timed_out is False
    assert "failed to launch" in stderr


def test_hermes_launch_failure_becomes_crashed_handoff(tmp_path, monkeypatch):
    """End to end: a launch failure inside execute() yields a CRASHED handoff,
    never an exception that would kill the orchestrator loop."""
    monkeypatch.setattr(backends, "find_hermes_bin", lambda: "hermes")
    ws = Workspace(tmp_path / "ws")

    def boom_capture(cmd, cwd, env, timeout_s):
        return -1, False, "", "failed to launch agent CLI: [WinError 206]"

    monkeypatch.setattr(backends, "_run_cli_capture", boom_capture)
    h = backends.HermesBackend(_FakeSettings(), ws).execute(_task(), [])
    assert h.outcome == AttemptOutcome.CRASHED.value
