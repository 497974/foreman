# Foreman × Hermes Agent — two-way integration

[hermes-agent](https://github.com/NousResearch/hermes-agent) (NousResearch,
MIT, 188k★) is a full autonomous agent: persistent memory, self-written
skills, web access, messaging gateways. Foreman is a management layer: plan a
checklist, verify every task with real test gates, keep a durable ledger.
They compose in both directions — Hermes is a great pair of hands, Foreman is
the foreman.

## Direction 1: Hermes as Foreman's executor (`FOREMAN_EXECUTOR_BACKEND=hermes`)

Foreman hands each task card to Hermes' headless mode (`hermes -z … --yolo`)
instead of the native tool-loop. Everything else is unchanged — the verifier
still runs the real pytest gate against the workspace, the ledger still
records every attempt, a rejection still comes back with exact feedback.
Hermes' own web access and skills become available to task execution for
free.

Setup (once):

```powershell
# 1. install hermes-agent (official installer, native Windows supported)
iex (irm https://hermes-agent.nousresearch.com/install.ps1)

# 2. point it at the same DashScope Qwen endpoint Foreman uses
python scripts/setup_hermes.py

# 3. smoke-test
hermes -z "Reply with exactly one word: ready"
```

Run any checklist through it:

```powershell
$env:FOREMAN_EXECUTOR_BACKEND = "hermes"
python main.py --checklist reqs.md
```

Notes (learned against a real 0.18.0 install, not from docs alone):

- Hermes' config lives at `%LOCALAPPDATA%\hermes\` on native Windows
  (`~/.hermes/` on Linux/macOS) — `scripts/setup_hermes.py` probes both.
- The `custom` provider needs `api_key:` set INSIDE the `model:` block of
  `config.yaml`; the `OPENAI_API_KEY` env/.env fallback did not authenticate
  against DashScope in our testing. The setup script handles this.
- `--quiet` is not a valid top-level flag (exit 2); `-z` is already the
  pure single-prompt mode. `--yolo` is required or a headless agent narrates
  instead of acting.
- Model per invocation is passed via `HERMES_INFERENCE_MODEL`, so Foreman's
  per-role model routing still applies.

## Direction 2: Foreman as a Hermes skill

`integrations/hermes-skill/foreman/SKILL.md` follows the agentskills format
Hermes uses (same category as its built-in claude-code/codex skills). Install:

```powershell
# Windows
mkdir "$env:LOCALAPPDATA\hermes\skills\autonomous-ai-agents\foreman" -Force
copy integrations\hermes-skill\foreman\SKILL.md "$env:LOCALAPPDATA\hermes\skills\autonomous-ai-agents\foreman\"
hermes skills list   # should show: foreman | autonomous-ai-agents | local | enabled
```

After that, a Hermes user who says "here are 15 requirements, build them all"
gets a Hermes that KNOWS to delegate the checklist to Foreman — planned,
gated, resumable — and to report done/blocked honestly from the run summary
instead of claiming completion itself.

## Why this matters (the honest version)

Foreman's native executor is deliberately minimal (six tools, pure Python).
A frontier agent like Hermes is far more capable per task — but it has no
verification gate, no dispute/arbitration, no crash-proof ledger, and no
concept of "the 17th item was never actually proven". The integration gives
each side the thing the other is missing. The verifier cannot tell which
backend wrote the code, and that is the point: **a rejection is a rejection,
whoever the author is.**
