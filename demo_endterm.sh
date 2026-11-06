#!/usr/bin/env bash
# Endterm demo: unauthorized access, abusive requests, instance failure (alert fires and resolves).
# Usage: ./demo_endterm.sh [all|unauthorized|abusive|instance]     DEMO_FAST=1 ./demo_endterm.sh (no pauses)
# Needs .env (cp .env.example .env and fill in values). Grafana: http://localhost:3000
set -euo pipefail
cd "$(dirname "$0")/experiments"
exec python3 demo_endterm.py "${1:-all}"
