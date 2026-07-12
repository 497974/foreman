"""Pluggable executor backends — who writes the code is swappable.

Foreman's executor boundary is a clean protocol: ``execute(task, deps) ->
Handoff``. Everything downstream — the verifier's real test gates, the ledger,
the dispute/arbiter layer — grades the *workspace*, not the author. So the
thing that actually writes the code can be swapped without touching any of
that. That is the whole point of the design: a rejection is a rejection whether
a hand-written loop or a state-of-the-art coding agent produced the diff.

Two backends ship:

* the default ``Executor`` (foreman/executor.py) — a hand-written OpenAI
  tool-calling loop, pure Python, only the ``openai`` dependency. This stays the
  default so the core, the 100+ tests, and the Alibaba Cloud FC deployment need
  nothing but Python.
* ``QwenCodeBackend`` — delegates a task to Alibaba's official **qwen-code** CLI
  (github.com/QwenLM/qwen-code, Apache-2.0), a far more capable coding agent,
  pointed at the same DashScope Qwen models via its OpenAI-compatible provider.
  It is opt-in (``FOREMAN_EXECUTOR_BACKEND=qwen-code``) so the Node.js runtime it
  needs never burdens the default path.

The verifier does not know or care which backend ran — which is exactly what
makes "we swapped in Alibaba's own coding agent and nothing else changed" true.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Protocol

from .executor import Executor
from .models import AttemptOutcome, Handoff, Task
from .workspace import Workspace


def _qwen_prompt(task: Task, dependency_handoffs: list[Handoff]) -> str:
    """Build an imperative instruction for the qwen-code CLI.

    A coding agent reliably *acts* on one flowing imperative request — the way
    a human would phrase it — but a weaker model (qwen-flash / qwen3-coder-flash)
    stalls or narrates when handed a structured spec: Foreman's formal task card
    (title/criteria/verification *headings*), bullet lists, or meta-instructions
    like "use your tools to create the files" reliably make it deliberate and
    ask for permission instead of writing. So we collapse the task into a single
    imperative paragraph — description first, criteria and the target test woven
    in as sentences, no headings or bullets — which is what makes it act.
    """
    parts = [(task.description.strip() or task.title).rstrip(".") + "."]
    if task.acceptance_criteria:
        parts.append("It must satisfy: " + "; ".join(task.acceptance_criteria) + ".")
    if task.test_strategy.strip():
        # The gate command is run VERBATIM by the verifier. Spell out the one
        # trap observed live with hermes-agent: the gate selected
        # `file.py::test_name` (a module-level pytest function), the agent
        # wrote a unittest-style class instead, and pytest's selector matched
        # nothing — three functionally-correct attempts rejected on exit 4.
        parts.append(
            f"Also create the test file(s) such that this exact command exits 0: "
            f"`{task.test_strategy}`. If that command selects a specific test id "
            "(file::test_name), define a TOP-LEVEL pytest function with exactly "
            "that name — not a unittest class method. Run the exact command "
            "yourself to confirm before finishing."
        )
    if dependency_handoffs:
        deps: list[str] = []
        for h in dependency_handoffs:
            deps += list(h.files_touched)
        if deps:
            parts.append("Relevant existing files: " + ", ".join(deps) + ".")
    if task.last_error:
        parts.append(f"A previous attempt was rejected — fix exactly this: {task.last_error}")
    return " ".join(parts)


class ExecutorBackend(Protocol):
    """The single method every executor — native or external — must provide.

    The existing ``Executor`` class already satisfies this without any change.
    """

    def execute(self, task: Task, dependency_handoffs: list[Handoff]) -> Handoff: ...


# ---- workspace change detection ------------------------------------------

# We do NOT trust an external agent to self-report which files it touched
# (qwen-code's JSON output carries no guaranteed file-diff field). Instead we
# snapshot the workspace before and after and diff — the same "grade the
# workspace, not the author's word" principle the verifier runs on.

_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv", ".qwen",
              ".pytest_cache"}
_SKIP_EXT = (".pyc", ".db", ".db-wal", ".db-shm")


def _snapshot(root: Path) -> dict[str, tuple[int, float]]:
    snap: dict[str, tuple[int, float]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(_SKIP_EXT):
                continue
            p = Path(dirpath) / fn
            try:
                st = p.stat()
                snap[str(p.relative_to(root))] = (st.st_size, st.st_mtime)
            except OSError:
                pass
    return snap


def _changed_files(before: dict, after: dict) -> list[str]:
    """Files that were created, modified, OR deleted during the run.

    Deletions matter: an agent whose only change is removing a file would
    otherwise report zero changed files, which (a) understates files_touched
    and (b) can trip the exit-0-but-no-changes false-success guard into
    classifying a correct delete-only task as CRASHED. Comparing both
    directions catches created/modified (in after, differing) and deleted
    (in before, absent from after)."""
    created_or_modified = {f for f, meta in after.items() if before.get(f) != meta}
    deleted = {f for f in before if f not in after}
    return sorted(created_or_modified | deleted)


def _kill_tree(proc) -> None:
    """Kill a process AND its children. An agent CLI spawns its own
    subprocesses (pytest, a dev server) as tool calls; a plain single-process
    kill on timeout orphans those, and a hung child (infinite loop, a server
    that never returns) keeps running and can dirty the next task's snapshot.
    On Windows taskkill /T walks the whole tree; POSIX best-effort kills the
    direct child."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=15,
            )
        else:
            proc.kill()
    except Exception:  # noqa: BLE001 — cleanup is best-effort, never fatal
        pass


