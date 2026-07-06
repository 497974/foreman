#!/usr/bin/env bash
# Foreman launcher for macOS/Linux — mirror of start_foreman.bat.
# Installs deps, checks .env, starts the web console (http://127.0.0.1:8787).
set -u
cd "$(dirname "$0")"

if [ ! -f ".env" ]; then
    echo ""
    echo "[Foreman] Missing .env file in this folder."
    echo "[Foreman] Please create a .env file with your DASHSCOPE_API_KEY, e.g.:"
    echo ""
    echo "    DASHSCOPE_API_KEY=sk-your-key-here"
    echo ""
    echo "[Foreman] Tip: no key yet? Try Demo mode instead — run 'python3 serve.py'"
    echo "          and check \"Demo mode\" in the New Run form (zero-key, offline)."
    echo ""
    exit 1
fi

PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python

"$PYTHON" -m pip install -r requirements.txt -q || {
    echo ""
    echo "[Foreman] pip install failed. Check your Python install / network and try again."
    exit 1
}

exec "$PYTHON" serve.py "$@"
