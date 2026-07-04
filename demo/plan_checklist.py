"""Plan a requirements checklist into task cards using the real Qwen planner.

Run:  python demo/plan_checklist.py [path/to/checklist.md]
      (defaults to demo/requirements_mini.md to keep token spend low)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foreman.config import Settings, make_client
from foreman.ledger import Ledger
from foreman.planner import Planner

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> None:
    checklist_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        HERE, "requirements_mini.md"
    )
    requirements = open(checklist_path, encoding="utf-8").read()

    s = Settings.from_env(os.path.join(HERE, "..", ".env"))
    planner = Planner(make_client(s), s.planner_model)
    print(f"planning {os.path.basename(checklist_path)} with {s.planner_model} ...\n")

    tasks = planner.plan(requirements)

    for t in tasks:
        deps = f" <- {', '.join(t.parents)}" if t.parents else ""
        print(f"  {t.task_id} [{t.role}/c{t.complexity_score}] {t.title}{deps}")
        for c in t.acceptance_criteria:
            print(f"       - {c}")
        print(f"       test: {t.test_strategy}")
    print(f"\n{len(tasks)} tasks planned.")

    # Load into a ledger and confirm the graph is schedulable end-to-end.
    led = Ledger()
    led.create_run(requirements)
    led.add_tasks(tasks)
    promoted = led.recompute_ready()
    print(f"loaded into ledger; ready to start: {promoted}")


if __name__ == "__main__":
    main()
