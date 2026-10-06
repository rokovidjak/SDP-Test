#!/usr/bin/env bash
# One-command launcher: creates a virtual environment on first run,
# installs dependencies, then starts the Repo Analysis Tool dashboard.
set -e
cd "$(dirname "$0")"

PORT="${PORT:-8000}"

if [ ! -d .venv ]; then
  echo "Creating virtual environment (.venv)..."
  python3 -m venv .venv
fi

. .venv/bin/activate
python -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
python -m pip install --quiet -r requirements.txt

echo
echo "Repo Analysis Tool running at http://localhost:${PORT}"
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT}"
