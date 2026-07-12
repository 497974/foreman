"""Single-agent baseline — the SAME thing as the eval harness's Condition A.

One Executor.execute() call on the ENTIRE checklist as a single task, with the
SAME Qwen executor model Foreman uses, a generous max_iters budget, and NO
verifier / NO decomposition / NO retries. This is the honest "here are all the
requirements, go" baseline. Whatever it produces is what it produces.

Usage: python comparison/single_agent.py <checklist.md> <output_dir>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from foreman.config import Settings, make_client
from foreman.executor import Executor
from foreman.models import Task
from foreman.workspace import Workspace


def main(checklist_path: str, out_dir: str):
    requirements = Path(checklist_path).read_text(encoding="utf-8")
    criteria = [
        ln.strip().lstrip("0123456789.)- ").strip()
        for ln in requirements.splitlines()
        if ln.strip() and ln.strip()[0].isdigit()
    ]
    task = Task(
        task_id="A00",
        title="Implement the entire requirements checklist",
        description=requirements,
        acceptance_criteria=criteria or ["implement everything in the checklist"],
        test_strategy="",
        role="generalist",
    )

    settings = Settings.from_env()
    client = make_client(settings)
    workspace = Workspace(Path(out_dir).resolve())
    # Same executor model as Foreman; a big single budget (Foreman spends a
    # comparable total across all its per-task calls). No verifier, no retries.
    executor = Executor(
        client,
        settings.executor_model,
        workspace,
        max_iters=60,
        fallback_models=list(settings.fallback_models),
    )

    print(f"single-agent: model={settings.executor_model}, out={workspace.root}")
    handoff = executor.execute(task, [])
    print(f"single-agent done: outcome={handoff.outcome}")
    print("files_touched:", handoff.files_touched)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: python single_agent.py <checklist.md> <output_dir>")
        raise SystemExit(2)
    main(sys.argv[1], sys.argv[2])
