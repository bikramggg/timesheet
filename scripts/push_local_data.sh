#!/usr/bin/env bash
# Run on Mac. Pushes VSCode tracker JSON + ActivityWatch DB to RPi.
set -euo pipefail

RPI="${RPI:?Set RPI=user@host (e.g. RPI=pi@raspberrypi.local)}"
DEST="${DEST:-/home/${RPI%@*}/timesheet/data/local}"
TMP="$(mktemp -d)"
trap "rm -rf $TMP" EXIT

# 1. VSCode tracker → JSON
sqlite3 ~/Library/Application\ Support/Code/User/globalStorage/state.vscdb \
  "SELECT value FROM ItemTable WHERE key='noorashuvo.simple-coding-time-tracker';" \
  > "$TMP/vscode_tracker.json"

# 2. ActivityWatch DB → copy (live DB; SQLite WAL means a copy is point-in-time consistent enough)
cp ~/Library/Application\ Support/activitywatch/aw-server/peewee-sqlite.v2.db "$TMP/activitywatch.db"

# 3. Push
ssh "$RPI" "mkdir -p $DEST"
rsync -az --partial "$TMP/vscode_tracker.json" "$RPI:$DEST/vscode_tracker.json"
rsync -az --partial "$TMP/activitywatch.db" "$RPI:$DEST/activitywatch.db"

echo "Pushed to $RPI:$DEST"

# ntfy ping (best-effort, uses RPi-side env on the server)
if [ -n "${NTFY_URL:-}" ] && [ -n "${NTFY_TOPIC:-}" ]; then
  TS=$(date '+%Y-%m-%d %H:%M:%S')
  curl -fsS -m 10 \
    -H "Title: Mac data pushed" \
    -H "Tags: laptop" \
    -d "vscode + activitywatch synced at $TS" \
    "$NTFY_URL/$NTFY_TOPIC" >/dev/null 2>&1 || echo "ntfy ping failed"
fi
