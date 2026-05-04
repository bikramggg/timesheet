#!/usr/bin/env bash
# Bulk log all April 2026 worklogs via Playwright replay (headless).
# Run from /home/bikramghosh/timesheet on RPi.
set -euo pipefail

cd "$(dirname "$0")/.."
set -a; . .env; set +a
export TIMESHEET_URL="${TIMESHEET_URL:-http://localhost:8080}"

LOGFILE="data/bulk-april.log"
: > "$LOGFILE"

for d in 2026-04-{01..30}; do
  echo "=== $d ===" | tee -a "$LOGFILE"
  .venv/bin/python scripts/log_worklog_playwright.py "$d" --mode=replay --headless 2>&1 | tee -a "$LOGFILE"
done

echo "Done. Log: $LOGFILE"
grep -E "^\\[replay\\] done" "$LOGFILE" | tail -30
