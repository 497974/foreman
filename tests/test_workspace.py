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


# ---- search_files ----------------------------------------------------------


def _seed_search_tree(root):
    """A small fake project: nested source, a vendored dir, a binary file."""
    (root / "app").mkdir(parents=True)
    (root / "app" / "routes.py").write_text(
        "from flask import Flask\n\n"
        "@app.route('/claims')\n"
        "def list_claims():\n"
        "    return CLAIMS\n",
        encoding="utf-8",
    )
    (root / "app" / "models.py").write_text(
        "class Claim:\n    status = 'pending'\n", encoding="utf-8"
    )
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text(
        "route route route\n", encoding="utf-8"
    )
    (root / "logo.bin").write_bytes(b"\x00\x01\x02route\x00")


def test_search_files_finds_matches_with_path_and_line_numbers(tmp_path):
    ws = Workspace(tmp_path / "ws")
    _seed_search_tree(ws.root)
    out = ws.search_files("route")
    assert "app/routes.py:3:" in out
    assert "@app.route('/claims')" in out


def test_search_files_is_case_insensitive(tmp_path):
    ws = Workspace(tmp_path / "ws")
    _seed_search_tree(ws.root)
    out = ws.search_files("CLAIM")
    assert "models.py" in out  # matches 'class Claim' despite casing


def test_search_files_skips_vendored_dirs_and_binaries(tmp_path):
    ws = Workspace(tmp_path / "ws")
    _seed_search_tree(ws.root)
    out = ws.search_files("route")
    assert "node_modules" not in out
    assert "logo.bin" not in out


def test_search_files_invalid_regex_falls_back_to_literal(tmp_path):
    ws = Workspace(tmp_path / "ws")
    _seed_search_tree(ws.root)
    # '(' alone is an invalid regex; as a literal it matches the decorator line.
    out = ws.search_files("route('")
    assert "routes.py" in out
    assert "no matches" not in out


def test_search_files_reports_no_matches_plainly(tmp_path):
    ws = Workspace(tmp_path / "ws")
    _seed_search_tree(ws.root)
    assert "no matches" in ws.search_files("zebra_quantum_flux")


def test_search_files_respects_the_jail(tmp_path):
    ws = Workspace(tmp_path / "ws")
    with pytest.raises(WorkspaceError):
        ws.search_files("anything", path="..")


def test_search_files_caps_results_with_truncation_note(tmp_path):
    from foreman.workspace import SEARCH_MAX_MATCHES

    ws = Workspace(tmp_path / "ws")
    lines = "\n".join(f"needle_{i}" for i in range(SEARCH_MAX_MATCHES + 50))
    (ws.root / "big.txt").write_text(lines, encoding="utf-8")
    out = ws.search_files("needle")
    assert out.count("\n") <= SEARCH_MAX_MATCHES + 1  # matches + truncation note
    assert "truncated" in out
