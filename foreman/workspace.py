"""The Workspace: a jailed filesystem + shell for one executor's attempt.

An executor's tool calls are model-generated strings — paths and shell
commands the LLM invented. Nothing here trusts them. Every path resolves
against ``self.root`` and is rejected the instant it would escape (symlink
tricks, ``..`` climbing, or an absolute path elsewhere on disk), and every
shell command runs with ``cwd`` pinned to that same root. This is the one
place in Foreman where model output touches the real filesystem, so it is
also the one place that has to assume the model is adversarial-by-accident
(hallucinated paths, not malice) rather than cooperative.

Output truncation exists for the same reason context windows exist: a
runaway command (``pip install`` chatter, an infinite test loop) must not
blow the executor's context on the next turn. Truncating here, once, keeps
that concern out of the executor loop entirely.

``run()`` also passes every command through ``foreman.safety.is_blocked_command``
before it ever reaches a shell: a conservative DENY list of catastrophic,
mostly absolute-path or host-touching patterns (whole-disk wipes, registry
surgery, shutdown, `git push`, deleting outside the workspace). It is a
blunt guard, not a sandbox — see docs/SECURITY.md for the honest threat
model this module does and does not cover.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .safety import is_blocked_command

MAX_OUTPUT = 10_000  # chars; keeps one runaway command from eating the context window


class WorkspaceError(RuntimeError):
    """Raised for jail violations and missing-file reads — never a bare OSError."""


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + f"\n... [truncated, {len(text) - MAX_OUTPUT} more chars]"


class Workspace:
    """A single task's sandbox: one directory tree, one shell cwd.

    Every executor attempt gets its own Workspace rooted at
    ``runs/<run_id>/workspace`` (or a tmp_path in tests). Nothing outside
    ``self.root`` is readable, writable, or runnable through this object.
    """

    def __init__(self, root: str | Path, allow_all: bool = False):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        # Default-deny: every command goes through is_blocked_command unless
        # a caller opts out explicitly. The executor hands this shell to
        # model-generated strings (see the module docstring) — the safe
        # default for "a string an LLM invented is about to run on this
        # machine" is to check it, not to trust it. allow_all exists for
        # power users who have their own containment (a disposable VM/
        # container) and find the DENY list gets in the way there; it is
        # off by default so a fresh Workspace is never silently unguarded.
        self.allow_all = allow_all

    # ---- the jail ------------------------------------------------------------

    def _resolve(self, rel: str) -> Path:
        """Resolve ``rel`` against the root and enforce the jail.

        ``resolve()`` collapses ``..`` and symlinks before the containment
        check runs, so both "climb out via dots" and "climb out via a
        symlinked file" are caught by the same comparison. Windows paths are
        case-insensitive; ``os.path.commonpath``-style prefix checks would
        get that wrong, so we lean on ``Path.resolve`` + ``relative_to``
        which normalize case correctly on this platform.
        """
        candidate = (self.root / rel).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise WorkspaceError(
                f"path escapes workspace jail: {rel!r} resolved to {candidate}"
            ) from None
        return candidate

    # ---- file ops --------------------------------------------------------

    def read_file(self, path: str) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise WorkspaceError(f"no such file: {path}")
        return target.read_text(encoding="utf-8", errors="replace")

    def write_file(self, path: str, content: str) -> str:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"wrote {path} ({len(content.encode('utf-8'))} bytes)"

    def list_dir(self, path: str = ".") -> list[str]:
        target = self._resolve(path)
        if not target.is_dir():
            raise WorkspaceError(f"no such directory: {path}")
        names = []
        for entry in sorted(target.iterdir(), key=lambda p: p.name):
            names.append(entry.name + "/" if entry.is_dir() else entry.name)
        return names

    # ---- shell -------------------------------------------------------------

    def run(self, command: str, timeout: float = 120.0) -> CommandResult:
        """Run ``command`` in a shell rooted at the workspace.

        ``shell=True`` is required per the contract (Windows dev box, cmd.exe
        semantics) — this is why commands are never trusted with anything
        outside ``self.root``: shell=True hands the model a real shell, so
        the remaining containment is ``cwd`` plus the command-safety check
        below (unless this Workspace was constructed with ``allow_all=True``).

        A blocked command returns a ``CommandResult`` (exit_code=126) rather
        than raising: the executor loop must keep going and the model learns
        from the tool result exactly like any other failed command, instead
        of the attempt crashing.
        """
        if not self.allow_all:
            blocked, reason = is_blocked_command(command)
            if blocked:
                return CommandResult(
                    exit_code=126,
                    stdout="",
                    stderr=f"blocked by Foreman command safety policy: {reason}",
                    duration_s=0.0,
                    timed_out=False,
                )

        start = time.monotonic()
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            duration = time.monotonic() - start
            return CommandResult(
                exit_code=proc.returncode,
                stdout=_truncate(proc.stdout),
                stderr=_truncate(proc.stderr),
                duration_s=duration,
                timed_out=False,
            )
        except subprocess.TimeoutExpired as e:
            duration = time.monotonic() - start
            stdout = e.stdout.decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = e.stderr.decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            return CommandResult(
                exit_code=-1,
                stdout=_truncate(stdout),
                stderr=_truncate(stderr),
                duration_s=duration,
                timed_out=True,
            )
