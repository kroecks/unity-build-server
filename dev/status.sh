#!/bin/bash
# Is the server running? Quick check without hunting for the process.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$HERE/server.pid"
if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "RUNNING (pid $(cat "$PIDFILE"))."
else
  echo "Not running."
  if pgrep -f "uvicorn app.server:app" >/dev/null 2>&1; then
    echo "  (but a stray uvicorn exists: pids $(pgrep -f 'uvicorn app.server:app' | tr '\n' ' '))"
  fi
fi
