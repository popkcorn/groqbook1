#!/usr/bin/env bash
# Bookwright launcher for macOS and Linux.
#   ./start-mac-linux.sh            run it
#   ./start-mac-linux.sh --port 9000
set -euo pipefail
cd "$(dirname "$0")"

# 1. Find Python 3.10+
PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$candidate"
    break
  fi
done

if [ -z "$PY" ]; then
  cat <<'MSG'

  Python 3.10 or newer was not found.

  macOS:  brew install python@3.12
  Ubuntu: sudo apt install python3 python3-venv

MSG
  exit 1
fi

# 2. Local virtual environment, created once
if [ ! -x ".venv/bin/python" ]; then
  echo "Creating virtual environment (.venv)…"
  "$PY" -m venv .venv
fi

# 3. Dependencies, installed once
if ! .venv/bin/python -c "import fastapi, uvicorn, httpx, reportlab" >/dev/null 2>&1; then
  echo "Installing dependencies, this takes a minute on first run…"
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  .venv/bin/python -m pip install -r requirements-bookwright.txt
fi

# 4. Run
exec .venv/bin/python run.py "$@"
