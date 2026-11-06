#!/usr/bin/env bash
# Sends one transcript request and finds its request_id in the gateway, transcript-service
# and student-service logs. Fails if the ID is missing from any of them.
# Usage: experiments/trace_request.sh [student_id]   (one stack must be running)
set -uo pipefail
cd "$(dirname "$0")/.."

AUTH=()
if docker ps --format '{{.Names}}' | grep -q '^uni-ft-gateway'; then
  FILE=docker-compose.ft.yml
  TRANSCRIPT="transcript-service-1 transcript-service-2"
  STUDENT="student-service-1 student-service-2"
  # Endterm: log in as the student whose transcript we read.
  TOKEN=$(cd experiments && python3 -c "import auth; print(auth.student_token(${1:-1}))") || exit 1
  AUTH=(-H "Authorization: Bearer $TOKEN")
else
  FILE=docker-compose.baseline.yml
  TRANSCRIPT="transcript-service"
  STUDENT="student-service"
fi

ID=$(curl -s -D - -o /dev/null ${AUTH[@]+"${AUTH[@]}"} "http://localhost:8080/transcripts/${1:-1}" \
     | awk 'tolower($1) == "x-request-id:" { print $2 }' | tr -d '\r')
if [ -z "$ID" ]; then echo "FAIL: no X-Request-ID in the response"; exit 1; fi
echo "request_id: $ID ($FILE)"
sleep 1  # let the log lines reach Docker

failed=0
for group in "gateway:gateway" "transcript-service:$TRANSCRIPT" "student-service:$STUDENT"; do
  name=${group%%:*}
  lines=$(docker compose -f "$FILE" logs --no-color ${group#*:} 2>/dev/null | grep "$ID")
  if [ -z "$lines" ]; then
    echo "FAIL $name: not found"
    failed=1
  else
    echo "OK   $name:"
    echo "$lines" | sed 's/^/     /'
  fi
done
exit $failed
