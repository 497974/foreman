"""Git safety rails for existing-project mode.

Foreman normally works inside a fresh sandbox directory it fully owns. Existing-
project mode instead points the Workspace at a REAL git repository on disk —
the user's actual code. Every function here exists to make that safe: never
touch a dirty tree without permission, never write outside the repo root,
always work on an isolated branch, and always leave the user's original branch
(main/master/whatever they were on) with zero new commits.

Subprocess calls use list-form args (no shell=True — there is no untrusted
string interpolation here, unlike Workspace.run) with encoding="utf-8",
errors="replace". This is the SAME fix foreman/backends.py already needed for
qwen-code's non-ASCII stdout on Windows (cp1252 console encoding crashes on
box-drawing/emoji bytes) — git also happily emits non-ASCII (author names,
commit messages, paths) so the same guard applies here rather than rediscovering
that bug a second time.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


class GitSafetyError(RuntimeError):
    """Raised when the target folder is not safe to hand to Foreman as-is.

    Messages are written to be printed directly to a user (CLI/API layer just
    surfaces str(e)) — always say what to do next, not just what is wrong.
    """


def _run_git(path, args: list[str]) -> subprocess.CompletedProcess:
    """Run `git -C <path> <args>`, tolerating non-ASCII output on Windows.

    Never raises on a non-zero exit — callers inspect .returncode/.stdout
    themselves (git's exit codes are meaningful, e.g. `branch --list` with no
    match still exits 0 with empty stdout, `status --porcelain` is 0 either
    way). check=False is deliberate.
    """
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def is_git_repo(path) -> bool:
    """True if `path` is inside a git work tree."""
    result = _run_git(path, ["rev-parse", "--is-inside-work-tree"])
    return result.returncode == 0 and result.stdout.strip() == "true"


def repo_root(path) -> Optional[Path]:
    """The repo's top-level directory, or None if `path` is not a git repo."""
    result = _run_git(path, ["rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    if not top:
        return None
    return Path(top).resolve()


def is_clean(path) -> bool:
    """True if `git status --porcelain` reports no changes at all."""
    result = _run_git(path, ["status", "--porcelain"])
    return result.returncode == 0 and result.stdout.strip() == ""


def current_branch(path) -> Optional[str]:
    """The checked-out branch name, or None if detached / not determinable.

    `git rev-parse --abbrev-ref HEAD` prints the branch name, or the literal
    "HEAD" when in detached-HEAD state — which we normalize to None so callers
    can treat "not on any named branch" uniformly.
    """
    result = _run_git(path, ["rev-parse", "--abbrev-ref", "HEAD"])
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    return None if (not name or name == "HEAD") else name


def in_progress_operation(path) -> Optional[str]:
    """The name of any git operation currently mid-flight ("merge", "rebase",
    "cherry-pick", "revert", "bisect"), or None if the repo is at rest.

    A repo stuck mid-rebase/merge is NOT a safe target even under force_dirty:
    `git checkout -b` from that state succeeds, the conflict markers in the
    tree would get committed as-is by commit_all, and the rebase machinery
    (.git/rebase-merge) is left dangling against a branch that is no longer
    checked out — a corrupted, hard-to-recover state. ensure_ready treats this
    as non-overridable, same tier as "not a repo at all".

    Marker paths are resolved via `git rev-parse --git-path` rather than
    assuming `<repo>/.git/<marker>` — .git can be a FILE (worktrees,
    submodules) pointing at the real git dir elsewhere.
    """
    markers = [
        ("MERGE_HEAD", "merge"),
        ("rebase-merge", "rebase"),
        ("rebase-apply", "rebase"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("REVERT_HEAD", "revert"),
        ("BISECT_LOG", "bisect"),
    ]
    for marker, name in markers:
        result = _run_git(path, ["rev-parse", "--git-path", marker])
        if result.returncode != 0:
            continue
        marker_path = Path(result.stdout.strip())
        if not marker_path.is_absolute():
            marker_path = Path(path) / marker_path
        if marker_path.exists():
            return name
    return None


def create_or_checkout_branch(path, branch: str, allow_existing: bool = False) -> None:
    """Create `branch` and VERIFY we actually landed on it.

    ``allow_existing`` splits the two legitimate call sites:

    - First pass of a run (allow_existing=False, the default): the branch must
      NOT already exist. run_ids are freshly minted, so a pre-existing branch
      with this exact name was made by something else — a leftover from a
      wedged earlier process, or the user's own branch that happens to match.
      Silently checking it out would put Foreman's commits on top of history
      it does not own, while still claiming "isolated branch". Refuse instead.
    - Resume (allow_existing=True): the branch SHOULD already exist from the
      run's first pass — check it out; if the user deleted it in between,
      recreate it from current HEAD (same as a first pass would).

    Critically: a git checkout can FAIL (an in-progress rebase/merge, an
    unmergeable dirty tree under force_dirty, a filesystem error). The entire
    safety promise — "commits only ever land on the isolated foreman branch,
    never the user's main branch" — hinges on this checkout succeeding. So we
    check the command's exit code AND confirm HEAD is now on `branch`
    afterwards; either failing raises GitSafetyError rather than silently
    leaving HEAD on whatever branch the user was on (which would then receive
    Foreman's commits).
    """
    listing = _run_git(path, ["branch", "--list", branch])
    if listing.stdout.strip():
        if not allow_existing:
            raise GitSafetyError(
                f"a branch named '{branch}' already exists in this repo. "
                "Foreman only ever commits to a branch it created itself for "
                "this exact run — a pre-existing branch by this name means a "
                "leftover from an earlier process or a naming collision with "
                "your own work. Delete or rename that branch (git branch -m "
                f"{branch} <something-else>) and re-run."
            )
        result = _run_git(path, ["checkout", branch])
    else:
        result = _run_git(path, ["checkout", "-b", branch])

    if result.returncode != 0:
        raise GitSafetyError(
            f"could not switch to the isolated Foreman branch '{branch}' "
            f"(git said: {result.stderr.strip() or result.stdout.strip()}). "
            "Foreman refuses to proceed rather than risk committing to your "
            "current branch — resolve the repo state (e.g. finish/abort any "
            "in-progress merge or rebase) and try again."
        )

    landed = current_branch(path)
    if landed != branch:
        raise GitSafetyError(
            f"expected to be on Foreman branch '{branch}' after checkout but "
            f"HEAD is on '{landed}'. Foreman refuses to proceed to avoid "
            "committing to the wrong branch."
        )


def commit_all(path, message: str, expected_branch: Optional[str] = None) -> bool:
    """Stage everything and commit if (and only if) something is staged.

    Returns True if a commit was made, False if there was nothing to commit —
    never makes an --allow-empty commit (a task that touched nothing real,
    e.g. a no-op verification-only retry, must not pollute the branch history
    with empty commits).

    ``expected_branch`` is the final safety backstop: if given, this refuses to
    commit unless HEAD is actually on that branch. create_or_checkout_branch
    already verifies the checkout, but this is defense in depth — the one
    guarantee the whole feature sells is "never commit to the user's branch",
    so the commit itself double-checks rather than trusting that nothing moved
    HEAD between checkout and here.

    The commit is made with an explicit, hard-coded author identity
    (-c user.name/user.email on the commit invocation only — this never reads
    or writes the repo's or the user's global git config). This was a real bug
    caught in live testing: a fresh machine with no git user.name/email
    configured anywhere makes a bare `git commit` fail with exit 128 ("Please
    tell me who you are"), and the old code treated that identically to
    "nothing to commit" — silently discarding the per-task checkpoint this
    function exists to guarantee. Pinning the identity here means the
    checkpoint guarantee holds on ANY machine, regardless of host git config.

    Raises GitSafetyError (with git's own stderr) if staging is non-empty but
    the commit itself still fails for some other reason — a real failure at
    this point must be loud, never swallowed as if it were a no-op.
    """
    if expected_branch is not None:
        on = current_branch(path)
        if on != expected_branch:
            raise GitSafetyError(
                f"refusing to commit: expected to be on '{expected_branch}' but "
                f"HEAD is on '{on}'. Foreman never commits to a branch other "
                "than its own isolated run branch."
            )
    _run_git(path, ["add", "-A"])
    diff = _run_git(path, ["diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        # Nothing staged — diff --cached --quiet exits 0 when there is no diff.
        return False
    commit = _run_git(
        path,
        [
            "-c", "user.name=Foreman",
            "-c", "user.email=foreman@localhost",
            "commit", "-m", message,
        ],
    )
    if commit.returncode != 0:
        raise GitSafetyError(
            f"a checkpoint commit failed for {path!s} even though changes were "
            f"staged (git said: {commit.stderr.strip() or commit.stdout.strip()})"
        )
    return True


def ensure_ready(path, force_dirty: bool = False) -> None:
    """Raise GitSafetyError with an actionable message unless `path` is a safe
    target for existing-project mode: a real repo, pointed at its root (not a
    subdirectory — avoids partial-repo commits), and clean (unless the caller
    explicitly opted out via force_dirty).
    """
    if not is_git_repo(path):
        raise GitSafetyError(
            f"{path} is not a git repository — run `git init` in that folder first."
        )

    root = repo_root(path)
    resolved = Path(path).resolve()
    if root != resolved:
        raise GitSafetyError(
            f"point Foreman at the repo ROOT ({root}), not a subdirectory "
            "— this avoids partial-repo commits."
        )

    # Deliberately checked BEFORE (and regardless of) force_dirty: an
    # in-progress merge/rebase is not "dirty", it is a repo mid-surgery.
    # Branching off it and committing would bake unresolved conflict markers
    # into history and leave the rebase machinery dangling.
    operation = in_progress_operation(path)
    if operation is not None:
        raise GitSafetyError(
            f"the repo has a {operation} in progress — finish it (git "
            f"{operation} --continue) or abort it (git {operation} --abort) "
            "first. Foreman refuses to branch off a mid-operation state, even "
            "with force_dirty."
        )

    if not force_dirty and not is_clean(path):
        raise GitSafetyError(
            "the repo has uncommitted changes — commit or stash your changes "
            "first, or pass force_dirty=True to proceed anyway (not recommended)."
        )


def _lock_path(path) -> Optional[Path]:
    """The lock file location inside the repo's real git dir, or None if the
    git dir can't be determined (caller treats that as 'cannot lock')."""
    result = _run_git(path, ["rev-parse", "--absolute-git-dir"])
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()) / "foreman.lock"


def acquire_lock(path, run_id: str) -> None:
    """Claim exclusive Foreman use of this repo for `run_id`.

    Two Foreman runs (two CLI invocations, two web-console tabs) pointed at
    the same repo would race each other's `git checkout`/`git add`/`git
    commit` against one shared working tree — one run's checkout flips HEAD
    mid-way through the other run's commit, and commits land on the wrong
    run's branch. A lock file in the git dir serializes them.

    Re-acquiring for the SAME run_id succeeds (resume after a crash finds its
    own stale lock and proceeds). A different run_id raises with the manual
    escape hatch spelled out: if the other run is truly dead, the user deletes
    the file. Crash-staleness is accepted rather than papered over with
    PID-liveness guesses — the message says exactly what to check and do.
    """
    lock = _lock_path(path)
    if lock is None:
        raise GitSafetyError(f"could not locate the git directory for {path!s} to lock it.")
    if lock.exists():
        holder = lock.read_text(encoding="utf-8", errors="replace").strip()
        if holder == run_id:
            return  # our own lock (a resume, or a crashed earlier pass of us)
        raise GitSafetyError(
            f"another Foreman run ({holder or 'unknown'}) is already using "
            f"this repo. Wait for it to finish — or, if you are sure no other "
            f"Foreman run is active, delete {lock} and retry."
        )
    lock.write_text(run_id, encoding="utf-8")


def release_lock(path, run_id: str) -> None:
    """Release the lock IF it is still ours. Never raises — releasing is
    best-effort cleanup on the way out; a failure to release only means the
    next run sees the stale-lock message with its delete instruction."""
    try:
        lock = _lock_path(path)
        if lock is None or not lock.exists():
            return
        holder = lock.read_text(encoding="utf-8", errors="replace").strip()
        if holder == run_id:
            lock.unlink()
    except OSError:
        pass
