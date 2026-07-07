---
name: foreman
description: "Foreman is YOUR (Hermes') multitasking engine. When the user hands you a long list of requirements / many tasks at once, use Foreman to plan them into verifiable pieces, execute each one (as yourself, headless), verify with real tests, retry the failures, and keep a durable ledger — so nothing gets silently dropped."
version: 2.0.0
author: Foreman
license: Apache-2.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Orchestration, Multitask, Checklist, Verification, Long-Tasks, Self-Delegation]
    related_skills: [claude-code, codex, opencode]
---

# Foreman — your multitasking engine

You (Hermes) are the all-capable agent: you operate the computer, edit files,
run commands, remember, search the web. **Foreman is not another agent — it is
a feature you gained.** It exists for exactly one situation you are otherwise
bad at: when the user gives you a LONG list ("here are 15 requirements, build
them all" / "do these 20 things to my project"), a single pass tends to finish
a few and quietly claim the rest are done. Foreman makes that impossible: it
splits the list into small tasks, runs each one **through you, headless**,
refuses to accept any task until its real test passes, retries the failures
with concrete feedback, and records everything in a ledger that survives
crashes.

So the division of labor is: **you are the hands; Foreman is the checklist
foreman standing over your shoulder.** Every task is still executed by a fresh
headless *you* — Foreman just decides what to run next, checks the result, and
never loses the thread.

## When to use this

- The user gives you **more than a few tasks at once**, or a numbered
  requirements list, or a whole project's worth of work.
- The work needs to be **provably complete** (each item verified), not just
  attempted.
- A job is long enough that you'd want a **resumable ledger** if it's
  interrupted.

For a single quick action ("change my wallpaper", "rename these files") just
do it yourself — Foreman's overhead isn't worth it.

## How to run it

Foreman lives at `C:\Users\<user>\Desktop\Foreman`. It runs on the free
Google Gemini tier (its `.env` is already set to `FOREMAN_PROVIDER=gemini`),
and it is configured to execute each task **through you** by setting the
hermes backend. So a long checklist becomes:

```
terminal(
  command="set FOREMAN_EXECUTOR_BACKEND=hermes && python main.py --checklist reqs.md",
  workdir="C:/Users/<user>/Desktop/Foreman",
  timeout=3600
)
```

- Write the user's requirements to `reqs.md` first (one item per line).
- `FOREMAN_EXECUTOR_BACKEND=hermes` is what routes each task's execution back
  to a headless `hermes -z` call — i.e. to you. (Omit it and Foreman uses its
  own lightweight built-in executor instead, which is fine for pure coding
  tasks and uses less quota.)
- To work on the user's REAL project, add `--project-dir C:/path/to/repo`
  (Foreman commits each verified task on an isolated `foreman/<run_id>`
  branch and never touches their main branch).
- To operate the machine broadly (any folder, any command, no git), add
  `--computer-mode` (optionally `--work-dir C:/some/folder`).

## Reading the result

The run prints a summary: `done: N/M`, any `blocked` tasks, per-task attempts,
and a `run_id`. Report it to the user honestly:

- Tell them **done/total** and name any **blocked** task with the reason the
  verifier gave (do NOT claim a blocked or pending task is done — Foreman's
  whole point is that "done" means a test actually passed).
- Say **where the output is**: `runs/<run_id>/workspace/` for a fresh build,
  or the `foreman/<run_id>` git branch for an existing project.
- If the run was interrupted, resume it with
  `python main.py --resume <run_id>` — no re-planning, finished tasks stay
  finished.

## Watching it live (optional)

`python serve.py` opens a console at http://127.0.0.1:8787 with a status wall
and live event feed, so the user can watch tasks go grey → amber → green.
