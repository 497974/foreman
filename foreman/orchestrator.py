"""The Orchestrator: wires ledger + dispatcher + planner + executor + verifier
into the single loop that turns a requirements checklist into a done project.

Nothing here makes a judgement call itself — planning, executing, and
verifying are all delegated to their own modules. This module only owns
sequencing (claim -> execute -> submit -> verify -> record) and the run's
durable artifacts: the ledger DB, the workspace directory, and an append-only
events.jsonl that is the future UI/SSE data source.

Parent handoffs are reconstructed from the ledger's attempt history rather
than kept in memory, on purpose: it is the same durability property the
ledger gives everything else. A task's executor never sees anything except
what a fresh read of the ledger would show a brand new process.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from . import git_safety, llm, repo_context
from .arbiter import Arbiter, solicit_dispute
from .backends import make_executor
from .config import Settings, make_client
from .dispatcher import Dispatcher
from .executor import Executor
from .ledger import Ledger
from .models import AttemptOutcome, Handoff, Task, TaskStatus
from .planner import Planner
from .telemetry import set_current_run
from .verifier import Verifier
from .workspace import Workspace

# ASCII status wall (see demo/smoke_run.py) — portable across Windows consoles
# and cloud log capture.
WALL = {
    TaskStatus.DONE: "[#]", TaskStatus.IN_PROGRESS: "[>]",
    TaskStatus.PENDING_REVIEW: "[?]", TaskStatus.BLOCKED: "[X]",
    TaskStatus.READY: "[ ]", TaskStatus.PENDING: "[.]", TaskStatus.ARCHIVED: "[#]",
}

MAX_CLAIMS_SAFETY_CAP = 50  # global stop condition regardless of ledger state


def status_wall(led: Ledger) -> str:
    return " ".join(WALL[t.status] for t in led.all_tasks())


def _feedback_reason(report) -> str:
    """Build the string ledger.record_verdict stores into last_error.

    The executor only ever sees ``task.last_error`` on its next attempt, so
    this is the one chance the verifier's actionable feedback has to reach
    the retry. We fold in the verifier's own one-liner plus the first few
    actionable_feedback items (already file/expected-vs-actual flavoured per
    contract §3).
    """
    parts = [report.reason]
    extra = [f for f in report.actionable_feedback if f and f not in report.reason]
    if extra:
        parts.append("; ".join(extra[:3]))
    return " | ".join(p for p in parts if p)


def _reconstruct_handoff(task_id: str, attempts: list[dict]) -> Optional[Handoff]:
    """Rebuild the most recent *successful* Handoff for one parent task.

    The ledger stores each attempt's handoff as JSON in the ``summary``
    column (see Ledger._record_attempt). We want the handoff belonging to the
    attempt that actually got the task to DONE — i.e. the most recent attempt
    with outcome == success — not just the latest attempt row (a task can
    have later rejected attempts only if it was revived after DONE, which
    does not happen in this system, but we still pick success-most-recent to
    be defensive).
    """
    mine = [a for a in attempts if a["task_id"] == task_id]
    if not mine:
        return None
    # attempts are ordered by started_at ascending (Ledger.attempt_history);
    # walk backwards for the most recent success.
    for row in reversed(mine):
        if row["outcome"] == AttemptOutcome.SUCCESS.value and row["summary"]:
            data = json.loads(row["summary"])
            return Handoff(**data)
    # No successful attempt recorded (should not happen for a DONE parent,
    # but fail soft rather than crash the run).
    row = mine[-1]
    if row["summary"]:
        return Handoff(**json.loads(row["summary"]))
    return None


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        run_root: str = "runs",
        project_dir: str | Path | None = None,
        force_dirty: bool = False,
        computer_mode: bool = False,
        work_dir: str | Path | None = None,
    ):
        self.settings = settings
        self.client = make_client(settings)
        # Computer mode (电脑操控模式): the user explicitly grants Foreman full
        # reign over their own machine — the Workspace is pointed at a real
        # folder they choose (default: home), the command deny-list is OFF
        # (allow_all), and there is NO git requirement or branch isolation.
        # This is the "operate my computer, edit any file, run any command"
        # capability, opt-in and off by default. It is deliberately separate
        # from existing-project mode (which keeps every git guardrail) —
        # nobody should get an unguarded shell on their real repo by accident.
        self.computer_mode = bool(computer_mode)

        self.run_root = Path(run_root)
        self.run_root.mkdir(parents=True, exist_ok=True)

        # The run_id used for the filesystem layout (runs/<run_id>/...) is
        # minted directly rather than via Ledger.create_run, because the
        # ledger's db file itself must live under that same run_dir — i.e.
        # the directory name has to be decided before the Ledger exists.
        # ledger.create_run() is still called later (in run_checklist) to
        # populate the `runs` table inside that db with the requirements
        # text; its own internally-minted run_id is only used as that row's
        # primary key and is not exposed here.
        from .models import new_id
        self.run_id = new_id("run")

        run_dir = self.run_root / self.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        self.run_dir = run_dir

        self.ledger = Ledger(db_path=str(run_dir / "ledger.db"))
        self.dispatcher = Dispatcher(self.ledger)
        # Set before any possible self._emit() call below (project_mode event
        # can fire during __init__ itself, in existing-project mode).
        self.events_path = run_dir / "events.jsonl"

        # Existing-project mode (contract Addendum 4 §14): point the Workspace
        # at the user's real repo instead of runs/<run_id>/workspace. Ledger
        # db / events.jsonl / config.json still live under run_dir as always —
        # only the code workspace itself moves, so Foreman's own bookkeeping
        # never pollutes the user's repo.
        self.project_dir: Optional[Path] = None
        self.project_branch: Optional[str] = None
        repo_snapshot = ""
        if project_dir is not None:
            resolved_project = Path(project_dir).resolve()

            # Foreman's own run artifacts (the SQLite ledger, events.jsonl,
            # workspace bookkeeping) live under run_dir. If run_dir happens to
            # sit INSIDE the user's repo — e.g. they ran with the default
            # --run-root=runs while their cwd was the repo — then commit_all's
            # `git add -A` would sweep Foreman's internal files (including a
            # binary .db) into the user's checkpoint commits. Refuse that
            # outright with an actionable message rather than silently polluting
            # their history.
            #
            # This check runs BEFORE ensure_ready on purpose: creating run_dir
            # a few lines above already wrote files under the repo, so the tree
            # is now dirty. If we let ensure_ready run first it would fail with
            # the misleading "repo has uncommitted changes" (a symptom we
            # caused), burying the real cause. Naming the actual problem —
            # run_dir location — is the actionable message.
            try:
                self.run_dir.resolve().relative_to(resolved_project)
                inside = True
            except ValueError:
                inside = False
            if inside:
                raise git_safety.GitSafetyError(
                    f"Foreman's run directory ({self.run_dir}) is inside the "
                    f"target project ({resolved_project}); that would commit "
                    "Foreman's own files into your repo. Re-run with a "
                    "--run-root outside the project (or from a different "
                    "working directory)."
                )

            # Raises GitSafetyError with an actionable message; let it
            # propagate — the CLI/API layer turns it into a clean user-facing
            # error rather than a traceback.
            git_safety.ensure_ready(project_dir, force_dirty)

            self.project_dir = resolved_project

            # Serialize runs against this repo: two concurrent runs would race
            # checkout/commit on one shared working tree (released in
            # run_checklist/resume_run's finally).
            git_safety.acquire_lock(self.project_dir, self.run_id)

            # Snapshot the dirty flag BEFORE the checkout: `checkout -b`
            # carries uncommitted changes onto the new branch, and we want the
            # user's pre-existing work committed SEPARATELY from anything a
            # task later writes — one clearly-labeled commit at the branch
            # tip, so `git log`/`git blame` never attributes the user's own
            # code to an AI-authored task checkpoint.
            had_dirty_tree = force_dirty and not git_safety.is_clean(self.project_dir)

            self.project_branch = f"foreman/{self.run_id}"
            git_safety.create_or_checkout_branch(self.project_dir, self.project_branch)

            if had_dirty_tree:
                snapshotted = git_safety.commit_all(
                    self.project_dir,
                    "Foreman: snapshot of pre-existing uncommitted changes "
                    "(present before this run started; not authored by Foreman)",
                    expected_branch=self.project_branch,
                )
                if snapshotted:
                    self._emit("preexisting_snapshot", detail={"branch": self.project_branch})

            self.workspace = Workspace(self.project_dir)

            (run_dir / "project_mode.json").write_text(
                json.dumps(
                    {"project_dir": str(self.project_dir), "branch": self.project_branch},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self._emit(
                "project_mode",
                detail={"project_dir": str(self.project_dir), "branch": self.project_branch},
            )

            repo_snapshot = repo_context.build_repo_snapshot(self.project_dir)
        elif self.computer_mode:
            # Full-machine mode: root the Workspace at a real folder the user
            # named (default their home directory) with the command policy
            # OFF. run_command can now do anything the user's own account can
            # — set the wallpaper, edit any file under work_dir, invoke system
            # tools. No git, no jail beyond the chosen root.
            root = Path(work_dir).expanduser().resolve() if work_dir else Path.home()
            root.mkdir(parents=True, exist_ok=True)
            self.work_dir = root
            self.workspace = Workspace(root, allow_all=True)
            (run_dir / "computer_mode.json").write_text(
                json.dumps({"work_dir": str(root)}, ensure_ascii=False),
                encoding="utf-8",
            )
            self._emit("computer_mode", detail={"work_dir": str(root)})
        else:
            self.workspace = Workspace(run_dir / "workspace")

        self.planner = Planner(self.client, settings.planner_model)
        self._repo_context = repo_snapshot
        if self.project_dir is not None:
            # Contract §14 step 7: existing-project runs use the native,
            # hand-written Executor with existing_project=True (not
            # make_executor's pluggable backend selection) so the extra
            # system-prompt line is guaranteed regardless of
            # FOREMAN_EXECUTOR_BACKEND.
            self.executor = Executor(
                self.client, settings.executor_model, self.workspace, existing_project=True
            )
        else:
            # Computer mode still honors FOREMAN_EXECUTOR_BACKEND (so a power
            # user can drive it with the full hermes-agent), but passes
            # computer_mode through so the native executor's system prompt tells
            # it it's operating a real machine, not a sandbox.
            self.executor = make_executor(
                settings, self.workspace, self.client, computer_mode=self.computer_mode
            )
        self.verifier = Verifier(self.client, settings.verifier_model, self.workspace)
        # Arbiter uses the planner-tier model (qwen-max) on purpose: it is
        # meant to out-rank both the executor that disputes and the verifier
        # being disputed against (contract §6).
        self.arbiter = Arbiter(self.client, settings.planner_model, self.workspace)

        # One dispute per task per run (contract §6). Keyed by task_id, not
        # attempt number, so a task cannot re-litigate a later rejection
        # either — the appeal is a single use, not one per attempt.
        self.disputed_task_ids: set[str] = set()

        # Contract §12: quota-exhaustion should degrade gracefully instead of
        # killing the run. Wire the fallback chain into the executor and
        # register the module-level hook so a substitution shows up in
        # events.jsonl (and therefore the console/README) instead of silently
        # vanishing into stdout.
        self.executor.fallback_models = list(settings.fallback_models)
        llm.on_model_fallback = self._on_model_fallback
        llm.on_rate_limit_wait = self._on_rate_limit_wait

    def _on_model_fallback(self, original: str, used: str) -> None:
        self._emit("model_fallback", detail={"from": original, "to": used})

    def _on_rate_limit_wait(self, model: str, delay_s: float) -> None:
        self._emit("rate_limit_wait", detail={"model": model, "delay_s": round(delay_s, 1)})

    # ---- events ---------------------------------------------------------

    def _emit(self, event_type: str, task_id: str = "", detail: Optional[dict] = None) -> None:
        from .models import now_ts

        line = {
            "ts": now_ts(),
            "type": event_type,
            "task_id": task_id,
            "detail": detail or {},
        }
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    # ---- parent handoff reconstruction -----------------------------------

    def _parent_handoffs(self, task: Task) -> list[Handoff]:
        handoffs: list[Handoff] = []
        for parent_id in task.parents:
            attempts = self.ledger.attempt_history(parent_id)
            h = _reconstruct_handoff(parent_id, attempts)
            if h is not None:
                handoffs.append(h)
        return handoffs

    # ---- dispute + arbitration (contract §6) ------------------------------

    def _dispute_eligible(self, task: Task, report) -> bool:
        """A REJECT is disputable only when every objective gate was green.

        Gate failures are machine-checked ground truth; rhetoric cannot argue
        with an exit code, so those rejections skip the negotiation layer
        entirely. Criteria-only rejections are LLM judgement, which is the
        one thing worth a second opinion. Also enforces the one-appeal-per-
        task-per-run rule via ``self.disputed_task_ids``.
        """
        if report.passed:
            return False
        if task.task_id in self.disputed_task_ids:
            return False
        gate = report.objective_gate or {}
        gate_green = gate.get("passed", True)
        return bool(gate_green)

    def _run_dispute_flow(self, task: Task, handoff: Handoff, report) -> tuple[bool, str]:
        """Run the executor-dispute + arbiter-ruling flow for one rejection.

        Returns (passed, reason) to feed straight into ledger.record_verdict.
        Marks the task as having used its one dispute regardless of outcome
        (concede included is NOT marked here — conceding does not spend the
        appeal, only an actual dispute does, per contract: "ONE dispute per
        task per run" refers to an actual dispute being raised).
        """
        dispute_data = solicit_dispute(self.client, self.settings.executor_model, task, handoff, report)
        if not dispute_data["dispute"]:
            # Concede: proceed to record_verdict(passed=False) unchanged.
            return False, _feedback_reason(report)

        self.disputed_task_ids.add(task.task_id)
        self._emit(
            "dispute",
            task_id=task.task_id,
            detail={
                "rebuttal": dispute_data["rebuttal"][:500],
                "evidence_files": [str(e.get("file", "")) for e in dispute_data["evidence"]],
            },
        )

        ruling = self.arbiter.rule(
            task, handoff, report, dispute_data["rebuttal"], dispute_data["evidence"]
        )
        self._emit(
            "arbitration",
            task_id=task.task_id,
            detail={
                "ruling": ruling["ruling"],
                "reasoning": ruling["reasoning"][:500],
            },
        )

        if ruling["ruling"] == "overturn":
            return True, f"arbiter overturned: {ruling['reasoning']}"

        # Uphold: fold the arbiter's clarification into the reason so it
        # reaches the executor's next attempt via task.last_error.
        base_reason = _feedback_reason(report)
        clarification = ruling["criteria_clarification"]
        reason = base_reason
        if clarification:
            reason = f"{base_reason} | arbiter upheld: {clarification}"
        return False, reason

    # ---- the loop ---------------------------------------------------------

    def run_checklist(self, requirements: str) -> dict:
        """Tag this worker thread with the run_id for the whole call (contract
        §9.1): every chat_json/executor METER.record() call made on this
        thread while planning + driving the loop gets attributed to this run,
        so the console can show a live per-run cost readout. Cleared in a
        finally so a thread pool reusing this thread never leaks the tag into
        unrelated work.
        """
        set_current_run(self.run_id)
        try:
            # getattr-guarded: fake-planner test/mocks construct Orchestrator
            # via __new__ and never set _repo_context; FakePlanner.plan also
            # does not accept a repo_context kwarg, so skip passing it when
            # the attribute doesn't exist (greenfield/mock behavior unchanged).
            repo_context_str = getattr(self, "_repo_context", "")
            if repo_context_str:
                tasks = self.planner.plan(requirements, repo_context=repo_context_str)
            else:
                tasks = self.planner.plan(requirements)
            self._emit("plan", detail={"n_tasks": len(tasks), "task_ids": [t.task_id for t in tasks]})

            return self.run_tasks(requirements, tasks, _tag_thread=False)
        finally:
            set_current_run(None)

    def run_tasks(self, requirements: str, tasks: list[Task], _tag_thread: bool = True) -> dict:
        """Queue an already-planned task list and drive it, skipping the planner.

        Identical tail to ``run_checklist`` — the only difference is where the
        tasks came from. This is what the evaluation harness's Condition C
        (full Foreman) uses: the exam is planned exactly once, up front, by
        the real Planner, and then fed here so every condition in the
        three-way comparison is judged against the same frozen task list
        rather than three different plans. ``requirements`` is recorded on
        the run (create_run) even though it is not re-planned here, so the
        ledger still carries the original checklist text for reference.

        This overwrites the bootstrap run_id row created in __init__ with the
        real requirements text (create_run mints its own id — the ledger's
        `runs` table id does not have to match self.run_id used for the
        filesystem layout; only the run_dir naming needs to be decided before
        the ledger exists).

        ``_tag_thread`` is internal: when called directly (Condition C of the
        eval harness, or any caller with an already-planned list) this method
        owns the thread-local run tag itself (contract §9.1); when called
        from ``run_checklist`` the tag is already set by the caller, so it is
        left alone here to avoid clearing it prematurely inside a shared
        try/finally.
        """
        if _tag_thread:
            set_current_run(self.run_id)
        try:
            self.ledger.create_run(requirements)
            self.ledger.add_tasks(tasks)
            return self._drive_loop()
        finally:
            # getattr-guarded like _repo_context: __new__-built test/resume
            # orchestrators may not have run the project_dir wiring at all.
            if getattr(self, "project_dir", None) is not None:
                git_safety.release_lock(self.project_dir, self.run_id)
            if _tag_thread:
                set_current_run(None)

    def resume_run(self, run_id: str) -> dict:
        """Continue an existing run without re-planning (contract §7).

        The plan already lives in the ledger (this same run_id's tasks table),
        so there is nothing to ask the planner for. The only state surgery
        needed is reviving BLOCKED tasks — everything else (READY, PENDING,
        DONE, etc.) is already exactly where a fresh process would find it,
        which is the whole point of a durable ledger: resuming looks just
        like the loop continuing after a hiccup, not a special code path.

        ``self.run_id``/``self.run_dir`` are expected to already point at the
        existing run's directory (the caller — main.py's --resume — must
        construct the Orchestrator against that run_id's ledger/workspace
        before calling this; see main.py for the wiring).

        Existing-project mode (contract Addendum 4 §14): BEFORE the Workspace
        the caller built is used for anything, check for
        ``run_dir/project_mode.json``. If present, self-derive project_dir/
        branch from it, repoint ``self.workspace`` at the real repo, and
        re-run create_or_checkout_branch (idempotent) — this is what lets
        ``--resume`` work for an existing-project run without the caller
        re-specifying --project-dir.

        Deletes the STOP sentinel (contract §9.3) if present before entering
        the loop — a resumed run should not immediately observe a stale stop
        request left over from whichever earlier process wrote it.
        """
        set_current_run(self.run_id)
        try:
            project_mode_path = self.run_dir / "project_mode.json"
            if project_mode_path.is_file():
                data = json.loads(project_mode_path.read_text(encoding="utf-8"))
                self.project_dir = Path(data["project_dir"])
                self.project_branch = data["branch"]
                # Re-assert the safety invariants, not just the plumbing: the
                # repo may have changed hands between the first pass and this
                # resume. A merge/rebase the user started in the meantime is
                # as unsafe now as it would have been at first-run time.
                # (Cleanliness is deliberately NOT re-checked: a crash mid-task
                # legitimately leaves Foreman's own uncommitted work behind,
                # and that in-flight state must survive the resume.)
                operation = git_safety.in_progress_operation(self.project_dir)
                if operation is not None:
                    raise git_safety.GitSafetyError(
                        f"cannot resume: the repo has a {operation} in progress "
                        f"— finish or abort it first (git {operation} --abort)."
                    )
                git_safety.acquire_lock(self.project_dir, self.run_id)
                # allow_existing: the branch normally survives from the first
                # pass (checked out); if the user deleted it, it is recreated.
                git_safety.create_or_checkout_branch(
                    self.project_dir, self.project_branch, allow_existing=True
                )
                self.workspace = Workspace(self.project_dir)
                self.executor.workspace = self.workspace
                self.verifier.workspace = self.workspace
                self.arbiter.workspace = self.workspace

            stop_path = self.run_dir / "STOP"
            if stop_path.exists():
                stop_path.unlink()

            # Heal tasks stranded mid-verification (found live, not in a
            # unit test: a network error killed the VERIFIER's LLM call, i.e.
            # the process died between submit_for_review and record_verdict).
            # Such a task sits in PENDING_REVIEW with no claim to expire and
            # nothing for the drive loop to pick up — the resumed run would
            # stall forever with 0 claims. The executor's work is NOT lost:
            # submit_for_review durably recorded the handoff in the attempts
            # table. So finish the interrupted step — re-verify the stored
            # handoff through the exact same verify/dispute/commit/verdict
            # path an uninterrupted run would have used.
            for task in self.ledger.tasks_by_status(TaskStatus.PENDING_REVIEW):
                handoff = _reconstruct_handoff(
                    task.task_id, self.ledger.attempt_history(task.task_id)
                )
                self._emit(
                    "reverify",
                    task_id=task.task_id,
                    detail={"run_id": run_id, "handoff_found": handoff is not None},
                )
                if handoff is None:
                    # No stored handoff to grade (should be impossible —
                    # submit always records one). Fail the attempt honestly
                    # rather than inventing evidence to score.
                    self.ledger.record_verdict(
                        task.task_id,
                        passed=False,
                        reason="resume: task was stranded in review with no stored handoff",
                    )
                    continue
                self._verify_and_record(task, handoff)

            blocked = self.ledger.tasks_by_status(TaskStatus.BLOCKED)
            for task in blocked:
                self.ledger.revive_blocked(task.task_id, reset_attempts=True)
                self._emit("revive", task_id=task.task_id, detail={"run_id": run_id})

            return self._drive_loop()
        finally:
            if getattr(self, "project_dir", None) is not None:
                git_safety.release_lock(self.project_dir, self.run_id)
            set_current_run(None)

    def _verify_and_record(self, task: Task, handoff: Handoff) -> bool:
        """Verify one submitted handoff and finalize its verdict — the single
        path from "executor submitted" to "ledger has a verdict".

        Shared by the drive loop (normal flow) and resume_run's stranded-task
        healing (a crash between submit_for_review and record_verdict leaves a
        task in PENDING_REVIEW; on resume this same method finishes the
        interrupted step). Includes the dispute/arbitration flow and the
        existing-project checkpoint commit, so a healed task gets exactly the
        treatment an uninterrupted one would have gotten.
        """
        report = self.verifier.verify(task, handoff)

        if self._dispute_eligible(task, report):
            passed, reason = self._run_dispute_flow(task, handoff, report)
        else:
            passed, reason = report.passed, _feedback_reason(report)

        # Existing-project mode (contract Addendum 4 §14): this is the
        # single point both the plain-verify-pass path and the dispute/
        # arbitration-overturn path funnel through before record_verdict
        # — the one true "this task is finalized as a pass" moment. One
        # commit per task, only on a real pass, never an empty commit
        # (git_safety.commit_all no-ops if nothing changed).
        # getattr-guarded: other test suites (tests/test_orchestrator.py,
        # foreman/mocks.py) build an Orchestrator via __new__ and never
        # set project_dir at all — those must see a plain no-op here,
        # exactly as if project_dir had defaulted to None.
        if getattr(self, "project_dir", None) is not None and passed:
            # The checkpoint commit is an audit-trail guarantee, not the
            # correctness mechanism — the task already passed real
            # verification. A commit failure here (e.g. a host-level git
            # problem) must be surfaced loudly, but must never unwind an
            # already-earned DONE: that would make the code disappear from
            # the ledger's view while it still sits, unrecorded, on disk.
            try:
                git_safety.commit_all(
                    self.project_dir,
                    f"Foreman: {task.task_id} {task.title}",
                    expected_branch=self.project_branch,
                )
            except git_safety.GitSafetyError as exc:
                self._emit(
                    "checkpoint_failed",
                    task_id=task.task_id,
                    detail={"message": str(exc)},
                )

        new_status = self.ledger.record_verdict(task.task_id, passed=passed, reason=reason)
        self._emit(
            "verdict",
            task_id=task.task_id,
            detail={
                "passed": passed,
                "new_status": new_status.value,
                "reason": reason,
                "coverage_rate": report.coverage_rate,
            },
        )

        print(f"  {status_wall(self.ledger)}   {task.task_id} "
              f"{'PASS' if passed else f'REJECT ({new_status.value})'}")
        return passed

    def _drive_loop(self) -> dict:
        """The claim -> execute -> submit -> verify -> (dispute) -> record
        loop, shared verbatim by a fresh run and a resumed one. Nothing here
        is aware of whether tasks were just planned or already existed in the
        ledger from a prior process — that is the entire point of routing
        every decision through the ledger rather than in-memory state.

        Stop sentinel (contract §9.3): a file at ``runs/<run_id>/STOP`` is
        checked at the TOP of every iteration, before claiming the next task.
        This gives task-boundary granularity, not instant interruption — an
        in-flight executor attempt (already claimed before the sentinel
        appeared) always finishes its current claim -> execute -> verify ->
        record cycle first; the loop only stops before starting the NEXT one.
        When the sentinel is found, a "stopped" event is emitted and the
        summary carries ``"stopped": True`` so callers (the console) can
        distinguish a deliberate stop from natural completion/stall.
        """
        import time

        start = time.monotonic()

        claims = 0
        worker_id = "orchestrator-worker-1"
        stop_path = self.run_dir / "STOP"
        stopped = False

        while True:
            if stop_path.exists():
                stopped = True
                self._emit("stopped", detail={"claims": claims})
                break

            tick = self.dispatcher.tick()

            if tick.reclaimed:
                for tid in tick.reclaimed:
                    self._emit("reclaim", task_id=tid)
            if tick.promoted:
                for tid in tick.promoted:
                    self._emit("promote", task_id=tid)

            if tick.complete:
                break
            if tick.stalled:
                break
            if claims >= MAX_CLAIMS_SAFETY_CAP:
                break

            task = self.ledger.claim_next(worker_id, lease_seconds=900.0)
            if task is None:
                # Nothing ready right now (e.g. everything in_progress/blocked
                # with no promotions possible) — this is effectively a stall
                # the dispatcher hasn't flagged yet; stop rather than spin.
                break

            claims += 1
            self._emit("claim", task_id=task.task_id, detail={"attempt": task.attempt_count})

            dep_handoffs = self._parent_handoffs(task)
            handoff = self.executor.execute(task, dep_handoffs)

            self.ledger.submit_for_review(task.task_id, worker_id, handoff)
            self._emit("submit", task_id=task.task_id, detail={"outcome": handoff.outcome})

            self._verify_and_record(task, handoff)

        elapsed = time.monotonic() - start
        counts = self.ledger.counts()
        all_tasks = self.ledger.all_tasks()
        attempts_per_task = {t.task_id: t.attempt_count for t in all_tasks}

        return {
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "counts": counts,
            "done": counts.get(TaskStatus.DONE.value, 0),
            "blocked": counts.get(TaskStatus.BLOCKED.value, 0),
            "total_tasks": len(all_tasks),
            "attempts_per_task": attempts_per_task,
            "claims": claims,
            "elapsed_s": elapsed,
            "complete": self.ledger.is_run_complete(),
            "stopped": stopped,
        }
