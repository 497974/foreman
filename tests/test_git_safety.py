"""Tests for foreman/git_safety.py using the REAL git binary via subprocess.

git is present in this dev environment (contract Addendum 4 §14), so these
tests exercise git_safety against real repos built under tmp_path rather than
mocking subprocess — the same spirit as tests/test_workspace.py using a real
filesystem. Every repo gets a local user.name/user.email config (some CI/dev
boxes have no global git identity configured) so `git commit` never fails for
that reason alone.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from foreman.git_safety import (
    GitSafetyError,
    commit_all,
    create_or_checkout_branch,
    ensure_ready,
    is_clean,
    is_git_repo,
    repo_root,
)


def _git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True, encoding="utf-8", errors="replace", check=True,
    )


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.name", "Foreman Test")
    _git(path, "config", "user.email", "test@foreman.local")
    return path


def _init_repo_with_commit(path):
    _init_repo(path)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "initial commit")
    return path


# ---- is_git_repo -------------------------------------------------------------


def test_is_git_repo_true_for_real_repo(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    assert is_git_repo(repo) is True


def test_is_git_repo_false_for_plain_directory(tmp_path):
    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    assert is_git_repo(plain) is False


# ---- repo_root ----------------------------------------------------------------


def test_repo_root_matches_for_repo_root_itself(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    assert repo_root(repo) == repo.resolve()


def test_repo_root_matches_from_a_subdirectory(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    sub = repo / "sub"
    sub.mkdir()
    # repo_root(sub) still reports the TOP of the repo, not sub itself — this
    # mismatch (root != resolved(sub)) is exactly what ensure_ready uses to
    # reject "pointed at a subdirectory".
    assert repo_root(sub) == repo.resolve()
    assert repo_root(sub) != sub.resolve()


def test_repo_root_none_for_non_repo(tmp_path):
    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    assert repo_root(plain) is None


# ---- is_clean -------------------------------------------------------------


def test_is_clean_true_on_fresh_commit(tmp_path):
    repo = _init_repo_with_commit(tmp_path / "repo")
    assert is_clean(repo) is True


def test_is_clean_false_after_writing_a_file(tmp_path):
    repo = _init_repo_with_commit(tmp_path / "repo")
    (repo / "new_file.txt").write_text("dirty\n", encoding="utf-8")
    assert is_clean(repo) is False


# ---- create_or_checkout_branch ------------------------------------------------


def test_create_or_checkout_branch_creates_new_branch(tmp_path):
    repo = _init_repo_with_commit(tmp_path / "repo")
    create_or_checkout_branch(repo, "foreman/run_abc123")
    current = _git(repo, "branch", "--show-current").stdout.strip()
    assert current == "foreman/run_abc123"


def test_create_or_checkout_branch_is_idempotent(tmp_path):
    repo = _init_repo_with_commit(tmp_path / "repo")
    create_or_checkout_branch(repo, "foreman/run_abc123")
    # Call again while already on the branch — must re-check-out the same
    # branch idempotently without error (this is what makes --resume safe to
    # call repeatedly).
    create_or_checkout_branch(repo, "foreman/run_abc123")
    current = _git(repo, "branch", "--show-current").stdout.strip()
    assert current == "foreman/run_abc123"
    # branch --list shows exactly one match, not a duplicate/error state
    listing = _git(repo, "branch", "--list", "foreman/run_abc123").stdout
    assert listing.count("foreman/run_abc123") == 1


# ---- commit_all -----------------------------------------------------------


def test_commit_all_returns_false_when_nothing_to_commit(tmp_path):
    repo = _init_repo_with_commit(tmp_path / "repo")
    result = commit_all(repo, "Foreman: T01 nothing changed")
    assert result is False
    # no new commit was made (allow-empty must never happen)
    log = _git(repo, "log", "--oneline").stdout.strip().splitlines()
    assert len(log) == 1


def test_commit_all_returns_true_after_a_real_change(tmp_path):
    repo = _init_repo_with_commit(tmp_path / "repo")
    (repo / "feature.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    result = commit_all(repo, "Foreman: T01 add feature")
    assert result is True
    log = _git(repo, "log", "--oneline", "-1").stdout
    assert "Foreman: T01 add feature" in log
    # the file is actually committed (not just staged)
    assert is_clean(repo) is True


# ---- ensure_ready -----------------------------------------------------------


def test_ensure_ready_raises_for_non_repo(tmp_path):
    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    with pytest.raises(GitSafetyError, match="git init"):
        ensure_ready(plain)


def test_ensure_ready_raises_for_subdirectory(tmp_path):
    repo = _init_repo_with_commit(tmp_path / "repo")
    sub = repo / "sub"
    sub.mkdir()
    with pytest.raises(GitSafetyError, match="repo ROOT"):
        ensure_ready(sub)


def test_ensure_ready_raises_for_dirty_repo(tmp_path):
    repo = _init_repo_with_commit(tmp_path / "repo")
    (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    with pytest.raises(GitSafetyError, match="force_dirty"):
        ensure_ready(repo)


def test_ensure_ready_passes_for_clean_repo_root(tmp_path):
    repo = _init_repo_with_commit(tmp_path / "repo")
    ensure_ready(repo)  # must not raise


def test_ensure_ready_passes_for_dirty_repo_with_force_dirty(tmp_path):
    repo = _init_repo_with_commit(tmp_path / "repo")
    (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    ensure_ready(repo, force_dirty=True)  # must not raise
