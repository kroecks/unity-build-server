#!/bin/bash
# Stop the background server started by start.sh. No Ctrl-C needed.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$HERE/server.pid"

if [[ ! -f "$PIDFILE" ]]; then
  echo "No pidfile — server not running (or wasn't started via start.sh)."
  # Best-effort: offer to find a stray uvicorn for this app.
  if pgrep -f "uvicorn app.server:app" >/dev/null 2>&1; then
    echo "Found a stray uvicorn for app.server. Kill it with:"
    echo "    pkill -f 'uvicorn app.server:app'"
  fi
  exit 0
fi

PID="$(cat "$PIDFILE")"
if kill -0 "$PID" 2>/dev/null; then
  echo "Stopping server (pid $PID)..."
  kill "$PID" 2>/dev/null || true
  # Wait up to 5s for a clean exit, then force.
  for _ in 1 2 3 4 5; do
    kill -0 "$PID" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$PID" 2>/dev/null; then
    echo "Didn't stop gracefully; forcing."
    kill -9 "$PID" 2>/dev/null || true
  fi
  echo "Stopped."
else
  echo "Process $PID not running; clearing stale pidfile."
fi
rm -f "$PIDFILE"
