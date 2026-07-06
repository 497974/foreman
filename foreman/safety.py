"""Command safety policy: a DENY-list guard checked before a shell command runs.

``Workspace.run`` (see ``foreman/workspace.py``) hands the model a real shell
via ``subprocess.run(..., shell=True)``. The ``cwd`` jail on file ops
(``Workspace._resolve``) stops nothing once a command string names an
absolute path elsewhere on disk, or asks the OS to do something that has
nothing to do with files at all (shut the machine down, edit the registry,
push a git branch upstream). This module is the one blunt instrument left
standing between a hallucinated-or-adversarial command string and the rest
of the host: a conservative DENY list, checked before the shell ever sees
the command.

This is deliberately a DENY list, not an ALLOW list. An allow-list would
constantly reject legitimate dev commands (pytest invocations, pip installs,
npm scripts, arbitrary git subcommands) that there is no way to enumerate in
advance; a DENY list only has to describe the shapes that are almost never
legitimate for an autonomous coding executor to run. That asymmetry is also
its limit — see docs/SECURITY.md for the honest threat model, including what
this module does not and cannot catch (obfuscated commands, a script that
constructs the dangerous command at runtime, a `python -c "..."` payload that
does damage from inside an allowed interpreter).

Patterns target the *theme* (whole-disk destruction, system state changes,
absolute-path writes/deletes outside the workspace, letting sandboxed work
escape upstream) rather than trying to enumerate every dangerous flag
combination. Ordinary relative dev commands — pytest, python, pip install,
npm, git add/commit, mkdir, echo, ls, `rm somefile.py` — must never match.
"""

from __future__ import annotations

import re

# An absolute path fragment: a drive letter (C:\, D:/) or a UNC path. Used
# inside patterns that care about "outside the workspace" more than about
# identifying one specific drive.
_ABS_PATH = r"[a-zA-Z]:[\\/]|\\\\"

_DENY_PATTERNS: list[tuple[str, str]] = [
    (
        "rm -rf on an absolute/root path",
        r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rR][a-zA-Z]*\s+(-[a-zA-Z]+\s+)*(" + _ABS_PATH + r"|/)",
    ),
    (
        "rm -rf on the home directory (~)",
        r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rR][a-zA-Z]*\s+(-[a-zA-Z]+\s+)*~",
    ),
    (
        "del /s /q on an absolute path",
        r"\bdel\b(?:\s+/[a-zA-Z]+)*\s+/[sS]\b(?:\s+/[a-zA-Z]+)*\s+/[qQ]\b.*(" + _ABS_PATH + r")",
    ),
    (
        "rd /s on an absolute path",
        r"\brd\b(?:\s+/[a-zA-Z]+)*\s+/[sS]\b.*(" + _ABS_PATH + r")",
    ),
    (
        "rmdir /s on an absolute path",
        r"\brmdir\b(?:\s+/[a-zA-Z]+)*\s+/[sS]\b.*(" + _ABS_PATH + r")",
    ),
    (
        "rmdir/del of a drive root",
        r"\b(rmdir|rd|del)\b.*[a-zA-Z]:[\\/]\s*(?:$|[\"'])",
    ),
    (
        "PowerShell Remove-Item -Recurse on an absolute path",
        r"\bremove-item\b.*-recurse\b.*(" + _ABS_PATH + r")",
    ),
    (
        "format command",
        r"\bformat\b\s+[a-zA-Z]:",
    ),
    (
        "mkfs (filesystem creation)",
        r"\bmkfs(\.[a-zA-Z0-9]+)?\b",
    ),
    (
        "dd writing to a device",
        r"\bdd\b\s+.*\bif=",
    ),
    (
        "shutdown/reboot",
        r"\b(shutdown|reboot)\b",
    ),
    (
        "bash fork bomb",
        r":\(\)\s*\{\s*:\s*\|\s*:\s*&?\s*\}\s*;?\s*:",
    ),
    (
        "pipe a remote script straight into a shell (curl|bash)",
        r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(bash|sh|zsh)\b",
    ),
    (
        "pipe a remote script straight into a shell (iwr|iex)",
        r"\b(iwr|invoke-webrequest|curl)\b[^|]*\|\s*(iex|invoke-expression)\b",
    ),
    (
        "git push (would let sandboxed work escape upstream)",
        r"\bgit\b\s+push\b",
    ),
    # In existing-project mode the workspace IS the user's real repo, so these
    # git subcommands can move HEAD off Foreman's isolated branch or destroy
    # the user's committed/uncommitted work — block them outright (Foreman's
    # own per-task commits go through git_safety, not the executor's shell).
    (
        "git reset --hard (discards work / rewrites history destructively)",
        r"\bgit\b\s+reset\b[^\n]*--hard\b",
    ),
    (
        "git clean -f/-d (deletes untracked files, including the user's)",
        r"\bgit\b\s+clean\b[^\n]*-[a-zA-Z]*[fdx]",
    ),
    (
        "git checkout/switch of a branch (would escape Foreman's isolated branch)",
        r"\bgit\b\s+(checkout|switch)\b\s+(-[a-zA-Z]+\s+)*[^\s.-]",
    ),
    (
        "git branch -D / -M (force-delete or move branches)",
        r"\bgit\b\s+branch\b[^\n]*\s-[a-zA-Z]*[DM]",
    ),
    (
        "registry edit on HKLM",
        r"\breg\b\s+(add|delete)\b.*\bHKLM\b",
    ),
    (
        "diskpart",
        r"\bdiskpart\b",
    ),
    (
        "netsh (network configuration surgery)",
        r"\bnetsh\b",
    ),
    (
        "taskkill /f (force-kill outside the sandbox)",
        r"\btaskkill\b.*\s/[fF]\b",
    ),
    (
        "cipher /w (secure wipe)",
        r"\bcipher\b\s+/[wW]\b",
    ),
    (
        "redirect into C:\\Windows",
        r">\s*[\"']?[a-zA-Z]:[\\/][wW]indows",
    ),
]

_COMPILED_DENY_PATTERNS = [
    (name, re.compile(pattern, re.IGNORECASE)) for name, pattern in _DENY_PATTERNS
]


def is_blocked_command(cmd: str) -> tuple[bool, str]:
    """Return ``(True, human_reason)`` if ``cmd`` matches a DENY pattern, else ``(False, "")``.

    A conservative, case-insensitive, word-boundary-aware regex scan — not a
    shell parser. It looks for the *theme* of a command (whole-disk wipes,
    system shutdown, registry surgery, deleting/writing outside the
    workspace via an absolute path, letting sandboxed work escape upstream
    via `git push`) rather than trying to model shell semantics exactly.
    Ordinary relative dev commands never match any pattern here.
    """
    for name, pattern in _COMPILED_DENY_PATTERNS:
        if pattern.search(cmd):
            return True, name
    return False, ""
