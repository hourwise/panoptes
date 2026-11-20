#!/bin/bash
# Start the Panoptes dashboard (if it is not already up) and open it in the browser.
# Safe to launch repeatedly: if /api/health answers, the browser is opened and nothing is started.
set -euo pipefail

URL="http://127.0.0.1:8765"
REPO="$(dirname "$(dirname "$(dirname "$(readlink -f "$0")")")")"   # .../ops/desktop -> repo root
LOG_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/panoptes"
LOG="$LOG_DIR/server.log"

health() {
    python3 - "$URL/api/health" <<'PY' 2>/dev/null
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=2) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
}

if health; then
    exec xdg-open "$URL/"
fi

mkdir -p "$LOG_DIR"
cd "$REPO"
setsid python3 -m wsd >>"$LOG" 2>&1 </dev/null &

started=""
for _ in $(seq 1 15); do
    if health; then started=yes; break; fi
    sleep 1
done

if [ -z "$started" ]; then
    MSG="$(tail -n 20 "$LOG" 2>/dev/null || echo "(no log file at $LOG)")"
    if command -v notify-send >/dev/null 2>&1; then
        notify-send "Panoptes" "The dashboard server failed to start. Last log lines:

$MSG" || true
    else
        echo "Panoptes: the dashboard server failed to start. Last log lines:" >&2
        printf '%s\n' "$MSG" >&2
    fi
    exit 1
fi

exec xdg-open "$URL/"
