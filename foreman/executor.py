"""The Executor: turns one Task into one Handoff via an OpenAI tool-calling loop.

This is the only place in Foreman that talks to Qwen (via an OpenAI-compatible
client from ``config.make_client``) to actually produce work, as opposed to
just judging or planning it. Everything else in the system exists to give this
loop a *clean* context: one task card, its dependency handoffs, and — on a
retry — the verifier's exact complaint. No planner chatter, no other tasks'
history. That isolation is what lets a hundred-task run stay coherent instead
of each attempt inheriting the last one's confusion.

The loop itself is intentionally dumb: read a tool call, run it against the
Workspace, feed the string result back, repeat until the model calls `done`.
All the actual judgment (was the work good enough?) is deliberately punted to
foreman/verifier.py — the executor's job is to attempt, not to grade itself.
"""

from __future__ import annotations

import json
import re

from .llm import create_with_fallback
from .models import AttemptOutcome, Handoff, Task
from .telemetry import METER
from .websearch import web_search
from .workspace import Workspace, WorkspaceError

# Tools exposed to the model. `done` ends the loop; the other four map 1:1
# onto Workspace methods. Keeping the schema here (not in workspace.py) keeps
# workspace.py importable without pulling in any notion of "what an LLM sees".
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file from the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write (or overwrite) a text file in the workspace, creating parent directories as needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List the contents of a directory in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Defaults to the workspace root."}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Search file CONTENTS across the workspace for a pattern "
                "(regex; an invalid regex is retried as literal text; "
                "case-insensitive). Returns matching lines as "
                "'path:line_number: text'. In an existing project, use this "
                "FIRST to locate where a symbol/route/config lives before "
                "reading whole files — much cheaper than list_dir + read_file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {
                        "type": "string",
                        "description": "Directory to search under; defaults to the workspace root.",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web (DuckDuckGo, no key needed). Returns up to 5 "
                "results as title/url/snippet. Use when the task needs facts "
                "you don't have — current data, library versions, API shapes. "
                "Results are for orientation; verify anything you rely on."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command with cwd at the workspace root. Use this to run tests / linters / the verification command.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Call this exactly once, when — and only when — the task is complete and verified. Ends the attempt.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "completed_work": {"type": "array", "items": {"type": "string"}},
                    "files_touched": {"type": "array", "items": {"type": "string"}},
                    "gotchas": {"type": "array", "items": {"type": "string"}},
                    "self_check": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["summary", "completed_work", "files_touched", "gotchas", "self_check"],
            },
        },
    },
]

_TOOL_NAMES = {t["function"]["name"] for t in TOOLS}

SYSTEM_PROMPT = """You are an autonomous software executor working inside a sandboxed workspace.

Work ONLY inside the workspace via the tools you have been given — read_file,
write_file, list_dir, search_files, web_search, run_command. Never claim to
have done something you did not actually do through a tool call.

web_search reaches the live internet: use it when the task needs facts you
do not reliably know (current data, versions, API shapes). Do not paste
search snippets into code as truth — verify anything load-bearing.

When working in an existing codebase, ORIENT before you edit: use
search_files to find where the relevant symbol, route, or config actually
lives, then read exactly those files. Do not guess file locations and do not
read directories wholesale.

DO NOT implement placeholders or simplified implementations. A stub, a TODO,
a "left as an exercise", or a happy-path-only implementation is not
acceptable — full implementation is required, matching the task's acceptance
criteria exactly.

Run the verification command for this task (its test_strategy) via
run_command before calling done. You cannot claim completion without it
passing — a green run is the minimum bar, not optional polish.

If a previous attempt was rejected, the verifier feedback quoted in this
task's history is authoritative. Fix exactly what it says is wrong; do not
re-litigate the feedback or redo work it says already passed.

When the task is genuinely complete and verified, call the `done` tool with a
summary, the list of completed work items, the files you touched, any
gotchas future work should know about, and a self-check against each
acceptance criterion."""

NUDGE = "You responded with plain text and no tool call. Use one of your tools, or call done() if the task is finished."


def _task_card(task: Task) -> str:
    lines = [
        f"# Task: {task.title}",
        f"task_id: {task.task_id}",
        f"attempt: {task.attempt_count}",
        "",
        "## Description",
        task.description,
        "",
        "## Acceptance criteria",
    ]
    lines += [f"- {c}" for c in task.acceptance_criteria] or ["(none specified)"]
    lines += ["", "## Verification command (test_strategy)", task.test_strategy or "(none specified)"]
    if task.last_error:
        lines += ["", "## Verifier feedback from the previous attempt (authoritative)", task.last_error]
    return "\n".join(lines)


