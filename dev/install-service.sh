#!/bin/bash
# Install the build server as a macOS LaunchAgent so it starts automatically
# and survives crashes + reboots (with auto-login on; see notes at the end).
#
#   ./dev/install-service.sh          # install + start, LAN on default port
#   HOST=127.0.0.1 PORT=9000 ./dev/install-service.sh
#
# This REPLACES the nohup/start.sh way of running. After installing, manage the
# server with ./dev/service.sh {start|stop|restart|status|logs}.
#
# IMPORTANT: run this from a Terminal on the Mac's DESKTOP (or via PiKVM's
# screen), NOT over plain SSH. `launchctl bootstrap` into the GUI session fails
# with a domain error when there's no Aqua session, which is the usual reason a
# freshly-installed agent "doesn't start".
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

LABEL="com.voxelmage.buildserver"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8712}"
CONFIG="${UBS_CONFIG:-$ROOT/config/config.json}"
LOGFILE="$HERE/server.log"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
TEMPLATE="$HERE/com.voxelmage.buildserver.plist.template"

echo "== Installing $LABEL as a LaunchAgent =="

# 0. Sanity: must be macOS, must be in a GUI session for bootstrap to work.
if [[ "$(uname)" != "Darwin" ]]; then
  echo "This installs a macOS LaunchAgent; you're not on macOS. Aborting." >&2
  exit 1
fi
# launchctl bootstrap needs an Aqua (GUI) session. Detect a plain SSH shell.
if [[ -n "${SSH_CONNECTION:-}" ]] && ! launchctl managername 2>/dev/null | grep -qi aqua; then
  echo "WARNING: you appear to be over SSH with no GUI session." >&2
  echo "  'launchctl bootstrap' will likely fail. Run this from the Mac's" >&2
  echo "  desktop (or the PiKVM screen) instead. Continuing anyway..." >&2
fi

# 1. Find uvicorn (prefer the venv).
if [[ -x "$ROOT/.venv/bin/uvicorn" ]]; then
  UVICORN="$ROOT/.venv/bin/uvicorn"
else
  UVICORN="$(command -v uvicorn || true)"
fi
if [[ -z "$UVICORN" ]]; then
  echo "Could not find uvicorn. Create the venv and install deps first:" >&2
  echo "    python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt" >&2
  exit 1
fi
echo "uvicorn: $UVICORN"

# 2. Stop the old nohup server if it's running, so we don't double-bind the port.
if [[ -f "$HERE/server.pid" ]] && kill -0 "$(cat "$HERE/server.pid")" 2>/dev/null; then
  echo "Stopping the nohup-managed server first..."
  "$HERE/stop.sh" || true
fi

# 3. Render the plist from the template.
mkdir -p "$HOME/Library/LaunchAgents"
sed \
  -e "s|__LABEL__|$LABEL|g" \
  -e "s|__UVICORN__|$UVICORN|g" \
  -e "s|__HOST__|$HOST|g" \
  -e "s|__PORT__|$PORT|g" \
  -e "s|__ROOT__|$ROOT|g" \
  -e "s|__CONFIG__|$CONFIG|g" \
  -e "s|__LOGFILE__|$LOGFILE|g" \
  "$TEMPLATE" > "$PLIST_DST"

# 4. Validate before loading — a bad plist silently refuses to load.
if ! plutil -lint "$PLIST_DST" >/dev/null; then
  echo "Generated plist failed validation. Left it at $PLIST_DST for inspection." >&2
  exit 1
fi
echo "Installed plist: $PLIST_DST"

# 5. (Re)bootstrap into the user's GUI domain.
DOMAIN="gui/$(id -u)"
# If an older copy is loaded, boot it out first (ignore errors).
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
if launchctl bootstrap "$DOMAIN" "$PLIST_DST" 2>/tmp/ubs-bootstrap.err; then
  echo "Bootstrapped into $DOMAIN."
else
  echo "launchctl bootstrap failed:" >&2
  cat /tmp/ubs-bootstrap.err >&2
  echo >&2
  echo "Most common cause: not in a GUI session (SSH). Run from the desktop." >&2
  exit 1
fi
launchctl enable "$DOMAIN/$LABEL" 2>/dev/null || true
# Kickstart so it starts right now without waiting.
launchctl kickstart -k "$DOMAIN/$LABEL" 2>/dev/null || true

sleep 2
# 6. Health check.
if curl -fsS "http://127.0.0.1:$PORT/status" >/dev/null 2>&1; then
  echo "OK — server is up on :$PORT and managed by launchd."
else
  echo "Installed, but /status didn't answer yet. Check: ./dev/service.sh logs"
fi

cat <<EOF

────────────────────────────────────────────────────────────────────────
Done. The server now starts automatically and restarts if it crashes.

To ALSO survive full reboots, enable auto-login for this user so the Mac
logs in (and the agent starts) without someone at the keyboard:

  System Settings → Users & Groups → "Automatically log in as" → $USER

And keep the Mac from sleeping:

  sudo pmset -a sleep 0 disablesleep 1

One more thing for signing to work unattended: the login keychain must be
unlocked in the auto-login session. Auto-login unlocks it automatically. If
you ever see signing fail with a keychain prompt after a reboot, run once:

  security unlock-keychain ~/Library/Keychains/login.keychain-db

Manage the service with:  ./dev/service.sh {start|stop|restart|status|logs}
Uninstall with:           ./dev/uninstall-service.sh
────────────────────────────────────────────────────────────────────────
EOF