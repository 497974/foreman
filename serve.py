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
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from foreman import webui_data
from foreman.config import Settings, load_env

# Anchor everything to the repo (this file's directory), never the process
# cwd — the console must work when launched from a .bat, an IDE preview, or a
# cloud runtime whose working directory is somewhere else entirely.
REPO_ROOT = Path(__file__).parent
RUN_ROOT = str(REPO_ROOT / "runs")
WEBUI_DIR = REPO_ROOT / "foreman" / "webui"
INDEX_HTML = WEBUI_DIR / "index.html"

# Active background runs, keyed by run_id, so a second POST doesn't need to
# guess whether a thread is still alive; mostly useful for future /stop work.
_active_runs: dict[str, threading.Thread] = {}
_active_runs_lock = threading.Lock()


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _start_run_in_background(requirements: str) -> str:
    """Build a real Orchestrator and run it on a daemon thread.

    Returns the run_id immediately (Orchestrator mints it in __init__, before
    any planning happens), so the caller can start polling
    /api/runs/<id>/events right away even though no tasks exist yet.
    """
    from foreman.orchestrator import Orchestrator

    settings = Settings.from_env(REPO_ROOT / ".env")  # raises RuntimeError if key missing
    orch = Orchestrator(settings, run_root=RUN_ROOT)
    run_id = orch.run_id

    def _worker():
        try:
            orch.run_checklist(requirements)
        except Exception as exc:  # noqa: BLE001 - surface into events, don't crash the thread silently
            try:
                orch._emit("error", detail={"message": str(exc)})
            except Exception:
                pass

    t = threading.Thread(target=_worker, name=f"foreman-run-{run_id}", daemon=True)
    with _active_runs_lock:
        _active_runs[run_id] = t
    t.start()
    return run_id


class Handler(BaseHTTPRequestHandler):
    server_version = "ForemanConsole/1.0"

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
                self._send_json(200, webui_data.list_runs(RUN_ROOT))
            elif (m := re.fullmatch(r"/api/runs/([^/]+)", path)):
                detail = webui_data.get_run_detail(RUN_ROOT, m.group(1))
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
        try:
            if parsed.path == "/api/runs":
                self._handle_start_run()
            else:
                self._send_json(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"error": str(exc)})

    # ---- handlers -----------------------------------------------------------

    def _handle_start_run(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON body"})
            return

        requirements = (payload.get("requirements") or "").strip()
        if not requirements:
            self._send_json(400, {"error": "requirements text is required"})
            return

        # Cheap key check up front so the error is instant and specific, even
        # though _start_run_in_background would also raise via Settings.from_env().
        load_env(".env")
        if not os.environ.get("DASHSCOPE_API_KEY"):
            self._send_json(
                409,
                {
                    "error": "DASHSCOPE_API_KEY is not set. Add it to a .env file "
                    "at the repo root (DASHSCOPE_API_KEY=sk-...) and restart serve.py."
                },
            )
            return

        try:
            run_id = _start_run_in_background(requirements)
        except RuntimeError as exc:
            self._send_json(409, {"error": str(exc)})
            return

        self._send_json(200, {"run_id": run_id})

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
        "--no-browser", action="store_true",
        help="don't auto-open a browser (for previews / headless environments)",
    )
    args = parser.parse_args()

    Path(RUN_ROOT).mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
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
