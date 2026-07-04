"""The Planner: turns a requirements checklist into a task DAG.

This is where a full day of work is broken into pieces small enough to land
inside an agent's reliable range. Every card it emits carries acceptance
criteria and a *runnable* verification command — because a task with no
objective definition of done is a task the verifier can only guess at.

Design rules enforced in the prompt:
  * one atomic task per card ("one-sentence test": describable without "and");
  * acceptance_criteria are individually checkable statements;
  * test_strategy is a concrete command / assertion, not "make sure it works";
  * dependencies reference earlier task ids only (the graph is a DAG);
  * complexity 1-10; anything a model would rate >=5 should already be split.
"""

from __future__ import annotations

from .llm import chat_json
from .models import Task


def _as_list(value) -> list[str]:
    """Coerce a field to a list of strings — models sometimes return a bare
    string ("T01") or a comma-joined string ("T01, T02") where a list is asked
    for. Normalizing here keeps the DAG validator from walking a string char by
    char."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
        return [p for p in parts if p]
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]

_SYSTEM = """You are the Planner in an autonomous task-execution system, acting \
like a meticulous engineering foreman. You receive a checklist of requirements \
and break it into a dependency-ordered list of small, independently verifiable \
tasks that downstream executor agents will each implement in isolation.

Rules:
- Each task must be ATOMIC: describable in one sentence without the word "and".
  If a requirement bundles multiple deliverables, split it into several tasks.
- Give every task 1-4 acceptance_criteria: concrete, individually checkable
  statements ("returns HTTP 400 when amount is negative"), never vague ("works").
- Give every task a test_strategy: EXACTLY a pytest command of the form
  `python -m pytest <test_file>::<optional_node> -q` that an independent
  verifier can execute OFFLINE in the project directory. `python -c` is
  FORBIDDEN — multi-statement one-liners are a SyntaxError, and shell quoting
  differs per platform. Never "manually verify". Never require a live server
  or network: web apps are exercised through their test client (e.g. Flask's
  app.test_client()), not curl. The task's implementation work includes
  creating that test file with meaningful assertions covering the
  acceptance criteria.
- Set dependencies to the ids of tasks that MUST finish first. Reference only
  tasks that appear earlier in your list. Keep the graph acyclic.
- role is one of: frontend, backend, data, infra, generalist.
- complexity is 1-10 (effort/uncertainty). If you would rate a task >=5, split it.
- Assign ids as T01, T02, ... in dependency order.

Output JSON of exactly this shape:
{"tasks": [{"id": "T01", "title": "...", "description": "...",
  "acceptance_criteria": ["...", "..."], "test_strategy": "...",
  "dependencies": [], "role": "backend", "complexity": 3}]}"""


class Planner:
    def __init__(self, client, model: str):
        self.client = client
        self.model = model

    def plan(self, requirements: str) -> list[Task]:
        data = chat_json(
            self.client,
            self.model,
            system=_SYSTEM,
            user=f"Requirements checklist:\n\n{requirements}",
            max_tokens=8192,
            temperature=0.2,
        )
        raw_tasks = data.get("tasks", [])
        if not raw_tasks:
            raise ValueError("planner returned no tasks")

        tasks: list[Task] = []
        for i, rt in enumerate(raw_tasks):
            tid = rt.get("id") or f"T{i + 1:02d}"
            criteria = _as_list(rt.get("acceptance_criteria"))
            if not criteria:
                # a task with no definition of done is unverifiable — reject early
                raise ValueError(f"task {tid} has no acceptance_criteria")
            tasks.append(
                Task(
                    task_id=tid,
                    title=rt.get("title", tid),
                    description=rt.get("description", ""),
                    acceptance_criteria=criteria,
                    test_strategy=rt.get("test_strategy", ""),
                    role=rt.get("role", "generalist"),
                    complexity_score=int(rt.get("complexity", 1) or 1),
                    parents=_as_list(rt.get("dependencies")),
                )
            )
        _validate_dag(tasks)
        return tasks


def _validate_dag(tasks: list[Task]) -> None:
    """Reject dangling dependencies and cycles before anything is queued."""
    ids = {t.task_id for t in tasks}
    for t in tasks:
        for dep in t.parents:
            if dep not in ids:
                raise ValueError(f"task {t.task_id} depends on unknown task {dep}")

    # cycle check via DFS
    graph = {t.task_id: list(t.parents) for t in tasks}
    WHITE, GREY, BLACK = 0, 1, 2
    color = {tid: WHITE for tid in graph}

    def visit(node: str) -> None:
        color[node] = GREY
        for nxt in graph[node]:
            if color[nxt] == GREY:
                raise ValueError(f"dependency cycle detected at {node} -> {nxt}")
            if color[nxt] == WHITE:
                visit(nxt)
        color[node] = BLACK

    for tid in graph:
        if color[tid] == WHITE:
            visit(tid)
