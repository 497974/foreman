"""Executor tests. Run: pytest -q  (no API key, no network, no pip installs).

The executor talks to an OpenAI-compatible client (DashScope, in production).
These tests never import or construct a real ``openai.OpenAI`` client — a
plain fake class scripts ``chat.completions.create`` to return pre-canned
responses shaped like the real SDK's, so the executor loop is exercised end
to end without any network or API key.
"""

import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foreman.executor import Executor, _as_str_list
from foreman.models import AttemptOutcome, Handoff, Task
from foreman.workspace import Workspace


# ---- _as_str_list: robustness to models that mis-fill array fields ----------


def test_as_str_list_passes_a_clean_list_through():
    assert _as_str_list(["app.py", "test_req02.py"]) == ["app.py", "test_req02.py"]


def test_as_str_list_splits_a_markdown_bulleted_string():
    # The exact shape qwen3-coder-flash produced live: a single string in an
    # array field, newline-and-bullet delimited. Left literal, this poisoned
    # the verifier (it read files named "- app.py") and false-rejected a task
    # whose pytest gate had already passed.
    assert _as_str_list("\n- app.py\n- test_req02.py\n") == ["app.py", "test_req02.py"]


def test_as_str_list_strips_numbered_and_star_bullets():
    assert _as_str_list("1. first\n2) second\n* third") == ["first", "second", "third"]


def test_as_str_list_drops_blank_entries_and_handles_none():
    assert _as_str_list(["", "  ", "keep"]) == ["keep"]
    assert _as_str_list(None) == []
    assert _as_str_list(42) == []


def make_task(**kw) -> Task:
    return Task(
        task_id="T1",
        title="write a greeting",
        description="Create greet.py with a function hello() returning 'hi'.",
        acceptance_criteria=["greet.py exists", "hello() returns 'hi'"],
        test_strategy="python -c \"import greet; assert greet.hello() == 'hi'\"",
        **kw,
    )


def _tool_call(call_id: str, name: str, arguments: dict):
    """Build one tool_call in the OpenAI SDK's response shape (SimpleNamespace,
    not dict, so attribute access like ``call.function.name`` works exactly
    as the executor expects from the real SDK)."""
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _message(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls or [])


