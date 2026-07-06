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
    acquire_lock,
    commit_all,
    create_or_checkout_branch,
    ensure_ready,
    in_progress_operation,
    is_clean,
    is_git_repo,
    release_lock,
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


def test_create_or_checkout_branch_resume_is_idempotent(tmp_path):
    repo = _init_repo_with_commit(tmp_path / "repo")
    create_or_checkout_branch(repo, "foreman/run_abc123")
    # Resume path: the branch exists from the first pass, allow_existing=True
    # must re-check-out the same branch idempotently without error (this is
    # what makes --resume safe to call repeatedly).
    create_or_checkout_branch(repo, "foreman/run_abc123", allow_existing=True)
    current = _git(repo, "branch", "--show-current").stdout.strip()
    assert current == "foreman/run_abc123"
    # branch --list shows exactly one match, not a duplicate/error state
    listing = _git(repo, "branch", "--list", "foreman/run_abc123").stdout
    assert listing.count("foreman/run_abc123") == 1


def test_create_or_checkout_branch_rejects_preexisting_branch_on_first_pass(tmp_path):
    """A branch already named foreman/<run_id> was NOT created by this run
    (run_ids are freshly minted) — silently reusing it would commit on top of
    history Foreman does not own while still claiming 'isolated branch'."""
    repo = _init_repo_with_commit(tmp_path / "repo")
    _git(repo, "branch", "foreman/run_abc123")  # someone else's leftover
    with pytest.raises(GitSafetyError, match="already exists"):
        create_or_checkout_branch(repo, "foreman/run_abc123")
    # and HEAD did not move off the user's branch
    assert _current_branch(repo) in ("master", "main")


def test_create_or_checkout_branch_recreates_deleted_branch_on_resume(tmp_path):
    """Resume with allow_existing=True where the user deleted the branch in
    between: recreate it from current HEAD rather than failing the resume."""
    repo = _init_repo_with_commit(tmp_path / "repo")
    create_or_checkout_branch(repo, "foreman/run_gone", allow_existing=True)
    assert _current_branch(repo) == "foreman/run_gone"


def test_create_or_checkout_branch_works_from_detached_head(tmp_path):
    """Detached HEAD at start is an accepted state — branch creation from the
    detached commit must land on the new branch. Pinned by a test so a future
    refactor can't silently regress what today is only incidental correctness."""
    repo = _init_repo_with_commit(tmp_path / "repo")
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "--detach", sha)
    create_or_checkout_branch(repo, "foreman/run_detached")
    assert _current_branch(repo) == "foreman/run_detached"


def test_create_or_checkout_branch_raises_when_checkout_fails(tmp_path, monkeypatch):
    """The whole safety promise hinges on the checkout succeeding — a silently
    failed checkout would leave HEAD on the user's branch, which would then
    receive Foreman's commits. Simulate a checkout that reports failure and
    confirm we raise GitSafetyError instead of proceeding."""
    import foreman.git_safety as gs

    repo = _init_repo_with_commit(tmp_path / "repo")
    real_run_git = gs._run_git

    def fake_run_git(path, args):
        if args[:1] == ["checkout"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="fatal: checkout blocked")
        return real_run_git(path, args)

    monkeypatch.setattr(gs, "_run_git", fake_run_git)
    with pytest.raises(GitSafetyError, match="isolated Foreman branch"):
        create_or_checkout_branch(repo, "foreman/run_xyz")


