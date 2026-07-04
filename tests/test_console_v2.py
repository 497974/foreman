"""Product Console v2 backend tests (contract Addendum 2, §9). Run: pytest -q
(no API key, no network, no HTTP server — serve.py's route handlers are thin
wiring over the same importable functions exercised here).
"""

import json
import os
import sys
import threading
import time
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foreman import webui_data
from foreman.ledger import Ledger
from foreman.models import Handoff, Task
from foreman.pricing import PRICES, estimate_usd
from foreman.telemetry import METER, get_current_run, set_current_run


# ---- 9.1 telemetry: thread-local run tagging -------------------------------


def test_telemetry_two_thread_run_tagging_isolation():
    METER.reset()

    results = {}

    def worker(run_id, model, prompt_tok, completion_tok, barrier):
        set_current_run(run_id)
        barrier.wait()  # maximize interleaving between the two threads
        METER.record(model, prompt_tok, completion_tok)
        results[run_id] = get_current_run()
        set_current_run(None)

    barrier = threading.Barrier(2)
    t1 = threading.Thread(target=worker, args=("run_a", "qwen-max", 100, 50, barrier))
    t2 = threading.Thread(target=worker, args=("run_b", "qwen-plus", 10, 5, barrier))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert results["run_a"] == "run_a"
    assert results["run_b"] == "run_b"

    totals_a = METER.run_totals("run_a")
    totals_b = METER.run_totals("run_b")
    assert totals_a["totals"] == {"prompt_tokens": 100, "completion_tokens": 50, "calls": 1}
    assert totals_b["totals"] == {"prompt_tokens": 10, "completion_tokens": 5, "calls": 1}
    assert totals_a["per_model"]["qwen-max"]["prompt_tokens"] == 100
    assert totals_b["per_model"]["qwen-plus"]["prompt_tokens"] == 10

    # main test thread was never tagged
    assert get_current_run() is None

    # untagged recording does not pollute either run's bucket
    METER.record("qwen-turbo", 1, 1)
    assert "qwen-turbo" not in METER.run_totals("run_a")["per_model"]
    assert "qwen-turbo" not in METER.run_totals("run_b")["per_model"]

    METER.reset()


def test_telemetry_run_totals_empty_for_unknown_run():
    METER.reset()
    totals = METER.run_totals("run_never_seen")
    assert totals == {"per_model": {}, "totals": {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}}


# ---- 9.2 pricing ------------------------------------------------------------


def test_pricing_known_model_math():
    per_model = {"qwen-max": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}}
    usd = estimate_usd(per_model)
    in_price, out_price = PRICES["qwen-max"]
    assert usd == in_price + out_price


def test_pricing_unknown_model_falls_back_to_default():
    per_model = {"totally-made-up-model": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}}
    usd = estimate_usd(per_model)
    in_price, out_price = PRICES["DEFAULT"]
    assert usd == in_price + out_price


def test_pricing_empty_input_is_zero():
    assert estimate_usd({}) == 0.0


# ---- 9.3 STOP sentinel + resume ---------------------------------------------


def _make_task(tid: str, parents=None) -> Task:
    return Task(
        task_id=tid, title=f"task {tid}", description="do it",
        acceptance_criteria=["done"], test_strategy="python -c \"print('ok')\"",
        parents=parents or [],
    )


