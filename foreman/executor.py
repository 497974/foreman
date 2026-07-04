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

from .models import AttemptOutcome, Handoff, Task
from .telemetry import METER
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
write_file, list_dir, run_command. Never claim to have done something you did
not actually do through a tool call.

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
    ):
        self.client = client
        self.model = model
        self.workspace = workspace
        self.max_iters = max_iters
        self.command_timeout = command_timeout

    def execute(self, task: Task, dependency_handoffs: list[Handoff]) -> Handoff:
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_message(task, dependency_handoffs)},
        ]

        for _ in range(self.max_iters):
            resp = self.client.chat.completions.create(
                model=self.model,
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
            # patch empty arguments to "{}": some Qwen variants emit "" for
            # zero-arg tool calls (list_dir), and DashScope then rejects the
            # echoed history with 400 "function.arguments must be JSON".
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
                                "arguments": (
                                    call.function.arguments
                                    if (call.function.arguments or "").strip()
                                    else "{}"
                                ),
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
                        completed_work=args.get("completed_work", []),
                        files_touched=args.get("files_touched", []),
                        gotchas=args.get("gotchas", []),
                        self_check=args.get("self_check", []),
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
