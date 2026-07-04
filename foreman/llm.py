"""Thin helpers for talking to Qwen in a structured (JSON) way.

Shared by the Planner, Verifier and Arbiter. Handles the two DashScope quirks
the research flagged: JSON mode requires the word "json" somewhere in the
messages, and models occasionally wrap output in a ```json fence.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .telemetry import METER

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _strip_fence(text: str) -> str:
    return _FENCE.sub("", text).strip()


def chat_json(
    client,
    model: str,
    system: str,
    user: str,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    retries: int = 2,
) -> dict[str, Any]:
    """Call the model and parse a JSON object from its reply.

    Retries on a parse failure, re-asking with the malformed output quoted back.
    The word "json" is guaranteed present (DashScope rejects json mode otherwise).
    """
    messages = [
        {"role": "system", "content": system + "\n\nRespond with a single JSON object."},
        {"role": "user", "content": user},
    ]
    last_err = ""
    for attempt in range(retries + 1):
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format={"type": "json_object"},
        )
        usage = getattr(resp, "usage", None)
        if usage is not None:
            METER.record(
                model,
                getattr(usage, "prompt_tokens", 0),
                getattr(usage, "completion_tokens", 0),
            )

        raw = resp.choices[0].message.content or ""
        try:
            return json.loads(_strip_fence(raw))
        except json.JSONDecodeError as e:
            last_err = f"{e}: {raw[:200]}"
            messages.append({"role": "assistant", "content": raw})
            messages.append(
                {"role": "user", "content": "That was not valid JSON. Reply with "
                 "ONLY a valid JSON object, no prose, no code fence."}
            )
    raise ValueError(f"model did not return valid JSON after {retries + 1} tries: {last_err}")