def test_stop_sentinel_stops_mock_run_between_tasks_and_resume_completes(tmp_path):
    from foreman.mocks import MockExecutor, MockPlanner, MockVerifier
    from foreman.orchestrator import Orchestrator
    from foreman.dispatcher import Dispatcher
    from foreman.workspace import Workspace
    from foreman.config import Settings

    run_root = tmp_path / "runs"
    run_root.mkdir()

    orch = Orchestrator.__new__(Orchestrator)
    orch.settings = Settings(api_key="x", base_url="http://mock.invalid",
                              planner_model="p", executor_model="e", verifier_model="v")
    orch.client = None
    orch.run_root = run_root
    from foreman.models import new_id
    orch.run_id = new_id("run")
    run_dir = run_root / orch.run_id
    run_dir.mkdir()
    orch.run_dir = run_dir
    orch.ledger = Ledger(db_path=str(run_dir / "ledger.db"))
    orch.workspace = Workspace(run_dir / "workspace")
    orch.dispatcher = Dispatcher(orch.ledger)
    orch.planner = MockPlanner()
    orch.executor = MockExecutor()
    orch.verifier = MockVerifier(always_pass=True)  # pass immediately: isolate the STOP behavior
    from foreman.arbiter import Arbiter
    orch.arbiter = Arbiter(None, "mock-arbiter", orch.workspace)
    orch.disputed_task_ids = set()
    orch.events_path = run_dir / "events.jsonl"

    # Write the STOP sentinel BEFORE driving the loop at all, so the very
    # first iteration observes it (task-boundary granularity: zero tasks
    # claimed yet is a valid boundary too).
    (run_dir / "STOP").touch()

    summary = orch.run_checklist("1. step one\n2. step two\n3. step three\n")

    assert summary["stopped"] is True
    assert summary["complete"] is False
    assert summary["claims"] == 0

    events = [json.loads(l) for l in orch.events_path.read_text(encoding="utf-8").strip().splitlines()]
    assert any(e["type"] == "stopped" for e in events)

    # Resume: sentinel must be cleared automatically and the run completes.
    assert (run_dir / "STOP").exists()
    orch2 = Orchestrator.__new__(Orchestrator)
    orch2.settings = orch.settings
    orch2.client = None
    orch2.run_root = run_root
    orch2.run_id = orch.run_id
    orch2.run_dir = run_dir
    orch2.ledger = Ledger(db_path=str(run_dir / "ledger.db"))
    orch2.workspace = Workspace(run_dir / "workspace")
    orch2.dispatcher = Dispatcher(orch2.ledger)
    orch2.planner = MockPlanner()
    orch2.executor = MockExecutor()
    orch2.verifier = MockVerifier(always_pass=True)
    orch2.arbiter = Arbiter(None, "mock-arbiter", orch2.workspace)
    orch2.disputed_task_ids = set()
    orch2.events_path = run_dir / "events.jsonl"

    summary2 = orch2.resume_run(orch.run_id)

    assert not (run_dir / "STOP").exists()
    assert summary2["complete"] is True
    assert summary2["stopped"] is False


def test_stop_sentinel_mid_run_via_dispatcher_thread(tmp_path):
    """Stops a run that has already made progress: write STOP after task 1 is
    done (simulated by a verifier that touches STOP as a side effect of its
    first verify call), confirm the loop halts at the next iteration rather
    than running every task."""
    from foreman.mocks import MockExecutor, MockPlanner
    from foreman.orchestrator import Orchestrator
    from foreman.dispatcher import Dispatcher
    from foreman.workspace import Workspace
    from foreman.config import Settings

    run_root = tmp_path / "runs"
    run_root.mkdir()

    orch = Orchestrator.__new__(Orchestrator)
    orch.settings = Settings(api_key="x", base_url="http://mock.invalid",
                              planner_model="p", executor_model="e", verifier_model="v")
    orch.client = None
    orch.run_root = run_root
    from foreman.models import new_id
    orch.run_id = new_id("run")
    run_dir = run_root / orch.run_id
    run_dir.mkdir()
    orch.run_dir = run_dir
    orch.ledger = Ledger(db_path=str(run_dir / "ledger.db"))
    orch.workspace = Workspace(run_dir / "workspace")
    orch.dispatcher = Dispatcher(orch.ledger)
    orch.planner = MockPlanner()
    orch.executor = MockExecutor()

    stop_path = run_dir / "STOP"

    class _StopAfterFirst:
        def __init__(self):
            self.n = 0

        def verify(self, task, handoff):
            from foreman.verifier import VerificationReport
            self.n += 1
            if self.n == 1:
                stop_path.touch()
            return VerificationReport(
                passed=True, coverage_rate=1.0,
                items=[{"criterion": "c", "status": "satisfied", "detail": "ok"}],
                objective_gate={"command": "mock", "exit_code": 0, "passed": True, "output_tail": ""},
                actionable_feedback=[], reason="ok",
            )

    orch.verifier = _StopAfterFirst()
    from foreman.arbiter import Arbiter
    orch.arbiter = Arbiter(None, "mock-arbiter", orch.workspace)
    orch.disputed_task_ids = set()
    orch.events_path = run_dir / "events.jsonl"

    summary = orch.run_checklist("1. step one\n2. step two\n3. step three\n")

    assert summary["stopped"] is True
    assert summary["done"] == 1  # only the first task got through before STOP halted the loop
    assert summary["complete"] is False


