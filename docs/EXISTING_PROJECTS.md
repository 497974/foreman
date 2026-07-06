# Working on an existing project

Foreman can point its Executor at a real git repository on disk instead of a
fresh sandbox directory — same planner/executor/verifier/dispute loop, same
durable ledger, same `--resume`, but the workspace *is* your repo. This page
covers prerequisites, both entry points (CLI and web console), exactly what
happens on disk, and the escape hatch for a dirty tree.

## Prerequisites

- The target folder must be a **git repository** (`git init` if it isn't one
  yet).
- Point Foreman at the repo **root**, not a subdirectory — a subdirectory
  target is rejected so Foreman never produces a partial-repo commit.
- The tree should be **clean** (`git status --porcelain` empty) before you
  start. Foreman refuses a dirty tree by default — see
  [Force-dirty escape hatch](#force-dirty-escape-hatch-not-recommended) below
  if you really want to proceed anyway.

None of this is enforced client-side for its own sake — it is enforced by
`foreman/git_safety.py` on the machine actually running Foreman, because that
is the only place a check can be trusted.

## CLI usage

```bash
python main.py --checklist reqs.md --project-dir C:\path\to\repo
```

Add `--force-dirty` only if you understand the risk (see below). Both flags
are rejected together with `--mock` and with `--resume` — a resumed
existing-project run re-derives its `project_dir`/branch from the run's own
`project_mode.json` automatically, so you never repeat yourself:

```bash
python main.py --resume run_xxxxxxxxxxxx
```

If the folder fails a safety check, Foreman prints the actionable message
(what to do next, not just what's wrong) and exits 1 — no traceback, no
partial run directory left behind.

## Web console usage

In the **New Run** card, fill in **Existing project folder (optional)** with
the absolute path to your repo, and check **"I understand this will create a
git branch and modify real files in that folder"** — the field is not sent to
the server unless that box is checked. This checkbox is a client-side
courtesy, not the real guard; the real guard is the same server-side
`git_safety.ensure_ready` check the CLI uses. If the folder isn't ready, the
error banner shows the exact message (e.g. *"the repo has uncommitted changes
— commit or stash your changes first..."*) so you know exactly what to fix.

Once the run is under way, the run header shows a `branch: foreman/<run_id>`
chip alongside the model/mock chips so you always know which branch a given
run is writing to.

## What happens

1. Foreman checks out (creating if needed) an isolated branch named
   **`foreman/<run_id>`** — your current branch is left exactly where it was.
2. The Planner is given a short snapshot of your repo (directory tree +
   README/package.json/requirements.txt/etc. previews) so it plans tasks that
   respect your existing structure instead of recreating things that already
   exist. This snapshot is for planning context only — the Executor still
   reads real files at execution time.
3. Every task that passes verification (directly, or after a dispute is
   overturned) gets **its own commit** on the `foreman/<run_id>` branch,
   message `Foreman: <task_id> <task title>`. A task that touches nothing
   real produces no empty commit.
4. Foreman's own bookkeeping (ledger, events, run config) still lives under
   `runs/<run_id>/` as always — it never pollutes your repo. Only the code
   workspace itself is your repo.
5. **Foreman never merges, rebases, or pushes anything.** When the run is
   done (or you want to check progress mid-run), review it yourself:

   ```bash
   git log foreman/run_xxxxxxxxxxxx
   git diff main...foreman/run_xxxxxxxxxxxx
   ```

   Merge the branch, cherry-pick from it, or delete it — that decision, and
   that command, is always yours.

## Force-dirty escape hatch (not recommended)

Passing `--force-dirty` (CLI) or `"force_dirty": true` (API body) lets Foreman
proceed against a repo with uncommitted changes instead of refusing. This
exists for the rare case where you're certain the dirty state is fine to mix
with Foreman's commits — but it means Foreman's first per-task commit will
also capture whatever was already sitting in your working tree, uncommitted
and unreviewed, as part of that commit. **Commit or stash first if you have
any doubt.** There is no equivalent "just trust me" flag for the repo-root or
git-repository checks — those are not overridable, by design.
