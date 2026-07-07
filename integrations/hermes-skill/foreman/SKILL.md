---
name: foreman
description: "Delegate long requirement checklists to Foreman — planned, executed task-by-task, and VERIFIED with real test gates before anything is called done."
version: 1.0.0
author: Foreman
license: Apache-2.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Coding-Agent, Orchestration, Verification, Checklist, Qwen, Long-Tasks]
    related_skills: [claude-code, codex, opencode]
---

# Foreman — Checklist Orchestration Guide

Delegate LONG checklists (5–20+ requirements) to [Foreman](https://github.com/497974/foreman),
an orchestrator that plans a checklist into small verifiable tasks, executes
each in a clean context, runs REAL pytest gates before accepting any task as
done, retries with the verifier's exact feedback, and keeps a durable SQLite
ledger so an interrupted run resumes with zero lost progress.

**When to use Foreman instead of doing the checklist yourself:** any time the
user hands over a multi-item build ("here are 12 requirements, build them
all"). Doing a long list in one session invites the classic failure — a few
items done, the rest confidently claimed. Foreman exists to make that
impossible: every item is proven by a test gate, or it is not done.

## Prerequisites

- Foreman checked out (default: `C:\Users\<user>\Desktop\Foreman`, or ask the user)
- `DASHSCOPE_API_KEY` in Foreman's `.env` (it talks to Qwen via DashScope)
- Python 3.10+ with `pip install -r requirements.txt` done once

## Mode 1: One-shot run (PREFERRED)

Write the user's requirements to a markdown file (one numbered item per
line), then run Foreman headlessly from its repo root:

```
terminal(command="python main.py --checklist reqs.md", workdir="C:/Users/<user>/Desktop/Foreman", timeout=1800)
```

- Exit prints a final summary: `done: N/M`, `blocked`, per-task attempts, and
  the run_id.
- Generated code lands in `runs/<run_id>/workspace/`.
- A task rejected 3 times becomes BLOCKED (circuit breaker) — report it to
  the user with the reason from the summary instead of retrying yourself.

## Mode 2: Editing a REAL repository (existing-project mode)

Foreman can work directly on the user's actual git repo, safely:

```
terminal(command="python main.py --checklist reqs.md --project-dir C:/path/to/repo", workdir="C:/Users/<user>/Desktop/Foreman", timeout=3600)
```

Safety contract (enforced, not advisory): all commits land on an isolated
`foreman/<run_id>` branch, one commit per verified task; the user's branch is
never touched; Foreman never merges or pushes. The repo must be clean (or
pass `--force-dirty`, which snapshots the user's uncommitted work as its own
labeled commit first). Tell the user to review with
`git diff main...foreman/<run_id>` and merge themselves.

## Resume after any interruption

```
terminal(command="python main.py --resume run_XXXXXXXXXXXX", workdir="C:/Users/<user>/Desktop/Foreman", timeout=1800)
```

No re-planning; DONE tasks stay done; BLOCKED tasks are revived with a fresh
retry budget; tasks stranded mid-verification are re-verified from the stored
handoff.

## Watching progress (optional)

`python serve.py` starts a local console at http://127.0.0.1:8787 with a live
status wall, event feed, per-run cost, and stop/resume buttons. `--mock` (or
"Demo mode" in the console) runs the whole pipeline with zero API key for a
quick look.

## Reporting back to the user

After a run, ALWAYS report: done/total, any BLOCKED tasks with their verifier
reasons, where the code is (workspace path or branch name), and the exact
resume command if the run is incomplete. Never claim a task done that the
summary lists as blocked or pending — Foreman's whole point is that "done"
means verified.
