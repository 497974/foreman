"""TokenMeter: a tiny thread-safe counter for LLM spend.

Foreman makes a lot of model calls per run (planner, executor's tool loop,
verifier, arbiter) and the evaluation harness needs an honest per-condition
cost comparison alongside the pass-count comparison — a condition that
"wins" by burning ten times the tokens is not actually the better design.
Rather than thread a return value through every call site, every module that
talks to the model records into one process-wide singleton here; the caller
that wants a comparison just resets it, runs one condition, and snapshots it.

Addendum 2 (contract §9.1) adds per-run attribution on top of the same
singleton: the Product Console needs a live "≈$0.0123 · 45,678 tok" readout
per run, and several runs may be executing concurrently on their own worker
threads. Rather than thread a run_id through every chat_json/executor call
site, each worker thread tags itself once (``set_current_run`` at the top of
Orchestrator.run_checklist/run_tasks/resume_run) and ``record`` reads that
thread-local tag automatically — the call sites that already exist do not
need to change at all.

Kept deliberately dumb: a dict of per-model counters behind a lock, plus a
dict-of-dicts for the per-run breakdown. No persistence, no cost-in-dollars
conversion (model pricing drifts and isn't this module's job — see
foreman/pricing.py) — just prompt/completion token and call counts.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class _ModelTotals:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0

    def as_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "calls": self.calls,
        }


# ---- per-run thread-local tagging (contract §9.1) --------------------------

_thread_local = threading.local()


def set_current_run(run_id: str | None) -> None:
    """Tag the CALLING thread with a run_id so subsequent METER.record() calls
    on this same thread are also attributed to that run. Pass None to clear.

    Orchestrator sets this at the top of run_checklist/run_tasks/resume_run
    (they execute on the worker thread) and clears it in a finally, so a
    crash or early return never leaves a stale tag behind for whatever the
    thread does next (thread pools / daemon threads can be reused).
    """
    _thread_local.run_id = run_id


def get_current_run() -> str | None:
    """The run_id the CALLING thread is currently tagged with, or None."""
    return getattr(_thread_local, "run_id", None)


class TokenMeter:
    """Thread-safe accumulator of token usage, broken down per model.

    Also accumulates a second, independent breakdown per run_id (see
    ``run_totals``) whenever the recording thread is tagged via
    ``set_current_run``. The two breakdowns are separate dicts so resetting
    (or querying) one never disturbs the other.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._per_model: dict[str, _ModelTotals] = {}
        # run_id -> model -> _ModelTotals
        self._per_run: dict[str, dict[str, _ModelTotals]] = {}

    def record(self, model: str, prompt_tokens: int, completion_tokens: int, calls: int = 1) -> None:
        run_id = get_current_run()
        with self._lock:
            totals = self._per_model.setdefault(model, _ModelTotals())
            totals.prompt_tokens += int(prompt_tokens or 0)
            totals.completion_tokens += int(completion_tokens or 0)
            totals.calls += int(calls or 0)

            if run_id:
                run_bucket = self._per_run.setdefault(run_id, {})
                run_totals = run_bucket.setdefault(model, _ModelTotals())
                run_totals.prompt_tokens += int(prompt_tokens or 0)
                run_totals.completion_tokens += int(completion_tokens or 0)
                run_totals.calls += int(calls or 0)

    def snapshot(self) -> dict:
        """Return per-model totals plus a grand total, safe to serialize as-is."""
        with self._lock:
            per_model = {model: totals.as_dict() for model, totals in self._per_model.items()}

        grand = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
        for totals in per_model.values():
            grand["prompt_tokens"] += totals["prompt_tokens"]
            grand["completion_tokens"] += totals["completion_tokens"]
            grand["total_tokens"] += totals["total_tokens"]
            grand["calls"] += totals["calls"]

        return {"per_model": per_model, "grand_total": grand}

    def run_totals(self, run_id: str) -> dict:
        """Per-model breakdown + totals for one run_id (contract §9.1).

        Shape: {"per_model": {model: {...}}, "totals": {"prompt_tokens",
        "completion_tokens", "calls"}}. Returns all-zero totals (empty
        per_model) for a run_id that never recorded anything — callers treat
        that the same as "no usage yet", not an error.
        """
        with self._lock:
            bucket = self._per_run.get(run_id, {})
            per_model = {model: totals.as_dict() for model, totals in bucket.items()}

        totals = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
        for m in per_model.values():
            totals["prompt_tokens"] += m["prompt_tokens"]
            totals["completion_tokens"] += m["completion_tokens"]
            totals["calls"] += m["calls"]

        return {"per_model": per_model, "totals": totals}

    def reset(self) -> None:
        with self._lock:
            self._per_model.clear()
            self._per_run.clear()


# Module-level singleton: every call site (llm.chat_json, executor's tool
# loop) imports this same instance rather than passing a meter around —
# simple beats elegant here, per the brief.
METER = TokenMeter()