# ---- 9.4 config.json write/read via get_run_detail --------------------------


def make_task(tid: str, parents=None) -> Task:
    return Task(
        task_id=tid, title=f"Task {tid}", description="do the thing",
        acceptance_criteria=["it works"], test_strategy="pytest -q", parents=parents or [],
    )


def _build_ledger_run(tmp_path: Path, run_id: str = "run_cfgtest") -> Path:
    run_root = tmp_path / "runs"
    run_dir = run_root / run_id
    run_dir.mkdir(parents=True)
    ledger = Ledger(db_path=str(run_dir / "ledger.db"))
    ledger.create_run("1. build it\n")
    ledger.add_task(make_task("T1"))
    ledger.recompute_ready()
    return run_root


def test_config_json_write_and_read_via_get_run_detail(tmp_path):
    run_root = _build_ledger_run(tmp_path)
    run_dir = run_root / "run_cfgtest"

    config = {
        "models": {"planner": "qwen-max", "executor": "qwen3-coder-plus", "verifier": "qwen-plus"},
        "mock": False,
        "created_at": "2026-07-04T00:00:00+00:00",
        "requirements_preview": "1. build it",
    }
    (run_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")

    detail = webui_data.get_run_detail(run_root, "run_cfgtest")
    assert detail["config"] == config
    assert detail["usage"]["totals"]["calls"] == 0
    assert detail["est_usd"] is None  # no usage recorded yet, no persisted est_usd either

    # No config.json at all -> config is None, not an error.
    run_dir2 = run_root / "run_no_config"
    run_dir2.mkdir()
    ledger2 = Ledger(db_path=str(run_dir2 / "ledger.db"))
    ledger2.create_run("nothing")
    detail2 = webui_data.get_run_detail(run_root, "run_no_config")
    assert detail2["config"] is None


def test_config_json_usage_final_merge_reflected_in_detail(tmp_path):
    run_root = _build_ledger_run(tmp_path, run_id="run_usagetest")
    run_dir = run_root / "run_usagetest"

    usage_final = {
        "per_model": {"qwen-max": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500, "calls": 2}},
        "totals": {"prompt_tokens": 1000, "completion_tokens": 500, "calls": 2},
    }
    config = {
        "models": {"planner": "qwen-max", "executor": "qwen-max", "verifier": "qwen-max"},
        "mock": False, "created_at": "2026-07-04T00:00:00+00:00", "requirements_preview": "x",
        "usage_final": usage_final,
        "est_usd": estimate_usd(usage_final["per_model"]),
    }
    (run_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")

    # Ensure METER has nothing live under this run_id so get_run_detail falls
    # back to the persisted usage_final (contract: "usage" = live if nonzero
    # else usage_final).
    METER.reset()

    detail = webui_data.get_run_detail(run_root, "run_usagetest")
    assert detail["usage"] == usage_final
    assert detail["est_usd"] == config["est_usd"]


# ---- download zip helper -----------------------------------------------------


def test_download_zip_excludes_db_and_pycache(tmp_path):
    import serve

    run_dir = tmp_path / "run_zip1"
    ws = run_dir / "workspace"
    ws.mkdir(parents=True)
    (ws / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (ws / "ledger.db").write_text("binary-ish", encoding="utf-8")
    (ws / "ledger.db-wal").write_text("wal", encoding="utf-8")
    pycache = ws / "__pycache__"
    pycache.mkdir()
    (pycache / "app.cpython-311.pyc").write_text("bytecode", encoding="utf-8")
    sub = ws / "sub"
    sub.mkdir()
    (sub / "util.py").write_text("x = 1\n", encoding="utf-8")

    data = serve._build_download_zip(run_dir)
    zf = zipfile.ZipFile(__import__("io").BytesIO(data))
    names = set(zf.namelist())

    assert "app.py" in names
    assert os.path.join("sub", "util.py").replace("\\", "/") in {n.replace("\\", "/") for n in names}
    assert not any("ledger.db" in n for n in names)
    assert not any("__pycache__" in n for n in names)


# ---- archive moves dir and list_runs skips it --------------------------------


def test_archive_moves_dir_and_list_runs_skips_it(tmp_path, monkeypatch):
    import serve

    run_root = tmp_path / "runs"
    run_dir = run_root / "run_archive1"
    run_dir.mkdir(parents=True)
    ledger = Ledger(db_path=str(run_dir / "ledger.db"))
    ledger.create_run("archive me")
    ledger.add_task(make_task("T1"))
    del ledger

    monkeypatch.setattr(serve, "RUN_ROOT", str(run_root))
    monkeypatch.setattr(serve, "ARCHIVE_ROOT", run_root / "_archived")

    assert len(webui_data.list_runs(run_root)) == 1

    dest = serve.ARCHIVE_ROOT / "run_archive1"
    serve.ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    import gc
    import shutil
    gc.collect()  # release any lingering sqlite3.Connection file handles (Windows)
    shutil.move(str(run_dir), str(dest))

    assert not run_dir.exists()
    assert dest.exists()
    assert webui_data.list_runs(run_root) == []


# ---- model name validation ----------------------------------------------------


def test_validate_model_name():
    import serve

    assert serve._validate_model_name("qwen-max") is True
    assert serve._validate_model_name("my-custom-model_v2") is True
    assert serve._validate_model_name("") is False
    assert serve._validate_model_name("x" * 65) is False
    assert serve._validate_model_name("bad\nname") is False


# ---- mask key -----------------------------------------------------------------


def test_mask_key_never_returns_full_key():
    import serve

    assert serve._mask_key("") is None
    masked = serve._mask_key("sk-1234567890abcdef")
    assert masked is not None
    assert "1234567890abcdef" not in masked
    assert masked.endswith("ef")


# ---- demo pacing: mock run delay_s ---------------------------------------------


def test_mock_run_delay_is_honored(tmp_path):
    """A mock run with a tiny per-call delay on 2 tasks takes at least
    2x delay_s wall-clock (one execute + one verify per task, always_pass so
    no retries) and still completes normally — pacing must not change the
    outcome, only the timing."""
    from foreman.mocks import build_mock_orchestrator, MockVerifier

    run_root = tmp_path / "runs"
    orch = build_mock_orchestrator(run_root=str(run_root), delay_s=0.05)
    orch.verifier = MockVerifier(always_pass=True, delay_s=0.05)  # isolate timing from the retry ladder

    start = time.monotonic()
    summary = orch.run_checklist("1. step one\n2. step two\n")
    elapsed = time.monotonic() - start

    assert summary["complete"] is True
    assert summary["done"] == 2
    # 2 tasks * (1 execute + 1 verify) * 0.05s = 0.2s minimum.
    assert elapsed >= 0.2


def test_mock_run_zero_delay_is_fast_by_default(tmp_path):
    """delay_s defaults to 0.0 — existing (pre-pacing-feature) callers and the
    rest of the test suite must see no behavior change."""
    from foreman.mocks import build_mock_orchestrator

    run_root = tmp_path / "runs"
    orch = build_mock_orchestrator(run_root=str(run_root))

    start = time.monotonic()
    summary = orch.run_checklist("1. step one\n2. step two\n")
    elapsed = time.monotonic() - start

    assert summary["complete"] is True
    assert elapsed < 2.0  # generous ceiling; this run has zero artificial delay


def test_clamp_mock_delay_s():
    import serve

    assert serve._clamp_mock_delay_s(5) == 5.0
    assert serve._clamp_mock_delay_s(31) == 30.0
    assert serve._clamp_mock_delay_s(100) == 30.0
    assert serve._clamp_mock_delay_s(-1) == 0.0
    assert serve._clamp_mock_delay_s(-0.001) == 0.0
    assert serve._clamp_mock_delay_s(0) == 0.0
    assert serve._clamp_mock_delay_s("not-a-number") == 0.0
    assert serve._clamp_mock_delay_s(None) == 0.0


def test_default_mock_delay_s_reads_env(monkeypatch):
    import serve

    monkeypatch.delenv("FOREMAN_MOCK_DELAY", raising=False)
    assert serve._default_mock_delay_s() == 0.0

    monkeypatch.setenv("FOREMAN_MOCK_DELAY", "4")
    assert serve._default_mock_delay_s() == 4.0

    monkeypatch.setenv("FOREMAN_MOCK_DELAY", "not-a-number")
    assert serve._default_mock_delay_s() == 0.0
