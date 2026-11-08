#!/usr/bin/env bash
# Crash-recovery demonstration (C13): kill -9 the controller at random moments, restart it, let it
# finish, then verify invariants against the simulator API and grade the run.
#   scripts/crash_test.sh [scenario] [kills] [port]
# Log: reports/crash_recovery.log
set -euo pipefail
cd "$(dirname "$0")/.."
SCENARIO="${1:-scenarios/public_02_incidents.yaml}"
KILLS="${2:-4}"
PORT="${3:-8730}"
PY="${PYTHON:-.venv/bin/python}"
NAME=crash
DB="state/${NAME}.db"
LOG=reports/crash_recovery.log
mkdir -p state logs reports runs
: > "$LOG"
: > "logs/${NAME}.controller.log"
say() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

"$PY" -m sim serve --scenario "$SCENARIO" --run-dir "runs/mine_${NAME}" --port "$PORT" --force > "logs/${NAME}.sim.log" 2>&1 &
SIM_PID=$!
trap 'kill $SIM_PID 2>/dev/null || true' EXIT
for _ in $(seq 1 600); do curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 && break; sleep 1; done
rm -f "$DB" "$DB-wal" "$DB-shm"
say "scenario $SCENARIO, simulator pid $SIM_PID on :$PORT, $KILLS random kill -9s"

start_controller() {   # sets PID (must not run in a subshell, or `wait` cannot reap it)
  CONTROLLER_DB="$DB" ./run.sh "http://127.0.0.1:${PORT}" >> "logs/${NAME}.controller.log" 2>&1 &
  PID=$!
}
sim_minute() { curl -s "http://127.0.0.1:${PORT}/sim/state" | "$PY" -c "import sys,json; s=json.load(sys.stdin); print(s['ts'], s['finished'])"; }

for i in $(seq 1 "$KILLS"); do
  start_controller
  WAIT=$(( (RANDOM % 6) + 2 ))
  sleep "$WAIT"
  if kill -0 "$PID" 2>/dev/null; then
    kill -9 "$PID"
    wait "$PID" 2>/dev/null || true
    say "kill -9 #$i after ${WAIT}s at sim $(sim_minute) | proposals in store: $(sqlite3 "$DB" "select group_concat(state||'='||n) from (select state, count(*) n from proposals group by state)" 2>/dev/null)"
  else
    say "controller exited before kill #$i"
  fi
done

say "final restart - running to completion"
start_controller
wait "$PID"
say "controller finished at sim $(sim_minute)"
say "--- invariants"
"$PY" scripts/verify_run.py --sim-url "http://127.0.0.1:${PORT}" --db "$DB" | tee -a "$LOG"
if [ -f "runs/base_public_02/_truth/aggregates.json" ] && [[ "$SCENARIO" == *public_02* ]]; then
  "$PY" -m sim grade --run "runs/mine_${NAME}" --baseline runs/base_public_02 --out "reports/${NAME}_grade.md" > /dev/null
  say "--- grade (runs/mine_${NAME} vs baseline) summary"
  sed -n '5,10p' "reports/${NAME}_grade.md" | tee -a "$LOG"
  grep -E "applied —|mttr" "reports/${NAME}_grade.md" | tee -a "$LOG"
fi
