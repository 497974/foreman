"""One-time wiring: point a local hermes-agent install at DashScope Qwen.

Run once after installing hermes-agent:

    python scripts/setup_hermes.py

What it does (and nothing else):
1. writes/updates the ``model:`` block in ``~/.hermes/config.yaml`` to call
   the same DashScope OpenAI-compatible endpoint Foreman uses ("when
   base_url is set, Hermes ignores the provider and calls that endpoint
   directly" — Hermes configuration docs);
2. writes ``OPENAI_API_KEY`` into ``~/.hermes/.env`` from Foreman's own
   ``.env`` (the key never appears on a command line or in shell history).

Idempotent: safe to re-run. It REFUSES to touch a config.yaml it cannot
parse-preserve (no YAML dependency — we do a marker-based block replace) and
backs the original up to config.yaml.bak first.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foreman.config import Settings  # noqa: E402

# Hermes config lives in ~/.hermes on Linux/macOS but %LOCALAPPDATA%\hermes
# on native Windows (verified against a real 0.18.0 install — the installer
# creates only the latter). Probe both, prefer whichever exists.
_CANDIDATES = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "hermes",
    Path.home() / ".hermes",
]
HERMES_DIR = next((p for p in _CANDIDATES if str(p) != "hermes" and p.is_dir()), _CANDIDATES[-1])

BLOCK_BEGIN = "# --- foreman-managed model block (setup_hermes.py) ---"
BLOCK_END = "# --- end foreman-managed block ---"


def main() -> int:
    settings = Settings.from_env()

    if not HERMES_DIR.is_dir():
        print(f"error: {HERMES_DIR} does not exist — install hermes-agent first:")
        print("  iex (irm https://hermes-agent.nousresearch.com/install.ps1)")
        return 1

    model_block = "\n".join(
        [
            BLOCK_BEGIN,
            "model:",
            "  provider: custom",
            f'  model: "{settings.executor_model}"',
            f'  base_url: "{settings.base_url}"',
            BLOCK_END,
            "",
        ]
    )

    config_path = HERMES_DIR / "config.yaml"
    if config_path.is_file():
        original = config_path.read_text(encoding="utf-8")
        config_path.with_suffix(".yaml.bak").write_text(original, encoding="utf-8")
        if BLOCK_BEGIN in original:
            # replace our previous block in place
            pattern = re.compile(
                re.escape(BLOCK_BEGIN) + r".*?" + re.escape(BLOCK_END) + r"\n?",
                re.DOTALL,
            )
            updated = pattern.sub(model_block, original)
        elif (
            'provider: "auto"' in original
            and 'base_url: "https://openrouter.ai/api/v1"' in original
        ):
            # The installer's STOCK template (verified against a real 0.18.0
            # install): provider auto + OpenRouter base_url + an anthropic
            # default model. Retarget those three keys in place, preserving
            # the template's extensive comments (they document every provider
            # and are genuinely useful to keep).
            updated = original.replace(
                'provider: "auto"', 'provider: "custom"', 1
            ).replace(
                'base_url: "https://openrouter.ai/api/v1"',
                f'base_url: "{settings.base_url}"', 1,
            )
            updated = re.sub(
                r'^(\s*default:\s*)"[^"]*"',
                rf'\g<1>"{settings.executor_model}"',
                updated, count=1, flags=re.MULTILINE,
            )
        elif re.search(r"^model\s*:", original, re.MULTILINE):
            print(
                f"error: {config_path} already has a customized model: block not "
                "managed by this script — edit it manually (set model.base_url to "
                f"{settings.base_url} and model.model to {settings.executor_model}) "
                "or remove that block and re-run."
            )
            return 1
        else:
            updated = original.rstrip("\n") + "\n\n" + model_block
        config_path.write_text(updated, encoding="utf-8")
    else:
        config_path.write_text(model_block, encoding="utf-8")
    print(f"wrote model block -> {config_path}")

    env_path = HERMES_DIR / ".env"
    lines: list[str] = []
    if env_path.is_file():
        lines = [
            l for l in env_path.read_text(encoding="utf-8").splitlines()
            if not l.startswith("OPENAI_API_KEY=")
        ]
    lines.append(f"OPENAI_API_KEY={settings.api_key}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote OPENAI_API_KEY -> {env_path}")

    print("\nhermes-agent is now wired to DashScope. Smoke-test it with:")
    print('  hermes -z "say the single word: ready" --quiet')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
