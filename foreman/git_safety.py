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


def create_or_checkout_branch(path, branch: str) -> None:
    """Create `branch` if it does not exist yet, else just check it out.

    Idempotent by design: resume_run calls this again on every resume, and it
    must be a no-op (aside from switching HEAD) when the branch already exists
    from the run's first pass.
    """
    listing = _run_git(path, ["branch", "--list", branch])
    if listing.stdout.strip():
        _run_git(path, ["checkout", branch])
    else:
        _run_git(path, ["checkout", "-b", branch])


def commit_all(path, message: str) -> bool:
    """Stage everything and commit if (and only if) something is staged.

    Returns True if a commit was made, False if there was nothing to commit —
    never makes an --allow-empty commit (a task that touched nothing real,
    e.g. a no-op verification-only retry, must not pollute the branch history
    with empty commits).
    """
    _run_git(path, ["add", "-A"])
    diff = _run_git(path, ["diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        # Nothing staged — diff --cached --quiet exits 0 when there is no diff.
        return False
    commit = _run_git(path, ["commit", "-m", message])
    return commit.returncode == 0


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

    if not force_dirty and not is_clean(path):
        raise GitSafetyError(
            "the repo has uncommitted changes — commit or stash your changes "
            "first, or pass force_dirty=True to proceed anyway (not recommended)."
        )
