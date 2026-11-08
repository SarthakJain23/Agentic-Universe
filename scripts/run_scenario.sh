#!/usr/bin/env bash
# Full cycle for one scenario: baseline (if missing) -> serve -> ./run.sh -> grade.
#   scripts/run_scenario.sh scenarios/public_01_dev.yaml public_01 [port]
# Outputs: runs/base_<name>, runs/mine_<name>, state/<name>.db, reports/<name>.md, logs/<name>.controller.log
set -euo pipefail
cd "$(dirname "$0")/.."
SCENARIO="${1:?scenario yaml}"
NAME="${2:?name}"
PORT="${3:-8700}"
PY="${PYTHON:-.venv/bin/python}"
mkdir -p runs reports logs state

if [ ! -f "runs/base_${NAME}/_truth/aggregates.json" ]; then
  echo "== baseline ${NAME}"
  "$PY" -m sim baseline --scenario "$SCENARIO" --run-dir "runs/base_${NAME}"
fi

echo "== serve ${NAME} on :${PORT}"
"$PY" -m sim serve --scenario "$SCENARIO" --run-dir "runs/mine_${NAME}" --port "$PORT" --force \
  > "logs/${NAME}.sim.log" 2>&1 &
SIM_PID=$!
trap 'kill $SIM_PID 2>/dev/null || true' EXIT
for _ in $(seq 1 600); do
  curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 && break
  sleep 1
done

rm -f "state/${NAME}.db" "state/${NAME}.db-wal" "state/${NAME}.db-shm"
echo "== controller ${NAME}"
START=$(date +%s)
CONTROLLER_DB="state/${NAME}.db" ./run.sh "http://127.0.0.1:${PORT}" > "logs/${NAME}.controller.log" 2>&1
echo "controller wall: $(( $(date +%s) - START ))s"

"$PY" -m sim grade --run "runs/mine_${NAME}" --baseline "runs/base_${NAME}" --out "reports/${NAME}.md" > /dev/null
sed -n '1,12p' "reports/${NAME}.md"
grep -E "applied —" "reports/${NAME}.md" || true