def _run_cli_capture(cmd, cwd, env, timeout_s):
    """Run an agent CLI, capturing UTF-8 output; return
    ``(exit_code, timed_out, stdout, stderr)``.

    Robust where subprocess.run was not:
    * A LAUNCH failure (OSError — a missing binary, or on Windows the ~8191-char
      command-line limit when a long retry prompt is passed via ``-p``) is
      reported as exit -1 / not-timed-out (which _build_handoff maps to CRASHED)
      instead of propagating and aborting the entire orchestrator loop.
    * On TIMEOUT the whole process TREE is killed (see _kill_tree), not just the
      top-level CLI.
    """
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace",
        )
    except OSError as exc:
        return -1, False, "", f"failed to launch agent CLI: {exc}"
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
        return proc.returncode, False, stdout or "", stderr or ""
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except Exception:  # noqa: BLE001
            stdout, stderr = "", ""
        return -1, True, stdout or "", stderr or ""


def find_qwen_bin() -> Optional[str]:
    """Locate the qwen-code CLI (``FOREMAN_QWEN_CODE_BIN`` overrides the PATH)."""
    return os.environ.get("FOREMAN_QWEN_CODE_BIN") or shutil.which("qwen")


def _build_handoff(
    task: Task, exit_code: int, timed_out: bool, stdout: str, stderr: str,
    changed: list[str],
) -> Handoff:
    """Translate a qwen-code run into Foreman's Handoff schema.

    Exit-code map follows qwen-code's documented codes: 0 = ok, 53 = turn limit,
    55 = budget (wall-time / tool-call) exceeded, others = failure.
    """
    if timed_out or exit_code in (53, 55):
        outcome = AttemptOutcome.TIMEOUT
    elif exit_code == 0:
        outcome = AttemptOutcome.SUCCESS
    else:
        outcome = AttemptOutcome.CRASHED

    summary_lines = [ln for ln in (stdout or "").strip().splitlines() if ln.strip()]
    summary = summary_lines[-1][:500] if summary_lines else ""

    gotchas: list[str] = []
    if outcome is not AttemptOutcome.SUCCESS:
        tail = ((stderr or "").strip() or (stdout or "").strip())[-800:]
        gotchas.append(f"qwen-code exit={exit_code} timed_out={timed_out}: {tail}")

    return Handoff(
        task_id=task.task_id,
        attempt_no=task.attempt_count,
        outcome=outcome.value,
        completed_work=[summary] if summary else [],
        files_touched=changed,
        gotchas=gotchas,
        handoff_reason=f"qwen-code backend (exit={exit_code})",
    )


