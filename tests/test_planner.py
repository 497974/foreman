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
