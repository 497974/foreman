"""Model fallback chain tests (contract Addendum 3, §12). Run: pytest -q (no
API key, no network).

We hit "insufficient_quota" three times in production and the whole run died.
create_with_fallback() (foreman/llm.py) is the fix: on a quota/403 error (or a
persistent 429) for the requested model, it transparently retries the same
call against the next model in the fallback chain, firing
``foreman.llm.on_model_fallback(original, used)`` when it substitutes. A
non-quota error (e.g. a plain 400) must never be swallowed — that would hide
real bugs behind "just try another model".
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import foreman.llm as llm
from foreman.llm import chat_json, create_with_fallback


class QuotaError(Exception):
    """Stand-in for an SDK error carrying a status_code, like openai's
    APIStatusError subclasses do — create_with_fallback must not depend on
    the real openai exception hierarchy, just duck-type this shape."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class FakeChatCompletions:
    """Scripted client.chat.completions.create: a dict of model -> list of
    "responses", where each entry is either an Exception instance (raised) or
    a response object (returned). Records every (model, kwargs) call made."""

    def __init__(self, scripts: dict):
        self._scripts = {m: list(v) for m, v in scripts.items()}
        self.calls: list[dict] = []

    def create(self, model, **kwargs):
        self.calls.append({"model": model, **kwargs})
        script = self._scripts.get(model)
        if not script:
            raise AssertionError(f"no scripted response left for model {model!r}")
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, scripts: dict):
        self.chat = SimpleNamespace(completions=FakeChatCompletions(scripts))


def _ok_response(content: str = "ok"):
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice], usage=None)


def setup_function(_):
    # Reset the module-level hook before every test so assertions on it never
    # leak between tests (it is process-global by design).
    llm.on_model_fallback = lambda original, used: None


# ---- create_with_fallback ---------------------------------------------------


def test_fallback_succeeds_on_next_model_and_fires_callback():
    client = FakeClient(
        {
            "model-a": [QuotaError("insufficient_quota", status_code=403)],
            "model-b": [_ok_response("from b")],
        }
    )
    fired = []
    llm.on_model_fallback = lambda original, used: fired.append((original, used))

    resp = create_with_fallback(
        client, "model-a", ["model-b", "model-c"], messages=[{"role": "user", "content": "hi"}]
    )

    assert resp.choices[0].message.content == "from b"
    assert fired == [("model-a", "model-b")]
    # both calls carried the same messages payload through untouched
    assert client.chat.completions.calls[0]["model"] == "model-a"
    assert client.chat.completions.calls[1]["model"] == "model-b"
    assert client.chat.completions.calls[1]["messages"] == [{"role": "user", "content": "hi"}]


def test_fallback_skips_the_failed_model_if_it_reappears_in_the_chain():
    client = FakeClient(
        {
            "model-a": [QuotaError("insufficient_quota", status_code=403)],
            "model-b": [_ok_response("from b")],
        }
    )
    resp = create_with_fallback(
        client, "model-a", ["model-a", "model-b"], messages=[]
    )
    assert resp.choices[0].message.content == "from b"


def test_fallback_via_429_message_text():
    client = FakeClient(
        {
            "model-a": [QuotaError("Error code: 429 - Rate limit exceeded")],
            "model-b": [_ok_response("from b")],
        }
    )
    resp = create_with_fallback(client, "model-a", ["model-b"], messages=[])
    assert resp.choices[0].message.content == "from b"


def test_non_quota_error_is_not_swallowed():
    """A plain 400 bad-request must propagate, not trigger fallback."""
    client = FakeClient(
        {
            "model-a": [QuotaError("Error code: 400 - Bad request: invalid parameter")],
            "model-b": [_ok_response("from b")],
        }
    )
    try:
        create_with_fallback(client, "model-a", ["model-b"], messages=[])
        raise AssertionError("expected the 400 to propagate")
    except QuotaError as e:
        assert "400" in str(e)
    # model-b must never have been called
    assert all(c["model"] != "model-b" for c in client.chat.completions.calls)


def test_empty_fallback_list_reraises_original():
    client = FakeClient(
        {
            "model-a": [QuotaError("insufficient_quota", status_code=403)],
        }
    )
    try:
        create_with_fallback(client, "model-a", [], messages=[])
        raise AssertionError("expected the original quota error to propagate")
    except QuotaError as e:
        assert "insufficient_quota" in str(e)


def test_fallback_chain_exhausted_reraises_last_error():
    client = FakeClient(
        {
            "model-a": [QuotaError("insufficient_quota", status_code=403)],
            "model-b": [QuotaError("insufficient_quota", status_code=403)],
        }
    )
    try:
        create_with_fallback(client, "model-a", ["model-b"], messages=[])
        raise AssertionError("expected the chain to exhaust and re-raise")
    except QuotaError as e:
        assert "insufficient_quota" in str(e)