class QwenCodeBackend:
    """Runs one task by shelling out to Alibaba's qwen-code CLI in headless mode.

    The CLI writes/edits files directly in ``workspace.root`` under the same
    DashScope Qwen model Foreman is configured with; we detect what it changed
    by snapshotting the directory, and map its exit code to a Handoff outcome.
    """

    def __init__(
        self,
        settings,
        workspace: Workspace,
        model: Optional[str] = None,
        timeout_s: float = 1000.0,
        bin_path: Optional[str] = None,
    ):
        self.settings = settings
        self.workspace = workspace
        self.model = model or getattr(settings, "executor_model", "qwen-plus")
        self.timeout_s = timeout_s
        self.bin = bin_path or find_qwen_bin()
        if not self.bin:
            raise RuntimeError(
                "qwen-code CLI not found. Install it (`npm install -g "
                "@qwen-code/qwen-code`) or set FOREMAN_QWEN_CODE_BIN to its path."
            )

    def execute(self, task: Task, dependency_handoffs: list[Handoff]) -> Handoff:
        prompt = _qwen_prompt(task, dependency_handoffs)
        root = self.workspace.root
        env = {
            **os.environ,
            # qwen-code reads its provider from the OpenAI-compatible triple.
            "OPENAI_API_KEY": self.settings.api_key,
            "OPENAI_BASE_URL": self.settings.base_url,
            "OPENAI_MODEL": self.model,
            "QWEN_CODE_SUPPRESS_YOLO_WARNING": "1",
        }
        # `-y` / `--yolo` is what auto-executes tool calls in headless mode. The
        # CLI itself is explicit: without it, write_file "requires user approval
        # but cannot execute in non-interactive mode" and the agent just narrates
        # what it *would* do (learned the hard way — --approval-mode was not
        # honored reliably). The process cwd IS the workspace, so no --cwd flag
        # (passing one on top of cwd was observed to make the CLI write nothing).
        cmd = [self.bin, "-p", prompt, "--yolo"]

        before = _snapshot(root)
        # UTF-8 capture + launch-failure and process-tree-kill handling live in
        # _run_cli_capture (qwen-code is a Node CLI emitting box-drawing/emoji;
        # cp1252 decode would crash, and a long -p prompt can exceed Windows'
        # command-line limit).
        exit_code, timed_out, stdout, stderr = _run_cli_capture(
            cmd, root, env, self.timeout_s
        )
        after = _snapshot(root)

        return _build_handoff(
            task, exit_code, timed_out, stdout or "", stderr or "",
            _changed_files(before, after),
        )


def find_hermes_bin() -> Optional[str]:
    """Locate the hermes-agent CLI (``FOREMAN_HERMES_BIN`` overrides the PATH)."""
    return os.environ.get("FOREMAN_HERMES_BIN") or shutil.which("hermes")


