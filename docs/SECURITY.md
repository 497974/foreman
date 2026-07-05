# Foreman — Security posture

This is the honest threat model, not a marketing page. Foreman runs an LLM
in a tool-calling loop with `read_file`, `write_file`, `list_dir`, and
`run_command` — the last one hands the model a real shell (`shell=True`,
per `docs/CONTRACTS.md`). Everything below is about what stands between
that shell and the rest of the machine, and — more importantly — what
doesn't.

## What IS protected

- **File-op jail** (`Workspace._resolve`, `foreman/workspace.py`).
  `read_file`, `write_file`, and `list_dir` all resolve their path against
  `self.root` and reject anything that would escape it — `..` climbing,
  an absolute path elsewhere on disk, or a symlink pointing out. This
  covers hallucinated paths reliably; see `docs/ARCHITECTURE.md` for why
  it's the one place model output touches the real filesystem.

- **Command policy** (`CommandPolicy`, `foreman/workspace.py`). Before
  `run()` ever calls `subprocess.run`, the command string is checked
  against a DENY list of catastrophic, mostly absolute-path patterns:
  `rm -rf` / `del /s /q` / `rd /s` on absolute paths, `format`, `mkfs`,
  `shutdown`/`reboot`, `reg add|delete` on `HKLM`, `diskpart`, `cipher /w`,
  redirecting into `C:\Windows`, and PowerShell `Remove-Item -Recurse` on
  absolute paths. It is a DENY list, not an ALLOW list — it targets the
  *theme* (whole-disk destruction, system state changes, absolute-path
  writes/deletes outside the workspace), not every dangerous flag
  combination. Ordinary relative commands (`pytest`, `python`, `pip
  install`, `npm ...`) all pass through untouched. Disable with
  `FOREMAN_COMMAND_POLICY=off` — documented here as dangerous, not
  hidden; the default is on.

- **Per-command timeout** (`Workspace.run(..., timeout=...)`, wired from
  `Executor(command_timeout=...)`). A runaway process is killed and
  reported back as `timed_out=True`, not left to hang the run forever.

- **Output truncation** (`MAX_OUTPUT`, `foreman/workspace.py`). Command
  stdout/stderr are capped so one chatty `pip install` or an infinite test
  loop can't blow the executor's context window on the next turn.

## What is NOT protected

- **A determined, prompt-injected model can still do damage through
  allowed interpreters.** The command policy blocks *commands that name
  their own destruction* — it cannot see what a `python -c "..."` payload
  or a `pip install`-ed package actually does at runtime. A model that
  writes a Python script which then deletes files, exfiltrates data, or
  calls out to the network is not stopped by anything in this repo. The
  jail protects *paths Foreman's own tools resolve*; it does not sandbox
  arbitrary code the shell goes on to execute.

- **This is not a container, a VM, or a seccomp profile.** There is no
  process isolation, no filesystem overlay, no network policy. `run()`
  executes with the same OS-level privileges as whatever ran Foreman.

- **The DENY list is necessarily incomplete.** Regexes match known-bad
  shapes; a sufficiently creative or obfuscated command (encoded strings,
  unusual quoting, an alias, a script that constructs the dangerous
  command at runtime instead of typing it literally) can slip past. Treat
  the policy as raising the bar against *accidental* self-destruction
  (the realistic failure mode — a hallucinated absolute path, a copy-
  pasted `rm -rf /` from a tutorial) far more than as a defense against
  an actively adversarial model.

- **Recommendation for hostile input:** if you are pointing Foreman at
  requirements you do not trust, or letting a model choose its own
  commands with no human in the loop, run it inside a container (or a
  disposable VM) with no credentials and no network access beyond what
  the task needs. Foreman's own guards are not a substitute for that
  isolation — they are what a **local dev tool** should have by default,
  not what a hostile-input system requires.

## Design principle: policy failures are tool results, not crashes

A blocked command does not raise an exception. `Workspace.run` returns an
ordinary `CommandResult` (`exit_code=126`, `stderr="blocked by Foreman
command policy: <pattern name>"`) exactly the way a real nonzero exit
would surface. The executor loop keeps going and the model sees the
refusal as a tool result it can react to — try a narrower command, ask a
different way, or give up and report a gotcha — the same way it reacts to
a failing test or a jail violation on a file op (`docs/CONTRACTS.md` §1,
§2). Crashing the loop on a policy hit would be strictly worse: it would
turn a safety guard into an outage, and it would deny the model the one
thing this whole architecture is built to give it — a clean, informative
signal it can act on next turn.
