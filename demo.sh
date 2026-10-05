#!/usr/bin/env bash
# Live demo of 3 failure scenarios.
# Usage: ./demo.sh [ft|baseline] [app_crash|db_failure|lost_transaction|all]
# DEMO_FAST=1 ./demo.sh ...   runs without "press Enter" pauses.
set -euo pipefail
cd "$(dirname "$0")"

MODE=${1:-ft}
WHAT=${2:-all}
COMPOSE="docker compose -f docker-compose.$MODE.yml"
RUN=demo_${MODE}_$(date +%H%M%S)
MON=results/${RUN}_monitor.csv
G=http://localhost:8080

step()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
pause() { if [ -z "${DEMO_FAST:-}" ]; then read -rp "   (press Enter) " _; fi; }

start_stack() {
  step "Start the $MODE stack"
  docker compose -f docker-compose.baseline.yml down -v >/dev/null 2>&1
  docker compose -f docker-compose.ft.yml down -v >/dev/null 2>&1
  MONITOR_FILE=${RUN}_monitor.csv $COMPOSE up -d --build >/dev/null 2>&1
  until curl -sf $G/students/1 >/dev/null && curl -sf $G/transcripts/1 >/dev/null; do sleep 1; done
  sleep 8
  $COMPOSE ps --format '   {{.Service}}: {{.Status}}'
}

monitor_since() {  # monitor state changes after time $1
  awk -F, -v t="$1" 'NR > 1 && $1 >= t { printf "   +%.2fs  %-13s %-9s %s\n", $1 - t, $2, $3, $4 }' "$MON"
}

run_scenario() {
  local sc=$1
  start_stack  # fresh stack per scenario, so one fault does not leak into the next
  step "Scenario: $sc ($MODE)"
  echo "   workload: 20 req/s for 45 s, fault injected at t=10 s"
  pause
  $COMPOSE run --rm -T workload python /exp/workload.py --duration 45 \
    --out /results/raw/${RUN}_${sc}.csv > results/raw/${RUN}_${sc}_summary.txt 2>&1 &
  local wl=$!
  sleep 10
  local t; t=$(python3 -c 'import time; print(f"{time.time():.3f}")')
  python3 experiments/inject.py "$sc" "$MODE" > /dev/null &
  local inj=$!
  echo "   injected $sc"

  sleep 5
  case $sc in
    app_crash)
      docker inspect -f '   payment container: {{.State.Status}}, restarts={{.RestartCount}}' \
        "$( [ "$MODE" = ft ] && echo uni-ft-payment-service-1-1 || echo uni-baseline-payment-service-1 )" ;;
    db_failure)
      curl -s -o /dev/null -w "   read  GET /timetable/1     -> %{http_code}\n" $G/timetable/1
      curl -s -o /dev/null -w "   write POST /registrations  -> %{http_code}\n" -X POST $G/registrations \
        -H 'content-type: application/json' -d '{"student_id":1,"course_id":2}' ;;
    lost_transaction)
      curl -s -o /dev/null -w "   POST /payments -> %{http_code}\n" -X POST $G/payments \
        -H 'content-type: application/json' -H "Idempotency-Key: demo-$RUN" -d '{"student_id":1,"amount":100}' ;;
  esac

  wait $inj $wl
  step "What the monitor saw (time after injection)"
  monitor_since "$t" | head -12
  step "Workload result"
  tail -7 results/raw/${RUN}_${sc}_summary.txt
  if [ "$sc" = lost_transaction ]; then
    step "Payments before reconciliation"
    $COMPOSE exec -T postgres-primary psql -U uni -d uni -tAc \
      "SELECT '   ' || status || ': ' || count(*) FROM payments GROUP BY status ORDER BY status;"
  fi
  step "Consistency check"
  echo "   $(python3 experiments/consistency_check.py "$MODE")"
  pause
}

if [ "$WHAT" = all ]; then
  for sc in app_crash db_failure lost_transaction; do run_scenario $sc; done
else
  run_scenario "$WHAT"
fi
step "Done. Stop with: $COMPOSE down -v"
