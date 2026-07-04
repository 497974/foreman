"""Workspace tests. Run: pytest -q  (no API key, no network, no pip installs)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from foreman.workspace import MAX_OUTPUT, CommandResult, Workspace, WorkspaceError


# ---- construction ------------------------------------------------------


def test_init_creates_root_if_missing(tmp_path):
    root = tmp_path / "nested" / "workspace"
    assert not root.exists()
    ws = Workspace(root)
    assert ws.root.is_dir()
    assert ws.root == root.resolve()


# ---- jail -----------------------------------------------------------------


def test_jail_blocks_dotdot_escape(tmp_path):
    ws = Workspace(tmp_path / "ws")
    with pytest.raises(WorkspaceError):
        ws.read_file("../outside.txt")


def test_jail_blocks_absolute_path_escape(tmp_path):
    ws = Workspace(tmp_path / "ws")
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("secret")
    with pytest.raises(WorkspaceError):
        ws.read_file(str(outside))


def test_jail_allows_legitimate_nested_path(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.write_file("a/b/c.txt", "hi")
    assert ws.read_file("a/b/c.txt") == "hi"


# ---- read/write/list roundtrip --------------------------------------------


def test_write_read_list_roundtrip(tmp_path):
    ws = Workspace(tmp_path / "ws")

    msg = ws.write_file("notes.txt", "hello world")
    assert "notes.txt" in msg
    assert "11" in msg  # byte count

    assert ws.read_file("notes.txt") == "hello world"

    ws.write_file("sub/deep.txt", "nested")
    entries = ws.list_dir(".")
    assert "notes.txt" in entries
    assert "sub/" in entries  # directories suffixed with "/"

    sub_entries = ws.list_dir("sub")
    assert sub_entries == ["deep.txt"]


def test_write_file_creates_parent_dirs(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.write_file("a/b/c/d.txt", "deep")
    assert (ws.root / "a" / "b" / "c" / "d.txt").is_file()


def test_read_file_missing_raises_workspace_error(tmp_path):
    ws = Workspace(tmp_path / "ws")
    with pytest.raises(WorkspaceError):
        ws.read_file("nope.txt")


def test_list_dir_missing_raises_workspace_error(tmp_path):
    ws = Workspace(tmp_path / "ws")
    with pytest.raises(WorkspaceError):
        ws.list_dir("nope")


def test_list_dir_defaults_to_root(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.write_file("x.txt", "x")
    assert ws.list_dir() == ["x.txt"]


# ---- run() ------------------------------------------------------------


def test_run_captures_exit_code_and_stdout(tmp_path):
    ws = Workspace(tmp_path / "ws")
    result = ws.run(f'{sys.executable} -c "print(1 + 1)"')
    assert isinstance(result, CommandResult)
    assert result.exit_code == 0
    assert result.stdout.strip() == "2"
    assert result.timed_out is False


def test_run_captures_nonzero_exit_and_stderr(tmp_path):
    ws = Workspace(tmp_path / "ws")
    result = ws.run(
        f'{sys.executable} -c "import sys; sys.stderr.write(\'boom\'); sys.exit(3)"'
    )
    assert result.exit_code == 3
    assert "boom" in result.stderr


def test_run_uses_workspace_as_cwd(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.write_file("marker.txt", "here")
    result = ws.run(
        f'{sys.executable} -c "import os; print(os.path.exists(\'marker.txt\'))"'
    )
    assert result.stdout.strip() == "True"


def test_run_timeout_sets_flag_and_negative_exit_code(tmp_path):
    ws = Workspace(tmp_path / "ws")
    result = ws.run(
        f'{sys.executable} -c "import time; time.sleep(5)"', timeout=0.2
    )
    assert result.timed_out is True
    assert result.exit_code == -1


def test_run_truncates_long_output(tmp_path):
    ws = Workspace(tmp_path / "ws")
    result = ws.run(
        f'{sys.executable} -c "print(\'x\' * 50000)"'
    )
    assert len(result.stdout) <= MAX_OUTPUT + 100  # small allowance for the truncation note
    assert "truncated" in result.stdout
