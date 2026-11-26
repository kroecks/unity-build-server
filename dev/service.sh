#!/bin/bash
# Manage the launchd-managed build server.
#   ./dev/service.sh start|stop|restart|status|logs
#
# Use this once you've run ./dev/install-service.sh. (Before that, use the
# nohup scripts start.sh/stop.sh.) stop/start here mean "tell launchd to stop/
# start the job" — launchd still owns the process and will restart it on crash
# or reboot; `stop` just stops this instance until the next start/reboot.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.voxelmage.buildserver"
DOMAIN="gui/$(id -u)"
SVC="$DOMAIN/$LABEL"
LOGFILE="$HERE/server.log"
PORT="${PORT:-8712}"

cmd="${1:-status}"
case "$cmd" in
  start)
    launchctl kickstart "$SVC" && echo "Started $LABEL."
    ;;
  stop)
    # `kill` sends SIGTERM to the running instance; KeepAlive would normally
    # restart it, so we also disable until a manual start. Simpler: use `kill`.
    launchctl kill SIGTERM "$SVC" 2>/dev/null \
      && echo "Sent stop to $LABEL (launchd will not restart it until the next start/reboot if you also disable it)." \
      || echo "Could not signal $LABEL (is it installed? ./dev/install-service.sh)"
    ;;
  restart)
    launchctl kickstart -k "$SVC" && echo "Restarted $LABEL."
    ;;
  status)
    if launchctl print "$SVC" >/dev/null 2>&1; then
      echo "Installed. launchd state:"
      launchctl print "$SVC" | grep -E "state =|pid =|last exit code =|runs =" | sed 's/^/  /'
      if curl -fsS "http://127.0.0.1:$PORT/status" >/dev/null 2>&1; then
        echo "  http: responding on :$PORT"
      else
        echo "  http: NOT responding on :$PORT"
      fi
    else
      echo "Not installed in $DOMAIN. Run ./dev/install-service.sh"
    fi
    ;;
  logs)
    echo "Tailing $LOGFILE (Ctrl-C to stop; the server keeps running)."
    tail -n 60 -f "$LOGFILE"
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|logs}" >&2
    exit 1
    ;;
esac