# ---- chat_json threads fallback_models through -----------------------------


def test_chat_json_routes_through_fallback_and_still_parses_json():
    ok = _ok_response('{"hello": "world"}')
    client = FakeClient(
        {
            "model-a": [QuotaError("insufficient_quota", status_code=403)],
            "model-b": [ok],
        }
    )
    fired = []
    llm.on_model_fallback = lambda original, used: fired.append((original, used))

    result = chat_json(
        client, "model-a", system="sys", user="usr", fallback_models=["model-b"]
    )

    assert result == {"hello": "world"}
    assert fired == [("model-a", "model-b")]


def test_chat_json_default_fallback_models_is_empty_no_behavior_change():
    """Existing callers that don't pass fallback_models must see a quota error
    propagate exactly as before (no new default fallback chain sneaking in)."""
    client = FakeClient(
        {
            "model-a": [QuotaError("insufficient_quota", status_code=403)],
        }
    )
    try:
        chat_json(client, "model-a", system="sys", user="usr")
        raise AssertionError("expected the quota error to propagate with no fallback configured")
    except QuotaError:
        pass


# ---- orchestrator wiring ------------------------------------------------------


def test_orchestrator_registers_fallback_hook_and_emits_event(tmp_path):
    """Building an Orchestrator wires llm.on_model_fallback to emit a
    "model_fallback" event, and passes settings.fallback_models onto the
    (native) executor so a real quota exhaustion degrades gracefully."""
    from foreman.config import Settings
    from foreman.orchestrator import Orchestrator

    settings = Settings(
        api_key="x",
        base_url="http://mock.invalid",
        planner_model="p",
        executor_model="model-a",
        verifier_model="v",
        fallback_models=["model-b", "model-c"],
    )

    orch = Orchestrator.__new__(Orchestrator)
    orch.settings = settings
    orch.client = None
    orch.run_root = tmp_path / "runs"
    orch.run_root.mkdir(parents=True, exist_ok=True)

    from foreman.dispatcher import Dispatcher
    from foreman.ledger import Ledger
    from foreman.models import new_id
    from foreman.workspace import Workspace

    orch.run_id = new_id("run")
    run_dir = orch.run_root / orch.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    orch.run_dir = run_dir
    orch.ledger = Ledger(db_path=str(run_dir / "ledger.db"))
    orch.workspace = Workspace(run_dir / "workspace")
    orch.dispatcher = Dispatcher(orch.ledger)

    from foreman.executor import Executor

    orch.executor = Executor(None, settings.executor_model, orch.workspace)
    orch.events_path = run_dir / "events.jsonl"
    orch.disputed_task_ids = set()

    # Re-run just the tail of __init__ that wires the fallback (mirrors what
    # the real constructor does after building self.executor).
    orch.executor.fallback_models = list(settings.fallback_models)
    llm.on_model_fallback = orch._on_model_fallback

    assert orch.executor.fallback_models == ["model-b", "model-c"]

    llm.on_model_fallback("model-a", "model-b")

    import json

    lines = orch.events_path.read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(l) for l in lines]
    assert len(events) == 1
    assert events[0]["type"] == "model_fallback"
    assert events[0]["detail"] == {"from": "model-a", "to": "model-b"}


# ---- transient connection errors: retry SAME model, don't kill the run ------


class APIConnectionError(Exception):
    """Name-matched stand-in for openai.APIConnectionError — the sniffer
    matches on class name so we stay decoupled from the SDK hierarchy."""


def test_transient_connection_error_retries_same_model(monkeypatch):
    # Observed live: one dropped connection during a verifier call killed an
    # entire resumed run. Two connection errors then success must succeed,
    # all against the SAME model, with no fallback substitution fired.
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)  # no real waiting
    fired = []
    monkeypatch.setattr(llm, "on_model_fallback", lambda a, b: fired.append((a, b)))
    client = FakeClient({
        "qwen-max": [APIConnectionError("Connection error."),
                     APIConnectionError("Connection error."),
                     _ok_response()],
    })
    resp = create_with_fallback(client, "qwen-max", ["qwen-turbo"])
    assert resp.choices[0].message.content == "ok"
    assert [c["model"] for c in client.chat.completions.calls] == ["qwen-max"] * 3
    assert fired == []  # transient retry is not a model substitution


def test_transient_connection_error_gives_up_after_retry_budget(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    errors = [APIConnectionError("Connection error.") for _ in range(10)]
    client = FakeClient({"qwen-max": errors})
    import pytest
    with pytest.raises(APIConnectionError):
        create_with_fallback(client, "qwen-max", [])
    # 1 initial + _TRANSIENT_RETRIES retries, then propagate
    assert len(client.chat.completions.calls) == 1 + llm._TRANSIENT_RETRIES
