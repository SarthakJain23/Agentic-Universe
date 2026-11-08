#!/usr/bin/env bash
# Starts the controller and runs it until the scenario finishes. No manual steps.
#   ./run.sh http://127.0.0.1:8700
#
# Runtime: plain Python >= 3.11 + the repo's requirements.txt (httpx, numpy). Storage: one SQLite file
# (stdlib) at $CONTROLLER_DB (default state/controller.db). No Docker, no extra services.
# Safe to kill and re-run: it resumes from the store and the simulator's current minute.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"

if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
elif [ -x .venv/bin/python ] && .venv/bin/python -c "import httpx, numpy" 2>/dev/null; then
  PY=.venv/bin/python
else
  python3 -m venv .venv
  .venv/bin/python -m pip install -q -r requirements.txt
  PY=.venv/bin/python
fi

exec "$PY" -m controller run --sim-url "$SIM_URL" --db "${CONTROLLER_DB:-state/controller.db}"
