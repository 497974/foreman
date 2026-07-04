"""TokenMeter: a tiny thread-safe counter for LLM spend.

Foreman makes a lot of model calls per run (planner, executor's tool loop,
verifier, arbiter) and the evaluation harness needs an honest per-condition
cost comparison alongside the pass-count comparison — a condition that
"wins" by burning ten times the tokens is not actually the better design.
Rather than thread a return value through every call site, every module that
talks to the model records into one process-wide singleton here; the caller
that wants a comparison just resets it, runs one condition, and snapshots it.

Kept deliberately dumb: a dict of per-model counters behind a lock. No
persistence, no cost-in-dollars conversion (model pricing drifts and isn't
this module's job) — just prompt/completion token and call counts.
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


class TokenMeter:
    """Thread-safe accumulator of token usage, broken down per model."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._per_model: dict[str, _ModelTotals] = {}

    def record(self, model: str, prompt_tokens: int, completion_tokens: int, calls: int = 1) -> None:
        with self._lock:
            totals = self._per_model.setdefault(model, _ModelTotals())
            totals.prompt_tokens += int(prompt_tokens or 0)
            totals.completion_tokens += int(completion_tokens or 0)
            totals.calls += int(calls or 0)

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

    def reset(self) -> None:
        with self._lock:
            self._per_model.clear()


# Module-level singleton: every call site (llm.chat_json, executor's tool
# loop) imports this same instance rather than passing a meter around —
# simple beats elegant here, per the brief.
METER = TokenMeter()