def test_create_or_checkout_branch_raises_when_head_not_on_branch(tmp_path, monkeypatch):
    """Defense in depth: even if checkout reports success, we verify HEAD
    actually moved. Simulate checkout 'succeeding' but HEAD staying put."""
    import foreman.git_safety as gs

    repo = _init_repo_with_commit(tmp_path / "repo")
    original = _current_branch(repo)
    real_run_git = gs._run_git

    def fake_run_git(path, args):
        if args[:1] == ["checkout"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")  # lies: says ok, does nothing
        return real_run_git(path, args)

    monkeypatch.setattr(gs, "_run_git", fake_run_git)
    with pytest.raises(GitSafetyError, match=f"HEAD is on '{original}'"):
        create_or_checkout_branch(repo, "foreman/run_xyz")


def test_commit_all_refuses_to_commit_on_wrong_branch(tmp_path):
    """The final backstop: commit_all with expected_branch must refuse to
    commit if HEAD is on a different (e.g. the user's main) branch, even when
    there are real staged changes — this is what guarantees Foreman never
    commits to the user's branch no matter what moved HEAD."""
    repo = _init_repo_with_commit(tmp_path / "repo")
    original = _current_branch(repo)  # user is on master/main
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(GitSafetyError, match="refusing to commit"):
        commit_all(repo, "Foreman: T01 x", expected_branch="foreman/run_notcheckedout")

    # And critically: the user's branch got ZERO new commits from that attempt.
    log = _git(repo, "log", "--oneline", original).stdout.strip().splitlines()
    assert len(log) == 1


def _current_branch(path):
    return _git(path, "branch", "--show-current").stdout.strip()


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


def test_commit_all_succeeds_with_zero_ambient_git_identity(tmp_path, monkeypatch):
    """Caught live, not by any unit test: every OTHER fixture in this file
    pre-configures user.name/user.email on the test repo (see _init_repo),
    so none of them could have caught this. A real machine with no git
    identity configured anywhere (no repo config, no global config, no
    GIT_AUTHOR_* env vars) makes a bare `git commit` fail with exit 128
    ("Please tell me who you are") — and the old code treated that failure
    identically to "nothing to commit", silently discarding the checkpoint
    this function exists to guarantee. commit_all must work regardless of
    the host's git configuration, because Foreman cannot assume any
    particular machine has git identity set up.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    # Deliberately do NOT configure user.name/user.email — this is the one
    # thing every other fixture in this file does that we must NOT do here.
    (repo / "existing.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    # The initial commit itself needs an identity to exist at all; supply it
    # ad hoc via -c (command-scoped, writes nothing to repo/global config) so
    # the repo/global config remains genuinely empty afterward.
    _git(repo, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-m", "seed")
    # `git config` (unlike `git log`/`status`) exits 1 when the key is unset —
    # use check=False directly rather than the file's check=True _git() helper,
    # which would raise CalledProcessError on that expected nonzero exit.
    assert subprocess.run(
        ["git", "-C", str(repo), "config", "--local", "user.name"],
        capture_output=True, check=False,
    ).returncode != 0

    # Also neutralize any process-level identity env vars, so this test is
    # airtight even if the CI/dev machine happens to export them.
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)

    (repo / "feature.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    result = commit_all(repo, "Foreman: T01 add feature")

    assert result is True
    log = _git(repo, "log", "--oneline", "-1").stdout
    assert "Foreman: T01 add feature" in log
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


def test_ensure_ready_rejects_bare_repo_with_actionable_message(tmp_path):
    """A bare repo has no work tree — is_git_repo's --is-inside-work-tree
    check rejects it via the ordinary 'not a git repository' branch. Asserted
    (not assumed) so the message stays sensible rather than a raw git error."""
    bare = tmp_path / "bare.git"
    bare.mkdir()
    subprocess.run(["git", "init", "--bare", str(bare)], capture_output=True, check=True)
    with pytest.raises(GitSafetyError, match="git init"):
        ensure_ready(bare)


# ---- in-progress operations (merge/rebase/cherry-pick) ------------------------


def _make_conflicted_merge(tmp_path):
    """A REAL mid-merge repo: two branches editing the same line, merge fails
    and leaves MERGE_HEAD + conflict markers behind."""
    repo = _init_repo_with_commit(tmp_path / "repo")
    (repo / "shared.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "feat")
    (repo / "shared.txt").write_text("feat version\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "feat edit")
    _git(repo, "checkout", "-")
    (repo / "shared.txt").write_text("main version\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main edit")
    merge = subprocess.run(
        ["git", "-C", str(repo), "merge", "feat"],
        capture_output=True, encoding="utf-8", errors="replace", check=False,
    )
    assert merge.returncode != 0  # the conflict is the point
    return repo


def test_in_progress_operation_detects_real_merge_conflict(tmp_path):
    repo = _make_conflicted_merge(tmp_path)
    assert in_progress_operation(repo) == "merge"


def test_in_progress_operation_none_for_repo_at_rest(tmp_path):
    repo = _init_repo_with_commit(tmp_path / "repo")
    assert in_progress_operation(repo) is None


def test_ensure_ready_rejects_mid_merge_even_with_force_dirty(tmp_path):
    """The force_dirty escape hatch must NOT extend to a repo mid-surgery:
    branching off a conflicted merge would commit raw conflict markers and
    leave .git's merge machinery dangling against a no-longer-checked-out
    branch. Non-overridable, same tier as 'not a repo at all'."""
    repo = _make_conflicted_merge(tmp_path)
    with pytest.raises(GitSafetyError, match="merge in progress"):
        ensure_ready(repo, force_dirty=True)


def test_ensure_ready_rejects_mid_rebase(tmp_path):
    """rebase-merge is a directory marker (not a file like MERGE_HEAD);
    simulated directly rather than via a real rebase for determinism."""
    repo = _init_repo_with_commit(tmp_path / "repo")
    (repo / ".git" / "rebase-merge").mkdir()
    with pytest.raises(GitSafetyError, match="rebase in progress"):
        ensure_ready(repo, force_dirty=True)


# ---- run lock -----------------------------------------------------------------


def test_acquire_lock_blocks_a_second_run(tmp_path):
    repo = _init_repo_with_commit(tmp_path / "repo")
    acquire_lock(repo, "run_first")
    with pytest.raises(GitSafetyError, match="another Foreman run"):
        acquire_lock(repo, "run_second")


def test_acquire_lock_reentrant_for_same_run_id(tmp_path):
    """A resume after a crash finds its OWN stale lock and proceeds."""
    repo = _init_repo_with_commit(tmp_path / "repo")
    acquire_lock(repo, "run_same")
    acquire_lock(repo, "run_same")  # must not raise


def test_release_lock_frees_the_repo_for_the_next_run(tmp_path):
    repo = _init_repo_with_commit(tmp_path / "repo")
    acquire_lock(repo, "run_first")
    release_lock(repo, "run_first")
    acquire_lock(repo, "run_second")  # must not raise


def test_release_lock_never_releases_someone_elses_lock(tmp_path):
    repo = _init_repo_with_commit(tmp_path / "repo")
    acquire_lock(repo, "run_first")
    release_lock(repo, "run_other")  # no-op, and must not raise
    with pytest.raises(GitSafetyError, match="run_first"):
        acquire_lock(repo, "run_second")