class HermesBackend:
    """Runs one task by shelling out to NousResearch's hermes-agent CLI
    (github.com/NousResearch/hermes-agent, MIT) in headless mode.

    Same delegation shape as QwenCodeBackend — Hermes is a full autonomous
    agent (persistent memory, self-written skills, web access) and Foreman is
    the management layer that feeds it ONE verified task at a time. `hermes -z`
    is its pure single-prompt mode ("prompt in, final text out"); `--yolo`
    auto-approves tool use, without which a headless agent narrates instead of
    acting (the exact failure mode QwenCodeBackend documents above).

    Model routing: Hermes reads its provider from ``~/.hermes/config.yaml``
    (one-time setup — see scripts/setup_hermes.py, which points it at the same
    DashScope OpenAI-compatible endpoint Foreman uses) and honors the
    ``HERMES_INFERENCE_MODEL`` env var per invocation, so Foreman's per-role
    model choice still applies. The process cwd IS the workspace — Hermes
    operates on the shell cwd, mirroring the no-flag lesson from qwen-code.
    """

    def __init__(
        self,
        settings,
        workspace: Workspace,
        model: Optional[str] = None,
        timeout_s: float = 1000.0,
        bin_path: Optional[str] = None,
    ):
        self.settings = settings
        self.workspace = workspace
        self.model = model or getattr(settings, "executor_model", "qwen3-coder-flash")
        self.timeout_s = timeout_s
        self.bin = bin_path or find_hermes_bin()
        if not self.bin:
            raise RuntimeError(
                "hermes-agent CLI not found. Install it (Windows PowerShell: "
                "`iex (irm https://hermes-agent.nousresearch.com/install.ps1)`) "
                "or set FOREMAN_HERMES_BIN to its path, then run "
                "`python scripts/setup_hermes.py` once to point it at DashScope."
            )

    def execute(self, task: Task, dependency_handoffs: list[Handoff]) -> Handoff:
        prompt = _qwen_prompt(task, dependency_handoffs)  # same flowing-imperative shape
        root = self.workspace.root
        env = {
            **os.environ,
            "OPENAI_API_KEY": self.settings.api_key,
            "HERMES_INFERENCE_MODEL": self.model,
        }
        # -z is already the pure single-prompt mode ("prompt in, final text
        # out"); --quiet is NOT a valid top-level flag (exit 2 on a real
        # 0.18.0 install — it belongs to subcommands only), so it stays off.
        cmd = [self.bin, "-z", prompt, "--yolo"]

        before = _snapshot(root)
        exit_code, timed_out, stdout, stderr = _run_cli_capture(
            cmd, root, env, self.timeout_s
        )
        after = _snapshot(root)

        changed = _changed_files(before, after)
        handoff = _build_handoff(
            task, exit_code, timed_out, stdout or "", stderr or "", changed,
        )
        handoff.handoff_reason = f"hermes backend (exit={exit_code})"

        # Caught live: when the model endpoint 403s (quota exhausted), hermes
        # prints the HTTP error as its "final answer" and exits 0 — which
        # _build_handoff reads as SUCCESS. That false success then costs a
        # real verifier call and a retry-budget slot per attempt, three times,
        # for work that never happened. An exit-0 run that changed NOTHING
        # and whose output is an HTTP/quota error is a crash, not a result.
        if (
            handoff.outcome == AttemptOutcome.SUCCESS.value
            and not changed
            and re.search(
                r"HTTP [45]\d\d|quota has been exhausted|invalid api-key",
                stdout or "", re.IGNORECASE,
            )
        ):
            handoff.outcome = AttemptOutcome.CRASHED.value
            handoff.gotchas = [f"hermes returned an API error instead of work: {(stdout or '').strip()[:300]}"]
            handoff.completed_work = []
        return handoff


def make_executor(settings, workspace: Workspace, client, computer_mode: bool = False) -> ExecutorBackend:
    """Build the executor named by ``settings.executor_backend`` (default native).

    'native'   -> the hand-written tool-loop Executor (pure Python, default).
    'qwen-code'-> delegate to the qwen-code CLI (opt-in; needs Node.js + the CLI).
    'hermes'   -> delegate to NousResearch's hermes-agent CLI (opt-in; see
                  scripts/setup_hermes.py for the one-time DashScope wiring).

    ``computer_mode`` is passed through to the native Executor so its system
    prompt tells it it's operating a real machine (see Executor). The external
    agent backends (qwen-code / hermes) are already full computer agents, so
    they need no such hint.
    """
    name = (getattr(settings, "executor_backend", "native") or "native").strip().lower()
    if name in ("qwen-code", "qwen_code", "qwencode"):
        return QwenCodeBackend(settings, workspace)
    if name in ("hermes", "hermes-agent", "hermes_agent"):
        return HermesBackend(settings, workspace)
    return Executor(
        client, settings.executor_model, workspace, computer_mode=computer_mode
    )
