#!/bin/bash
# Start the build server in the background, recording its PID so stop.sh can
# cleanly kill it (handy when you can't Ctrl-C, e.g. over PiKVM).
#
#   ./dev/start.sh            # bind to the LAN on the default port
#   HOST=127.0.0.1 ./dev/start.sh   # localhost only
#   PORT=9000 ./dev/start.sh
#
# Server stdout/stderr go to dev/server.log; follow with:  ./dev/logs.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PIDFILE="$HERE/server.pid"
LOGFILE="$HERE/server.log"

HOST="${HOST:-0.0.0.0}"     # LAN by default; set HOST=127.0.0.1 for localhost
PORT="${PORT:-8712}"

cd "$ROOT"

# Already running? Don't start a second one.
if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "Server already running (pid $(cat "$PIDFILE")) on port $PORT."
  echo "Use ./dev/restart.sh to restart, or ./dev/stop.sh to stop."
  exit 0
fi

# Prefer the venv's uvicorn if present, else whatever's on PATH.
if [[ -x "$ROOT/.venv/bin/uvicorn" ]]; then
  UVICORN="$ROOT/.venv/bin/uvicorn"
else
  UVICORN="uvicorn"
fi

echo "Starting server on $HOST:$PORT ..."
# nohup + & so it survives this shell; record the PID.
nohup "$UVICORN" app.server:app --host "$HOST" --port "$PORT" \
  >"$LOGFILE" 2>&1 &
echo $! > "$PIDFILE"
sleep 1

if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "Started (pid $(cat "$PIDFILE")). Logs: $LOGFILE"
  echo "Open: http://<this-mac>:$PORT/   (follow logs: ./dev/logs.sh)"
else
  echo "Server failed to start. Last log lines:"
  tail -n 20 "$LOGFILE" || true
  rm -f "$PIDFILE"
  exit 1
fi
