"""Thin helpers for talking to Qwen in a structured (JSON) way.

Shared by the Planner, Verifier and Arbiter. Handles the two DashScope quirks
the research flagged: JSON mode requires the word "json" somewhere in the
messages, and models occasionally wrap output in a ```json fence.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Optional

from .telemetry import METER

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _strip_fence(text: str) -> str:
    return _FENCE.sub("", text).strip()


# ---- model fallback chain (contract §12) -----------------------------------
#
# We hit "insufficient_quota" three times in practice and the whole run died.
# create_with_fallback() is the one place that knows how to route around that:
# on a quota/permission error for model M, it transparently retries the exact
# same call with the next model in fallback_models (skipping M itself), until
# one works or the chain is exhausted. Both chat_json (below) and the
# executor's raw tool-calling loop (foreman/executor.py) go through this.


def on_model_fallback(original: str, used: str) -> None:
    """Default no-op hook, fired when a substitution happens.

    The orchestrator overwrites this at startup (module-level, so both
    chat_json and the executor's calls — anything importing foreman.llm —
    report through the same hook) to emit a "model_fallback" event to
    events.jsonl. Kept as a plain module attribute (not a class) so tests can
    monkeypatch ``foreman.llm.on_model_fallback`` directly.
    """


def on_rate_limit_wait(model: str, delay_s: float) -> None:
    """Default no-op hook, fired before sleeping out a per-minute rate limit.

    The orchestrator overrides this to emit a "rate_limit_wait" event so the
    console can show "waiting 45s for the free-tier rate window" instead of
    looking frozen. Same module-attribute pattern as on_model_fallback."""


def _is_quota_or_forbidden_error(exc: BaseException) -> bool:
    """Best-effort sniff of an insufficient_quota / 403 style error.

    We don't depend on any specific SDK exception hierarchy here (that would
    couple us to the exact `openai` version) — we inspect the exception's
    message and, if present, a `status_code` attribute, which is how the
    `openai` SDK's APIStatusError subclasses expose the HTTP status.
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    text = str(exc).lower()
    if "insufficient_quota" in text:
        return True
    if status == 403:
        return True
    if "403" in text and "forbidden" in text:
        return True
    return False


def _is_persistent_429(exc: BaseException) -> bool:
    """A 429 (rate limit) that we treat as chain-worthy.

    Contract: "429 after retries" persists — since chat_json's own retry loop
    already re-issues the same call on failure, by the time create_with_fallback
    is asked to fall back it is reasonable to treat any 429 as persistent for
    that call and just move to the next model rather than hang the run.
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    text = str(exc).lower()
    return status == 429 or "429" in text or "rate limit" in text or "rate_limit" in text


def _rate_limit_retry_delay(exc: BaseException) -> Optional[float]:
    """Seconds to wait before retrying the SAME model after a 429, or None.

    Free tiers rate-limit by requests-per-MINUTE (e.g. Google AI Studio's
    gemini free tier is 5 RPM) and say so explicitly: the 429 body carries a
    ``retryDelay``/"retry in 45s" hint. That is NOT quota exhaustion — the
    right response is to wait the stated delay and try again, not to give up
    or burn a fallback model (on a single-model free tier there IS no other
    model). We only honor SHORT delays (<= 60s): a longer one means the
    per-DAY quota is gone, where sleeping inside one run would just hang it.
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    text = str(exc)
    is_429 = status == 429 or "429" in text or "RESOURCE_EXHAUSTED" in text
    if not is_429:
        return None
    # "retry in 45.6s", "retryDelay': '45s'", "retryDelay: 45"
    m = re.search(r"retry\w*[^0-9]{0,12}(\d+(?:\.\d+)?)\s*s", text, re.IGNORECASE)
    if m:
        secs = float(m.group(1))
        return secs + 1.0 if secs <= 60 else None
    # A 429 with NO explicit delay hint is left to the existing behavior
    # (treated as persistent → fall through to model chaining). We only spend
    # a run's wall-clock sleeping when the provider explicitly told us how long
    # to wait — otherwise a multi-model fallback chain is the faster recovery.
    return None


