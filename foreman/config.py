"""Configuration + LLM client factory.

Loads a local .env (no third-party dotenv dependency) and exposes an
OpenAI-compatible client plus the model chosen for each role.

Two providers are supported through the SAME OpenAI-compatible surface:

* ``qwen`` (default) — Alibaba DashScope. This is the hackathon target and
  the documented default; the FC deployment and the committed evaluation
  evidence all run on Qwen.
* ``gemini`` — Google AI Studio via its OpenAI-compatible endpoint
  (generativelanguage.googleapis.com/.../openai). This exists as a real
  fallback for when the DashScope free per-model quota is exhausted: set
  ``FOREMAN_PROVIDER=gemini`` and a ``GEMINI_API_KEY`` and every role runs on
  free Gemini instead, no code change. Nothing else in Foreman knows or cares
  which one is active — it is one base_url + key + model triple either way.

Model names are overridable per role via ``FOREMAN_*_MODEL`` because both
catalogs drift — never hard-code a model string you haven't confirmed live.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def load_env(path: str | os.PathLike = ".env") -> None:
    """Parse KEY=VALUE lines from a .env file into os.environ (won't overwrite)."""
    p = Path(path)
    if not p.exists():
        return
    # utf-8-sig tolerates a BOM written by Windows editors
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def _default_fallback_models() -> list[str]:
    return ["qwen-turbo", "qwen-flash", "qwen3-coder-flash"]


GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# Per-provider defaults. Roles can still be overridden individually with
# FOREMAN_PLANNER_MODEL / FOREMAN_EXECUTOR_MODEL / FOREMAN_VERIFIER_MODEL.
_PROVIDER_DEFAULTS = {
    "qwen": {
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "planner": "qwen-max",
        "executor": "qwen3-coder-plus",
        "verifier": "qwen-plus",
        "fallback": _default_fallback_models(),
        "key_names": ("DASHSCOPE_API_KEY",),
        "base_env": "DASHSCOPE_BASE_URL",
    },
    "gemini": {
        # gemini-2.5-flash is the workhorse with the most generous free tier;
        # 2.5-pro / 2.0-flash have tighter free limits (verified live).
        "base_url": GEMINI_BASE_URL,
        "planner": "gemini-2.5-flash",
        "executor": "gemini-2.5-flash",
        "verifier": "gemini-2.5-flash",
        "fallback": ["gemini-2.5-flash"],
        "key_names": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "base_env": "GEMINI_BASE_URL",
    },
}


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    planner_model: str
    executor_model: str
    verifier_model: str
    # Which executor writes the code: "native" (hand-written tool loop, default)
    # or "qwen-code" (delegate to Alibaba's qwen-code CLI). See foreman/backends.py.
    executor_backend: str = "native"
    # Contract §12: models to fall back to (in order) when the primary model
    # hits insufficient_quota/403 or a persistent 429. We hit this three times
    # in practice and the whole run died — this is what makes it a non-issue.
    fallback_models: list[str] = field(default_factory=_default_fallback_models)

    # Which LLM provider is active ("qwen" or "gemini"); surfaced so callers
    # (the console's config panel, telemetry) can report it.
    provider: str = "qwen"

    @classmethod
    def from_env(cls, env_path: str | os.PathLike = ".env") -> "Settings":
        load_env(env_path)
        provider = os.environ.get("FOREMAN_PROVIDER", "qwen").strip().lower()
        if provider not in _PROVIDER_DEFAULTS:
            provider = "qwen"
        pd = _PROVIDER_DEFAULTS[provider]

        api_key = ""
        for name in pd["key_names"]:
            api_key = os.environ.get(name, "")
            if api_key:
                break
        if not api_key:
            names = " or ".join(pd["key_names"])
            raise RuntimeError(
                f"{names} not found (provider={provider}). Put it in .env or the "
                "environment. To use Google's free Gemini tier instead of Qwen, "
                "set FOREMAN_PROVIDER=gemini and GEMINI_API_KEY."
            )

        base_url = os.environ.get(pd["base_env"], pd["base_url"])

        fallback_raw = os.environ.get("FOREMAN_FALLBACK_MODELS", "")
        fallback_models = (
            [m.strip() for m in fallback_raw.split(",") if m.strip()]
            if fallback_raw.strip()
            else list(pd["fallback"])
        )
        return cls(
            api_key=api_key,
            base_url=base_url,
            # Roles default per provider; override with FOREMAN_*_MODEL.
            planner_model=os.environ.get("FOREMAN_PLANNER_MODEL", pd["planner"]),
            executor_model=os.environ.get("FOREMAN_EXECUTOR_MODEL", pd["executor"]),
            verifier_model=os.environ.get("FOREMAN_VERIFIER_MODEL", pd["verifier"]),
            executor_backend=os.environ.get("FOREMAN_EXECUTOR_BACKEND", "native"),
            fallback_models=fallback_models,
            provider=provider,
        )


def make_client(settings: Optional[Settings] = None):
    """Return an OpenAI-compatible client bound to the DashScope endpoint."""
    from openai import OpenAI

    s = settings or Settings.from_env()
    return OpenAI(api_key=s.api_key, base_url=s.base_url)
