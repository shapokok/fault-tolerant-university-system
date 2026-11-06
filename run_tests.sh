#!/usr/bin/env bash
# All automated tests with one command.
#   Unit tests: in a container (Python 3.11 with the service libraries).
#   Integration tests: on this machine, against a fresh endterm stack (docker-compose.ft.yml, needs .env).
# Writes results/endterm/tests_junit.xml, results/endterm/tests_summary.txt and docs/test_plan.md.
# Usage: ./run_tests.sh            (restarts the endterm stack: known state)
#        ./run_tests.sh --keep     (uses the endterm stack that is already running)
set -uo pipefail
cd "$(dirname "$0")"
OUT=results/endterm
mkdir -p "$OUT"

echo "== unit tests"
docker build -q -t uni-tests -f tests/Dockerfile services >/dev/null
docker run --rm -v "$PWD:/repo" -w /repo uni-tests \
  python -m pytest tests/unit -q -p no:cacheprovider -o junit_family=xunit1 --junitxml="$OUT/tests_unit.xml"

echo "== integration tests"
if [ ! -x .venv-tests/bin/python ]; then  # pytest for this machine, inside the project only
  python3 -m venv .venv-tests && .venv-tests/bin/pip install -q pytest==8.3.3
fi
if [ "${1:-}" != "--keep" ]; then
  (cd experiments && python3 -c "from run_all import start_stack; start_stack('ft', 'endterm/raw/tests_monitor.csv')")
fi
.venv-tests/bin/python -m pytest tests/integration -q -p no:cacheprovider -o junit_family=xunit1 \
  --junitxml="$OUT/tests_integration.xml"

python3 tests/report.py
