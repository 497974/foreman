"""Planner parsing/validation tests — pure functions, no API calls."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from foreman.planner import _as_list, _validate_dag
from foreman.models import Task


def test_as_list_coerces_string_and_csv():
    assert _as_list(None) == []
    assert _as_list("T01") == ["T01"]                 # bare string, not walked char-wise
    assert _as_list("T01, T02") == ["T01", "T02"]     # comma-joined string
    assert _as_list(["T01", " T02 "]) == ["T01", "T02"]
    assert _as_list([]) == []


def _t(tid, parents=None):
    return Task(task_id=tid, title=tid, description="x", parents=parents or [])


def test_validate_dag_accepts_valid_chain():
    _validate_dag([_t("T01"), _t("T02", ["T01"]), _t("T03", ["T02"])])  # no raise


def test_validate_dag_rejects_dangling_dependency():
    with pytest.raises(ValueError, match="unknown task"):
        _validate_dag([_t("T01", ["T99"])])


def test_validate_dag_rejects_cycle():
    with pytest.raises(ValueError, match="cycle"):
        _validate_dag([_t("A", ["B"]), _t("B", ["A"])])


# ---- planner output validation gate (added after the empty-exam incident) ---


class _FakeResp:
    def __init__(self, payload):
        import json as _json
        from types import SimpleNamespace
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=_json.dumps(payload)))]
        self.usage = None


class _FakeClient:
    """Returns each payload in turn — lets a test script a bad plan then a fix."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = 0

        class _Completions:
            def __init__(self, outer):
                self._outer = outer

            def create(self, **kwargs):
                self._outer.calls += 1
                return _FakeResp(self._outer._payloads.pop(0))

        class _Chat:
            def __init__(self, outer):
                self.completions = _Completions(outer)

        self.chat = _Chat(self)


def _plan_payload(strategy):
    return {"tasks": [{
        "id": "T01", "title": "t", "description": "d",
        "acceptance_criteria": ["works"], "test_strategy": strategy,
        "dependencies": [], "role": "backend", "complexity": 1,
    }]}


def test_empty_test_strategy_is_reasked_then_accepted():
    from foreman.planner import Planner
    client = _FakeClient([_plan_payload(""), _plan_payload("python -m pytest test_t.py -q")])
    tasks = Planner(client, "fake").plan("1. do the thing")
    assert client.calls == 2                      # first plan rejected, re-asked
    assert tasks[0].test_strategy == "python -m pytest test_t.py -q"


def test_test_strategy_alias_keys_accepted():
    from foreman.planner import Planner
    payload = {"tasks": [{
        "id": "T01", "title": "t", "description": "d",
        "acceptance_criteria": ["works"], "testStrategy": "python -m pytest x.py -q",
        "dependencies": [],
    }]}
    tasks = Planner(_FakeClient([payload]), "fake").plan("1. thing")
    assert tasks[0].test_strategy == "python -m pytest x.py -q"


def test_persistently_empty_strategy_raises():
    from foreman.planner import Planner
    bad = _plan_payload("")
    with pytest.raises(ValueError, match="test_strategy"):
        Planner(_FakeClient([bad, bad, bad]), "fake").plan("1. thing")
