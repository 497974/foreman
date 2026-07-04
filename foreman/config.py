"""Configuration + Qwen (DashScope) client factory.

Loads a local .env (no third-party dotenv dependency) and exposes an
OpenAI-compatible client pointed at the workspace's DashScope endpoint, plus the
model chosen for each role. Model names are overridable via env because the
DashScope catalog drifts — never hard-code a model string you haven't confirmed
against the live console.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
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

    @classmethod
    def from_env(cls, env_path: str | os.PathLike = ".env") -> "Settings":
        load_env(env_path)
        api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "DASHSCOPE_API_KEY not found. Put it in .env or the environment."
            )
        base_url = os.environ.get(
            "DASHSCOPE_BASE_URL",
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        )
        return cls(
            api_key=api_key,
            base_url=base_url,
            # Roles per the plan; override with FOREMAN_*_MODEL if the catalog differs.
            planner_model=os.environ.get("FOREMAN_PLANNER_MODEL", "qwen-max"),
            executor_model=os.environ.get("FOREMAN_EXECUTOR_MODEL", "qwen3-coder-plus"),
            verifier_model=os.environ.get("FOREMAN_VERIFIER_MODEL", "qwen-plus"),
            executor_backend=os.environ.get("FOREMAN_EXECUTOR_BACKEND", "native"),
        )


def make_client(settings: Optional[Settings] = None):
    """Return an OpenAI-compatible client bound to the DashScope endpoint."""
    from openai import OpenAI

    s = settings or Settings.from_env()
    return OpenAI(api_key=s.api_key, base_url=s.base_url)
