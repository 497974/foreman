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

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env", {})
        # simulate qwen-code writing a file into the workspace
        (tmp_path / "hello.py").write_text("print('hi')", encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stdout="Created hello.py", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
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

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

    monkeypatch.setattr(subprocess, "run", fake_run)
    handoff = QwenCodeBackend(_FakeSettings(), ws, timeout_s=1).execute(_task(), [])
    assert handoff.outcome == AttemptOutcome.TIMEOUT.value
