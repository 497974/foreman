"""Foreman local web console.

    python serve.py [--port 8787]

Stdlib-only HTTP server (http.server.ThreadingHTTPServer) that serves the
single-file UI at foreman/webui/index.html and a small JSON API under /api/.
No Flask/FastAPI — the whole console must run from nothing but
requirements.txt (openai + pytest), per docs/CONTRACTS.md Addendum §8.

All ledger/events reads are re-done fresh on every request via
foreman.webui_data — that module has no socket code at all, so it is the
thing tests/test_webui_api.py exercises directly. This file is just wiring:
route -> data function -> JSON response, plus the POST /api/runs endpoint
that starts a real Orchestrator run in a background daemon thread.

Addendum 2 (contract §9.6) adds the Product Console v2 API: stop/resume,
download/archive, run config with model overrides, mock mode, and a small
/api/config + /api/templates surface for the frontend's New Run form.
"""

from __future__ import annotations

import argparse
import dataclasses
import io
import json
import os
import re
import shutil
import string
import sys
import threading
import webbrowser
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from foreman import webui_data
from foreman.config import Settings, load_env
from foreman.pricing import estimate_usd
from foreman.telemetry import METER

# Anchor everything to the repo (this file's directory), never the process
# cwd — the console must work when launched from a .bat, an IDE preview, or a
# cloud runtime whose working directory is somewhere else entirely.
REPO_ROOT = Path(__file__).parent
RUN_ROOT = str(REPO_ROOT / "runs")
ARCHIVE_ROOT = REPO_ROOT / "runs" / webui_data.ARCHIVED_DIRNAME
WEBUI_DIR = REPO_ROOT / "foreman" / "webui"
INDEX_HTML = WEBUI_DIR / "index.html"
DEMO_DIR = REPO_ROOT / "demo"

MODELS_KNOWN = ["qwen-max", "qwen-plus", "qwen3-coder-plus", "qwen-turbo", "qwen-flash"]

# Active background runs, keyed by run_id, so a second POST doesn't need to
# guess whether a thread is still alive; also backs stop/resume/archive's
# "is this run currently running" checks (contract §9.6).
_active_runs: dict[str, threading.Thread] = {}
_active_runs_lock = threading.Lock()


def is_active(run_id: str) -> bool:
    """True iff a worker thread for run_id is registered AND still alive.

    Dead threads are lazily dropped from the registry here rather than in a
    background reaper — this module has no timer infrastructure and a lazy
    check on the (infrequent, human-triggered) stop/resume/archive/list path
    is more than fast enough.
    """
    with _active_runs_lock:
        t = _active_runs.get(run_id)
        if t is None:
            return False
        if not t.is_alive():
            del _active_runs[run_id]
            return False
        return True


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_model_name(name: str) -> bool:
    """Non-empty, printable ASCII, <=64 chars. Any model NAME is allowed —
    custom models are a feature (contract §9.6) — this only guards against
    garbage/injection-shaped input, not against unknown model catalogs.
    """
    if not name or len(name) > 64:
        return False
    return all(ch in string.printable and ch not in "\r\n\t" for ch in name)