def _response(message):
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeChatCompletions:
    """Scripted stand-in for ``client.chat.completions``.

    ``responses`` is a list of pre-built SimpleNamespace responses, popped one
    per ``create()`` call in order. This is the "scripted fake client" the
    contract calls for — a plain class, not a mock of the real SDK.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, model, messages, tools):
        self.calls.append({"model": model, "messages": messages, "tools": tools})
        if not self._responses:
            raise AssertionError("FakeChatCompletions ran out of scripted responses")
        return self._responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=FakeChatCompletions(responses))


# ---- happy path: write_file -> run_command -> done -------------------------


def test_happy_path_write_run_done(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    task = make_task()

    responses = [
        _response(
            _message(
                tool_calls=[
                    _tool_call(
                        "call_1",
                        "write_file",
                        {"path": "greet.py", "content": "def hello():\n    return 'hi'\n"},
                    )
                ]
            )
        ),
        _response(
            _message(
                tool_calls=[
                    _tool_call(
                        "call_2",
                        "run_command",
                        {"command": f'{sys.executable} -c "import greet; assert greet.hello() == \'hi\'"'},
                    )
                ]
            )
        ),
        _response(
            _message(
                tool_calls=[
                    _tool_call(
                        "call_3",
                        "done",
                        {
                            "summary": "implemented greet.py",
                            "completed_work": ["wrote greet.py"],
                            "files_touched": ["greet.py"],
                            "gotchas": [],
                            "self_check": ["greet.py exists: yes", "hello() returns 'hi': yes"],
                        },
                    )
                ]
            )
        ),
    ]
    client = FakeClient(responses)
    executor = Executor(client=client, model="qwen3-coder-plus", workspace=workspace, max_iters=15)

    handoff = executor.execute(task, dependency_handoffs=[])

    assert isinstance(handoff, Handoff)
    assert handoff.task_id == "T1"
    assert handoff.attempt_no == task.attempt_count
    assert handoff.outcome == AttemptOutcome.SUCCESS.value
    assert handoff.completed_work == ["wrote greet.py"]
    assert handoff.files_touched == ["greet.py"]
    assert handoff.handoff_reason == "implemented greet.py"
    assert (workspace.root / "greet.py").is_file()

    # exactly 3 model turns were used
    assert len(client.chat.completions.calls) == 3


# ---- jail violation surfaces as a tool-result string, not an exception -----


def test_jail_violation_inside_tool_call_becomes_tool_result(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    task = make_task()

    responses = [
        _response(
            _message(
                tool_calls=[
                    _tool_call("call_1", "read_file", {"path": "../outside.txt"})
                ]
            )
        ),
        _response(
            _message(
                tool_calls=[
                    _tool_call(
                        "call_2",
                        "done",
                        {
                            "summary": "gave up after jail error",
                            "completed_work": [],
                            "files_touched": [],
                            "gotchas": ["tried to read outside the workspace"],
                            "self_check": [],
                        },
                    )
                ]
            )
        ),
    ]
    client = FakeClient(responses)
    executor = Executor(client=client, model="qwen3-coder-plus", workspace=workspace, max_iters=15)

    # Must not raise WorkspaceError (or anything else) out of execute().
    handoff = executor.execute(task, dependency_handoffs=[])

    assert handoff.outcome == AttemptOutcome.SUCCESS.value  # done() was still called
    # The tool result fed back to the model must carry the jail error as text.
    tool_messages = [
        m for m in client.chat.completions.calls[1]["messages"] if m.get("role") == "tool"
    ]
    assert any("error" in m["content"] for m in tool_messages)
    assert any("escapes workspace jail" in m["content"] for m in tool_messages)


# ---- max_iters cutoff -------------------------------------------------------


def test_max_iters_cutoff_produces_timeout_handoff(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    task = make_task()

    # The model calls list_dir forever and never calls done().
    responses = [
        _response(_message(tool_calls=[_tool_call(f"call_{i}", "list_dir", {})]))
        for i in range(5)
    ]
    client = FakeClient(responses)
    executor = Executor(client=client, model="qwen3-coder-plus", workspace=workspace, max_iters=5)

    handoff = executor.execute(task, dependency_handoffs=[])

    assert handoff.task_id == "T1"
    assert handoff.outcome == AttemptOutcome.TIMEOUT.value
    assert any("max_iters" in g for g in handoff.gotchas)
    assert len(client.chat.completions.calls) == 5


# ---- plain-text nudge -------------------------------------------------------


def test_plain_text_response_gets_nudged_then_counts_toward_max_iters(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    task = make_task()

    responses = [
        _response(_message(content="I am thinking about this task."))
        for _ in range(3)
    ]
    client = FakeClient(responses)
    executor = Executor(client=client, model="qwen3-coder-plus", workspace=workspace, max_iters=3)

    handoff = executor.execute(task, dependency_handoffs=[])

    # All 3 turns were plain text -> max_iters exhausted -> timeout handoff.
    assert handoff.outcome == AttemptOutcome.TIMEOUT.value
    assert len(client.chat.completions.calls) == 3

    # Each plain-text turn should have produced a nudge in the next request's messages.
    second_call_messages = client.chat.completions.calls[1]["messages"]
    assert any(
        m.get("role") == "user" and "use one of your tools" in m["content"].lower()
        for m in second_call_messages
    )


# ---- malformed tool-call arguments never poison the echoed history ---------
#
# Caught live: qwen-turbo occasionally emits a non-empty but syntactically
# invalid JSON string for a tool call's arguments. The prior fix only handled
# EMPTY arguments (some Qwen variants send "" for zero-arg calls); an
# invalid-but-non-empty string slipped through, got echoed verbatim into
# message history, and poisoned every subsequent turn with a 400 from
# DashScope ("function.arguments must be JSON format") — a Foreman run that
# had already passed planning died deep into execution. This test locks the
# fix: the echoed history must always carry valid JSON, even when the model's
# own arguments string does not parse.


def _tool_call_raw_arguments(call_id: str, name: str, raw_arguments: str):
    """Like _tool_call, but lets the test hand-supply a possibly-invalid raw
    arguments string instead of json.dumps()-ing a dict."""
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=raw_arguments),
    )


def test_malformed_json_arguments_are_sanitized_in_echoed_history(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    task = make_task()

    responses = [
        _response(
            _message(
                tool_calls=[
                    # Not valid JSON (unquoted keys, trailing comma) — a shape
                    # a weaker model can plausibly emit.
                    _tool_call_raw_arguments("call_1", "list_dir", "{path: '.', }")
                ]
            )
        ),
        _response(
            _message(
                tool_calls=[
                    _tool_call(
                        "call_2", "done",
                        {"summary": "done", "completed_work": [], "files_touched": [],
                         "gotchas": [], "self_check": []},
                    )
                ]
            )
        ),
    ]
    client = FakeClient(responses)
    executor = Executor(client=client, model="qwen3-coder-plus", workspace=workspace, max_iters=5)

    handoff = executor.execute(task, dependency_handoffs=[])

    assert handoff.outcome == AttemptOutcome.SUCCESS.value

    # The SECOND API call's message history is what would be sent to DashScope
    # next — every echoed tool_call's arguments string there must parse as JSON.
    second_call_messages = client.chat.completions.calls[1]["messages"]
    echoed_assistant = [
        m for m in second_call_messages
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert echoed_assistant, "expected an echoed assistant tool-call turn in history"
    for m in echoed_assistant:
        for tc in m["tool_calls"]:
            json.loads(tc["function"]["arguments"])  # must not raise

    # And the tool-execution loop still reported the parse error back to the
    # model on that same turn (existing behavior, unchanged by this fix).
    tool_results = [m for m in second_call_messages if m.get("role") == "tool"]
    assert any("could not parse arguments" in m["content"] for m in tool_results)