def _handoffs_section(dependency_handoffs: list[Handoff]) -> str:
    if not dependency_handoffs:
        return ""
    parts = ["", "## Handoffs from dependency tasks"]
    for h in dependency_handoffs:
        parts.append(f"### {h.task_id} (attempt {h.attempt_no}, outcome={h.outcome})")
        if h.completed_work:
            parts.append("Completed work: " + "; ".join(h.completed_work))
        if h.files_touched:
            parts.append("Files touched: " + ", ".join(h.files_touched))
        if h.interface_contract:
            parts.append("Interface contract: " + "; ".join(h.interface_contract))
        if h.gotchas:
            parts.append("Gotchas: " + "; ".join(h.gotchas))
    return "\n".join(parts)


def _user_message(task: Task, dependency_handoffs: list[Handoff]) -> str:
    return _task_card(task) + "\n" + _handoffs_section(dependency_handoffs)


def _as_str_list(value) -> list[str]:
    """Coerce a `done`-tool array field to a clean ``list[str]``.

    The `done` tool declares completed_work/files_touched/gotchas/self_check
    as JSON arrays, but weaker models do not always honor that. Caught live
    with qwen3-coder-flash: it filled ``files_touched`` with a single
    markdown-bulleted STRING — ``"\\n- app.py\\n- test_req02.py\\n"`` — instead
    of ``["app.py", "test_req02.py"]``. Taken literally that is catastrophic
    downstream: the verifier reads ``files_touched`` to fetch the evidence
    files it scores against, iterating a string yields *characters*, so it
    tries to read files named ``"-"``, ``" "``, ``"a"`` … finds none, and
    rejects a task whose objective gate (pytest) already passed — a false
    rejection that burns the retry budget and blocks the task.

    Normalize defensively: a real list is stripped of blank entries; a string
    is split on newlines with leading markdown bullets / ``1.`` numbering
    removed; anything else becomes ``[]`` (matching the old ``.get(..., [])``
    default).
    """
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        out: list[str] = []
        for line in value.splitlines():
            s = re.sub(r"^\s*[-*+•]\s*", "", line)  # markdown bullet
            s = re.sub(r"^\s*\d+[.)]\s*", "", s)          # "1. " / "1) "
            s = s.strip()
            if s:
                out.append(s)
        return out
    return []


def _tool_result_str(
    name: str, workspace: Workspace, args: dict, timeout: float = 120.0
) -> str:
    """Execute one tool call against the Workspace, returning its string result.

    Jail violations (WorkspaceError) are caught here and handed back to the
    model as an ordinary tool-result string rather than propagating — the
    executor loop must never crash because the model hallucinated a path; it
    should just learn, from the error text, to try a legal path instead.
    """
    if name == "read_file":
        return workspace.read_file(args["path"])
    if name == "write_file":
        return workspace.write_file(args["path"], args.get("content", ""))
    if name == "list_dir":
        entries = workspace.list_dir(args.get("path", "."))
        return "\n".join(entries) if entries else "(empty directory)"
    if name == "search_files":
        return workspace.search_files(args["pattern"], args.get("path", "."))
    if name == "web_search":
        return web_search(args["query"])
    if name == "run_command":
        result = workspace.run(args["command"], timeout=timeout)
        stdout = result.stdout
        stderr = result.stderr
        note = " [TIMED OUT]" if result.timed_out else ""
        return f"exit={result.exit_code}{note}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    raise ValueError(f"unknown tool: {name}")  # pragma: no cover — guarded by _TOOL_NAMES upstream