def _write_run_config(run_dir: Path, settings: Settings, mock: bool, requirements: str) -> None:
    config = {
        "models": {
            "planner": settings.planner_model,
            "executor": settings.executor_model,
            "verifier": settings.verifier_model,
        },
        "mock": bool(mock),
        "created_at": _iso_now(),
        "requirements_preview": requirements[:200],
    }
    (run_dir / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def _merge_usage_final(run_dir: Path, run_id: str) -> None:
    """Fold the run's final METER usage into config.json (contract §9.4).

    Called from the worker wrapper's finally, BEFORE the thread-local run tag
    is cleared, for both fresh runs and resumes, and regardless of whether
    the run finished naturally, stopped, or errored — a stopped/errored run
    still deserves an honest partial cost readout.
    """
    config_path = run_dir / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        config = {}

    usage_final = METER.run_totals(run_id)
    config["usage_final"] = usage_final
    config["est_usd"] = estimate_usd(usage_final.get("per_model", {}))

    try:
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _quota_friendly_detail(exc: Exception) -> dict:
    message = str(exc)
    detail = {"message": message}
    if "insufficient_quota" in message:
        detail["friendly"] = (
            "Model free quota exhausted — switch the executor model in New "
            "Run, or add billing/coupon in the console."
        )
    return detail


def _run_worker(orch, requirements: str) -> None:
    """Shared worker body for a freshly-started run (contract §9.1/§9.4).

    Wraps orch.run_checklist so config.json's usage_final/est_usd are always
    merged in a finally, however the run ends (natural completion, STOP
    sentinel, or exception) — and unregisters the thread from _active_runs so
    resume/archive/list see it as no-longer-active immediately.
    """
    run_id = orch.run_id
    try:
        orch.run_checklist(requirements)
    except Exception as exc:  # noqa: BLE001 - surface into events, don't crash the thread silently
        try:
            orch._emit("error", detail=_quota_friendly_detail(exc))
        except Exception:
            pass
    finally:
        _merge_usage_final(orch.run_dir, run_id)
        with _active_runs_lock:
            _active_runs.pop(run_id, None)


def _resume_worker(orch, run_id: str) -> None:
    """Worker body for a resumed run — same usage-merge contract as fresh runs."""
    try:
        orch.resume_run(run_id)
    except Exception as exc:  # noqa: BLE001
        try:
            orch._emit("error", detail=_quota_friendly_detail(exc))
        except Exception:
            pass
    finally:
        _merge_usage_final(orch.run_dir, run_id)
        with _active_runs_lock:
            _active_runs.pop(run_id, None)


def _settings_with_overrides(models: dict) -> Settings:
    """Settings.from_env() plus dataclasses.replace() for any model override
    supplied in the POST body (contract §9.6). Settings is frozen, so
    overriding fields means building a new instance via dataclasses.replace
    rather than mutation.
    """
    settings = Settings.from_env(REPO_ROOT / ".env")
    overrides = {}
    for role in ("planner", "executor", "verifier"):
        value = (models or {}).get(role)
        if value:
            overrides[f"{role}_model"] = value
    if overrides:
        settings = dataclasses.replace(settings, **overrides)
    return settings


def _start_real_run(requirements: str, models: dict) -> str:
    """Build a real Orchestrator (with optional per-role model overrides) and
    run it on a daemon thread. Returns the run_id immediately."""
    from foreman.orchestrator import Orchestrator

    settings = _settings_with_overrides(models)
    orch = Orchestrator(settings, run_root=RUN_ROOT)
    run_id = orch.run_id

    _write_run_config(orch.run_dir, orch.settings, mock=False, requirements=requirements)

    t = threading.Thread(target=_run_worker, args=(orch, requirements), name=f"foreman-run-{run_id}", daemon=True)
    with _active_runs_lock:
        _active_runs[run_id] = t
    t.start()
    return run_id


def _default_mock_delay_s() -> float:
    """Env-var default for mock pacing (FOREMAN_MOCK_DELAY), used when the
    POST body omits "mock_delay_s" entirely — lets filming set it once via
    the environment (e.g. before start_foreman.bat) instead of on every
    request. Falls back to 0.0 (unchanged, instant mock runs) on anything
    unset or unparseable.
    """
    raw = os.environ.get("FOREMAN_MOCK_DELAY", "")
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _clamp_mock_delay_s(value) -> float:
    """Clamp to [0, 30] seconds per call — a demo-pacing knob, not a real
    timeout, so a stray large or negative value can't hang or error a run."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v < 0:
        return 0.0
    if v > 30:
        return 30.0
    return v


def _start_mock_run(requirements: str, mock_delay_s: float = 0.0) -> str:
    """Build a mock Orchestrator (foreman.mocks) and run it on a daemon
    thread — no Settings/API key required (contract §9.5/§9.6).

    ``mock_delay_s`` paces each fake execute()/verify() call (demo filming
    aid, see foreman/mocks.py) — 0.0 keeps mock runs instant as before.
    """
    from foreman.mocks import build_mock_orchestrator

    orch = build_mock_orchestrator(run_root=RUN_ROOT, delay_s=mock_delay_s)
    run_id = orch.run_id

    _write_run_config(orch.run_dir, orch.settings, mock=True, requirements=requirements)

    t = threading.Thread(target=_run_worker, args=(orch, requirements), name=f"foreman-mockrun-{run_id}", daemon=True)
    with _active_runs_lock:
        _active_runs[run_id] = t
    t.start()
    return run_id


def _run_dir_for(run_id: str) -> Path:
    return Path(RUN_ROOT) / run_id


def _build_resume_orchestrator(run_id: str):
    """Reopen an existing run's ledger.db + workspace for POST .../resume.

    Mirrors main.py's _build_resume_orchestrator. Mock runs are rejected by
    the caller before this is reached (contract §9.6: "mock runs are not
    resumable") since a mock run has no real client/model to reconstruct.
    """
    from foreman.arbiter import Arbiter
    from foreman.config import make_client
    from foreman.dispatcher import Dispatcher
    from foreman.executor import Executor
    from foreman.ledger import Ledger
    from foreman.orchestrator import Orchestrator
    from foreman.planner import Planner
    from foreman.verifier import Verifier
    from foreman.workspace import Workspace

    run_dir = _run_dir_for(run_id)
    if not run_dir.is_dir():
        raise FileNotFoundError(run_id)

    settings = Settings.from_env(REPO_ROOT / ".env")

    orch = Orchestrator.__new__(Orchestrator)
    orch.settings = settings
    orch.client = make_client(settings)
    orch.run_root = Path(RUN_ROOT)
    orch.run_id = run_id
    orch.run_dir = run_dir
    orch.ledger = Ledger(db_path=str(run_dir / "ledger.db"))
    orch.workspace = Workspace(run_dir / "workspace")
    orch.dispatcher = Dispatcher(orch.ledger)
    orch.planner = Planner(orch.client, settings.planner_model)
    orch.executor = Executor(orch.client, settings.executor_model, orch.workspace)
    orch.verifier = Verifier(orch.client, settings.verifier_model, orch.workspace)
    orch.arbiter = Arbiter(orch.client, settings.planner_model, orch.workspace)
    orch.events_path = run_dir / "events.jsonl"
    orch.disputed_task_ids = set()
    return orch


def _build_download_zip(run_dir: Path) -> bytes:
    """Zip the run's workspace/ into an in-memory buffer, excluding
    __pycache__ directories and any sqlite db/wal/shm files (contract §9.6).
    """
    workspace_dir = run_dir / "workspace"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        if workspace_dir.is_dir():
            for path in workspace_dir.rglob("*"):
                if path.is_dir():
                    continue
                if "__pycache__" in path.parts:
                    continue
                if path.suffix == ".db" or ".db-" in path.name:
                    continue
                arcname = path.relative_to(workspace_dir)
                zf.write(path, arcname=str(arcname))
    return buf.getvalue()


def _mask_key(key: str) -> Optional[str]:
    if not key:
        return None
    if len(key) <= 6:
        return "…" + key[-2:]
    return f"{key[:5]}…{key[-2:]}"


class Handler(BaseHTTPRequestHandler):
    server_version = "ForemanConsole/2.0"

    # Quiet the default per-request stderr logging; keep it opt-in via env.
    def log_message(self, fmt, *args):
        if os.environ.get("FOREMAN_HTTP_VERBOSE"):
            super().log_message(fmt, *args)

    # ---- routing ----------------------------------------------------------

    def do_GET(self):  # noqa: N802 (stdlib naming)
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        try:
            if path == "/" or path == "/index.html":
                self._serve_index()
            elif path == "/api/runs":
                self._send_json(200, webui_data.list_runs(RUN_ROOT, is_active=is_active))
            elif path == "/api/config":
                self._handle_get_config()
            elif path == "/api/templates":
                self._handle_get_templates()
            elif (m := re.fullmatch(r"/api/runs/([^/]+)/download", path)):
                self._handle_download(m.group(1))
            elif (m := re.fullmatch(r"/api/runs/([^/]+)", path)):
                detail = webui_data.get_run_detail(RUN_ROOT, m.group(1), is_active=is_active)
                if detail is None:
                    self._send_json(404, {"error": f"unknown run {m.group(1)}"})
                else:
                    self._send_json(200, detail)
            elif (m := re.fullmatch(r"/api/runs/([^/]+)/events", path)):
                after = int(query.get("after", ["0"])[0])
                result = webui_data.read_events(RUN_ROOT, m.group(1), after=after)
                if result is None:
                    self._send_json(404, {"error": f"unknown run {m.group(1)}"})
                else:
                    self._send_json(200, result)
            elif (m := re.fullmatch(r"/api/runs/([^/]+)/task/([^/]+)", path)):
                task = webui_data.get_task_detail(RUN_ROOT, m.group(1), m.group(2))
                if task is None:
                    self._send_json(404, {"error": f"unknown task {m.group(2)}"})
                else:
                    self._send_json(200, task)
            else:
                self._send_json(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001 - never let a bad request kill the server
            self._send_json(500, {"error": str(exc)})

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/runs":
                self._handle_start_run()
            elif (m := re.fullmatch(r"/api/runs/([^/]+)/stop", path)):
                self._handle_stop(m.group(1))
            elif (m := re.fullmatch(r"/api/runs/([^/]+)/resume", path)):
                self._handle_resume(m.group(1))
            else:
                self._send_json(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"error": str(exc)})

    def do_DELETE(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if (m := re.fullmatch(r"/api/runs/([^/]+)", path)):
                self._handle_archive(m.group(1))
            else:
                self._send_json(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"error": str(exc)})

    # ---- body helpers -------------------------------------------------------

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    # ---- handlers -----------------------------------------------------------

    def _handle_start_run(self):
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON body"})
            return

        requirements = (payload.get("requirements") or "").strip()
        if not requirements:
            self._send_json(400, {"error": "requirements text is required"})
            return

        mock = bool(payload.get("mock", False))
        models = payload.get("models") or {}
        if not isinstance(models, dict):
            self._send_json(400, {"error": "models must be an object"})
            return
        for role, value in models.items():
            if role not in ("planner", "executor", "verifier"):
                self._send_json(400, {"error": f"unknown model role: {role}"})
                return
            if value is not None and not _validate_model_name(str(value)):
                self._send_json(400, {"error": f"invalid model name for {role}: {value!r}"})
                return

        if mock:
            # Demo mode requires NO key at all (contract §9.6).
            if "mock_delay_s" in payload:
                mock_delay_s = _clamp_mock_delay_s(payload.get("mock_delay_s"))
            else:
                mock_delay_s = _clamp_mock_delay_s(_default_mock_delay_s())
            run_id = _start_mock_run(requirements, mock_delay_s=mock_delay_s)
            self._send_json(200, {"run_id": run_id})
            return

        # Cheap key check up front so the error is instant and specific, even
        # though _start_real_run would also raise via Settings.from_env().
        load_env(REPO_ROOT / ".env")
        if not os.environ.get("DASHSCOPE_API_KEY"):
            self._send_json(
                409,
                {
                    "error": "DASHSCOPE_API_KEY is not set. Add it to a .env file "
                    "at the repo root (DASHSCOPE_API_KEY=sk-...) and restart serve.py, "
                    "or check \"Demo mode\" to run without a key."
                },
            )
            return

        try:
            run_id = _start_real_run(requirements, models)
        except RuntimeError as exc:
            self._send_json(409, {"error": str(exc)})
            return

        self._send_json(200, {"run_id": run_id})

    def _handle_stop(self, run_id: str):
        run_dir = _run_dir_for(run_id)
        if not run_dir.is_dir():
            self._send_json(404, {"error": f"unknown run {run_id}"})
            return
        # Idempotent: touching an already-stopped run's sentinel again is a
        # no-op success, not an error.
        (run_dir / "STOP").touch(exist_ok=True)
        self._send_json(200, {"stopped": True})

    def _handle_resume(self, run_id: str):
        run_dir = _run_dir_for(run_id)
        if not run_dir.is_dir():
            self._send_json(404, {"error": f"unknown run {run_id}"})
            return

        if is_active(run_id):
            self._send_json(409, {"error": f"run {run_id} is still active"})
            return

        config = webui_data._read_config(run_dir)
        if config and config.get("mock"):
            self._send_json(400, {"error": "mock runs are not resumable"})
            return

        load_env(REPO_ROOT / ".env")
        if not os.environ.get("DASHSCOPE_API_KEY"):
            self._send_json(
                409,
                {"error": "DASHSCOPE_API_KEY is not set. Add it to a .env file at the repo root and restart serve.py."},
            )
            return

        try:
            orch = _build_resume_orchestrator(run_id)
        except FileNotFoundError:
            self._send_json(404, {"error": f"unknown run {run_id}"})
            return

        stop_path = run_dir / "STOP"
        if stop_path.exists():
            stop_path.unlink()

        t = threading.Thread(target=_resume_worker, args=(orch, run_id), name=f"foreman-resume-{run_id}", daemon=True)
        with _active_runs_lock:
            _active_runs[run_id] = t
        t.start()

        self._send_json(200, {"run_id": run_id})

    def _handle_download(self, run_id: str):
        run_dir = _run_dir_for(run_id)
        if not run_dir.is_dir():
            self._send_json(404, {"error": f"unknown run {run_id}"})
            return
        body = _build_download_zip(run_dir)
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="foreman-{run_id}.zip"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_archive(self, run_id: str):
        run_dir = _run_dir_for(run_id)
        if not run_dir.is_dir():
            self._send_json(404, {"error": f"unknown run {run_id}"})
            return
        if is_active(run_id):
            self._send_json(409, {"error": f"run {run_id} is still active"})
            return

        ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
        dest = ARCHIVE_ROOT / run_id
        if dest.exists():
            shutil.rmtree(dest)
        # Windows keeps a file lock on ledger.db/-wal/-shm until every
        # sqlite3.Connection object referencing them is actually garbage
        # collected (webui_data opens short-lived connections per request but
        # never explicitly closes the Python object beyond its `with` block,
        # which only commits — it does not close()). A stale handle from a
        # request that ran moments ago is enough to make shutil.move raise
        # WinError 32 here; one gc.collect() is cheap and reliable insurance
        # on this platform. No-op cost on Linux/mac.
        import gc
        gc.collect()
        shutil.move(str(run_dir), str(dest))
        self._send_json(200, {"archived": True})

    def _handle_get_config(self):
        load_env(REPO_ROOT / ".env")
        api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        base_url = os.environ.get(
            "DASHSCOPE_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
        )
        self._send_json(
            200,
            {
                "key_present": bool(api_key),
                "key_masked": _mask_key(api_key),
                "endpoint": base_url,
                "models_known": MODELS_KNOWN,
                "defaults": {
                    "planner": os.environ.get("FOREMAN_PLANNER_MODEL", "qwen-max"),
                    "executor": os.environ.get("FOREMAN_EXECUTOR_MODEL", "qwen3-coder-plus"),
                    "verifier": os.environ.get("FOREMAN_VERIFIER_MODEL", "qwen-plus"),
                },
            },
        )

    def _handle_get_templates(self):
        templates = []
        if DEMO_DIR.is_dir():
            for path in sorted(DEMO_DIR.glob("*.md")):
                try:
                    content = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                templates.append({"name": path.stem, "content": content})
        self._send_json(200, templates)

    def _serve_index(self):
        if not INDEX_HTML.exists():
            self._send_json(500, {"error": f"missing {INDEX_HTML}"})
            return
        body = INDEX_HTML.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, obj) -> None:
        body = _json_bytes(obj)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    parser = argparse.ArgumentParser(description="Foreman local web console")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="bind address (use 0.0.0.0 for cloud/container runtimes, e.g. Alibaba "
        "Cloud Function Compute custom runtime, which requires listening on all "
        "interfaces; default 127.0.0.1 keeps local runs loopback-only)",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="don't auto-open a browser (for previews / headless environments)",
    )
    args = parser.parse_args()

    Path(RUN_ROOT).mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Foreman web console: {url}")
    print("Press Ctrl+C to stop.")

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass  # headless environments: no browser to open, still serve fine

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
