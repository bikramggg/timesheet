#!/usr/bin/env bash
# Daily: collect today's data, then log worklogs to HnR.
set -uo pipefail  # don't exit on errors, log them instead

cd "$(dirname "$0")/.."
set -a; . .env; set +a
export TIMESHEET_URL="${TIMESHEET_URL:-http://localhost:8080}"

TODAY=$(TZ=Asia/Kolkata date +%F)
LOGFILE="data/daily-worklog.log"

{
  echo "===== $(date -Is) — daily worklog for $TODAY ====="
  echo "--- collecting ---"
  .venv/bin/python -m collectors.run_all "$TODAY" "$TODAY" || echo "(collect failed, continuing)"
  echo "--- logging ---"
  .venv/bin/python scripts/log_worklog_playwright.py "$TODAY" --mode=replay --headless
} 2>&1 | tee -a "$LOGFILE"

# ntfy
if [ -n "${NTFY_URL:-}" ] && [ -n "${NTFY_TOPIC:-}" ]; then
  RESULT=$(grep -E "^\[replay\] done" "$LOGFILE" | tail -1)
  curl -fsS -m 10 \
    -H "Title: Worklog logged for $TODAY" \
    -H "Tags: stopwatch" \
    -d "${RESULT:-no result line}" \
    "$NTFY_URL/$NTFY_TOPIC" >/dev/null 2>&1 || true
fi
