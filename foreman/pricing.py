"""Approximate USD pricing for the Qwen/DashScope models Foreman uses.

Purely for the Product Console's cost estimate (contract §9.2) — a rough
"≈$0.0123" readout so a user can see a run's cost order of magnitude, not an
invoice. Prices drift; these are approximations as of 2026-07 — VERIFY IN THE
CONSOLE before trusting them for anything billing-adjacent.

Values are USD per 1,000,000 tokens, as (input_price, output_price).
"""

from __future__ import annotations

PRICES: dict[str, tuple[float, float]] = {
    "qwen-max": (1.6, 6.4),
    "qwen-plus": (0.4, 1.2),
    "qwen3-coder-plus": (1.0, 5.0),
    "qwen-turbo": (0.05, 0.2),
    "qwen-flash": (0.1, 0.4),
    "DEFAULT": (0.5, 1.5),
}


def estimate_usd(per_model: dict) -> float:
    """Estimate total USD cost from a ``{model: {"prompt_tokens", "completion_tokens", ...}}``
    breakdown, e.g. TokenMeter.run_totals(run_id)["per_model"].

    Unknown model names fall back to PRICES["DEFAULT"] rather than raising —
    the model catalog drifts and a missing entry should degrade to "rough
    estimate" instead of crashing the console.
    """
    total = 0.0
    for model, totals in (per_model or {}).items():
        in_price, out_price = PRICES.get(model, PRICES["DEFAULT"])
        prompt_tokens = totals.get("prompt_tokens", 0) or 0
        completion_tokens = totals.get("completion_tokens", 0) or 0
        total += (prompt_tokens / 1_000_000.0) * in_price
        total += (completion_tokens / 1_000_000.0) * out_price
    return total