class Executor:
    def __init__(
        self,
        client,
        model: str,
        workspace: Workspace,
        max_iters: int = 15,
        command_timeout: float = 120.0,
        fallback_models: list[str] | None = None,
        existing_project: bool = False,
    ):
        self.client = client
        self.model = model
        self.workspace = workspace
        self.max_iters = max_iters
        self.command_timeout = command_timeout
        # Contract §12: models to substitute in, in order, on insufficient_quota
        # / 403 / persistent 429 for self.model. Wired from Settings by the
        # orchestrator; defaults to [] so existing direct construction (tests,
        # mocks) sees no behavior change.
        self.fallback_models = fallback_models or []
        # Existing-project mode (contract Addendum 4 §14): append one line to
        # the system prompt rather than replacing it, so the base rules (work
        # only via tools, no placeholders, run verification, feedback is
        # authoritative) stay identical. Default False = zero behavior change
        # for greenfield mode / existing tests.
        self.existing_project = existing_project
        self.system_prompt = SYSTEM_PROMPT
        if existing_project:
            self.system_prompt = (
                SYSTEM_PROMPT
                + "\n\nThis is an EXISTING codebase, not a fresh scaffold — read "
                "relevant files before editing them, and follow existing "
                "conventions rather than rewriting things from scratch unless "
                "the task says to."
            )

    def execute(self, task: Task, dependency_handoffs: list[Handoff]) -> Handoff:
        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": _user_message(task, dependency_handoffs)},
        ]

        for i in range(self.max_iters):
            # Wind-down: when only a couple of steps remain, remind the model
            # once to converge — finish the current file, run the check, call
            # done() — rather than starting new work it can't finish. Cheaper
            # than burning the last turns exploring and then timing out.
            if i == self.max_iters - 2 and self.max_iters >= 3:
                messages.append({
                    "role": "system",
                    "content": "You are almost out of steps. Finish the current "
                    "file, run the verification command, and call done() now.",
                })

            resp = create_with_fallback(
                self.client,
                self.model,
                self.fallback_models,
                messages=messages,
                tools=TOOLS,
            )
            usage = getattr(resp, "usage", None)
            if usage is not None:
                METER.record(
                    self.model,
                    getattr(usage, "prompt_tokens", 0),
                    getattr(usage, "completion_tokens", 0),
                )
            choice = resp.choices[0]
            message = choice.message
            tool_calls = getattr(message, "tool_calls", None) or []

            if not tool_calls:
                # Plain text with no tool call: nudge once, then this attempt
                # still counts toward max_iters — a model that free-talks
                # forever must not get unlimited free turns.
                messages.append({"role": "assistant", "content": message.content or ""})
                messages.append({"role": "user", "content": NUDGE})
                continue

            # Echo the assistant turn (with its tool_calls) back into history
            # before appending results, exactly as the OpenAI protocol requires.
            # Normalize to plain dicts rather than echoing SDK objects, and
            # sanitize arguments to a valid JSON string: some Qwen variants
            # emit "" for zero-arg tool calls (list_dir), and weaker models
            # (observed live with qwen-turbo) occasionally emit a NON-empty but
            # syntactically invalid JSON string. Either way, DashScope rejects
            # the echoed history with 400 "function.arguments must be JSON" —
            # and because this goes into `messages`, a malformed string here
            # poisons every subsequent turn of the whole attempt, not just this
            # one. So validate, don't just check for emptiness.
            def _sanitized_arguments(raw: str | None) -> str:
                raw = raw or ""
                if raw.strip():
                    try:
                        json.loads(raw)
                        return raw  # already valid JSON — echo verbatim
                    except json.JSONDecodeError:
                        pass  # fall through: invalid JSON, substitute a safe default
                return "{}"

            messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": _sanitized_arguments(call.function.arguments),
                            },
                        }
                        for call in tool_calls
                    ],
                }
            )

            done_handoff = None
            for call in tool_calls:
                name = call.function.name
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError as e:
                    result_str = f"error: could not parse arguments as JSON: {e}"
                    messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": result_str}
                    )
                    continue

                if name == "done":
                    done_handoff = Handoff(
                        task_id=task.task_id,
                        attempt_no=task.attempt_count,
                        outcome=AttemptOutcome.SUCCESS.value,
                        completed_work=_as_str_list(args.get("completed_work")),
                        files_touched=_as_str_list(args.get("files_touched")),
                        gotchas=_as_str_list(args.get("gotchas")),
                        self_check=_as_str_list(args.get("self_check")),
                        handoff_reason=args.get("summary", "completed"),
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": "acknowledged: attempt complete"}
                    )
                    continue

                if name not in _TOOL_NAMES:
                    result_str = f"error: unknown tool {name!r}"
                else:
                    try:
                        result_str = _tool_result_str(
                            name, self.workspace, args, timeout=self.command_timeout
                        )
                    except WorkspaceError as e:
                        # Jail violation inside a tool call: report it as a
                        # tool result, don't let the exception unwind the loop.
                        result_str = f"error: {e}"

                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": result_str}
                )

            if done_handoff is not None:
                return done_handoff

        # max_iters reached without a done() call.
        return Handoff(
            task_id=task.task_id,
            attempt_no=task.attempt_count,
            outcome=AttemptOutcome.TIMEOUT.value,
            gotchas=[f"executor hit max_iters ({self.max_iters}) without calling done()"],
            handoff_reason="timeout: max_iters exhausted",
        )