def _is_transient_connection_error(exc: BaseException) -> bool:
    """A network-level failure worth retrying against the SAME model.

    Found live, not hypothetically: one dropped connection during a
    verifier call killed an entire resumed run (openai.APIConnectionError
    propagated straight up). Class-name sniffing keeps us decoupled from the
    SDK's exact exception hierarchy, same policy as the quota sniffers above.
    """
    if exc.__class__.__name__ in ("APIConnectionError", "APITimeoutError"):
        return True
    text = str(exc).lower()
    return "connection error" in text or "connection reset" in text or "connection aborted" in text


# Same-model retries for transient network failures: 3 tries with short
# backoff. Deliberately small — this exists to ride out a dropped packet,
# not to wait out an outage (a run stuck sleeping looks like a hang).
_TRANSIENT_RETRIES = 3
_TRANSIENT_BACKOFF_S = (1.0, 2.0, 4.0)

# Same-model retries for per-minute rate limits (free-tier 429 with a short
# retryDelay). Enough to ride out a couple of RPM windows without hanging
# forever: 6 waits of <=60s each caps the total stall at a few minutes.
_RATE_LIMIT_RETRIES = 6


def create_with_fallback(
    client,
    model: str,
    fallback_models: list[str] | None = None,
    **create_kwargs: Any,
):
    """Call ``client.chat.completions.create`` with automatic model fallback.

    On an insufficient_quota/403 (or a persistent 429) for ``model``, retries
    the exact same call against the next model in ``fallback_models`` that is
    not equal to the one that just failed, in order, until one succeeds or the
    list is exhausted (in which case the original exception is re-raised).

    A TRANSIENT network error (dropped connection, timeout) is different: the
    model is fine, the wire hiccuped — so the same call is retried against the
    SAME model up to ``_TRANSIENT_RETRIES`` times with short backoff before
    the error is allowed to propagate. Without this, one dropped packet kills
    an entire run (observed live during a resume).

    Any other exception (e.g. a plain 400 — bad request) is never swallowed:
    it propagates immediately, since silently masking a real bug behind
    "just try another model" would hide it forever.
    """
    fallback_models = fallback_models or []
    tried = {model}
    current = model
    transient_used = 0
    rate_limit_used = 0

    while True:
        try:
            return client.chat.completions.create(model=current, **create_kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised below if not retryable
            if _is_transient_connection_error(exc):
                if transient_used >= _TRANSIENT_RETRIES:
                    raise
                time.sleep(_TRANSIENT_BACKOFF_S[min(transient_used, len(_TRANSIENT_BACKOFF_S) - 1)])
                transient_used += 1
                continue
            # A per-minute rate limit with a short retry hint: wait it out on
            # the SAME model before considering it fatal. This is what makes a
            # free-tier key (e.g. Gemini 5 RPM) actually complete a run instead
            # of dying on the first burst. Only after exhausting these does a
            # 429 count as "persistent" and fall through to model chaining.
            rl_delay = _rate_limit_retry_delay(exc)
            if rl_delay is not None and rate_limit_used < _RATE_LIMIT_RETRIES:
                on_rate_limit_wait(current, rl_delay)
                time.sleep(rl_delay)
                rate_limit_used += 1
                continue
            if not (_is_quota_or_forbidden_error(exc) or _is_persistent_429(exc)):
                raise
            next_model = next(
                (m for m in fallback_models if m and m not in tried), None
            )
            if next_model is None:
                raise
            tried.add(next_model)
            on_model_fallback(current, next_model)
            current = next_model


def chat_json(
    client,
    model: str,
    system: str,
    user: str,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    retries: int = 2,
    fallback_models: list[str] | None = None,
) -> dict[str, Any]:
    """Call the model and parse a JSON object from its reply.

    Retries on a parse failure, re-asking with the malformed output quoted back.
    The word "json" is guaranteed present (DashScope rejects json mode otherwise).
    ``fallback_models`` (contract §12) is threaded through to
    ``create_with_fallback`` so a quota/403 error on ``model`` transparently
    substitutes the next model in the chain instead of killing the run;
    defaults to an empty list so existing callers see no behavior change.
    """
    messages = [
        {"role": "system", "content": system + "\n\nRespond with a single JSON object."},
        {"role": "user", "content": user},
    ]
    last_err = ""
    for attempt in range(retries + 1):
        resp = create_with_fallback(
            client,
            model,
            fallback_models,
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
