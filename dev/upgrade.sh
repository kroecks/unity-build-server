#!/bin/bash
# Safe in-place upgrade of the build server. Use this instead of hand-unzipping,
# so config is never clobbered and the restart + health check are automatic.
#
# Two modes:
#   ./dev/upgrade.sh                 # upgrade from a new release zip you downloaded
#        (set ZIP=/path/to/unity-build-server.zip, or it looks in ~/Downloads)
#   ./dev/upgrade.sh --git           # if the server dir is a git repo: pull + restart
#
# What it protects: config/config.json and config/projects.json are NEVER
# overwritten (they're backed up and restored). The venv is left alone.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

BACKUP="$ROOT/.upgrade-backup"
CONFIG="$ROOT/config/config.json"
REGISTRY="$ROOT/config/projects.json"

echo "== Unity build server upgrade =="

# 0. A running build is fine: builds run under a detached runner and the
#    restarted server reattaches to it (and to the queue). Just say so.
PORT="${PORT:-8712}"
if command -v curl >/dev/null; then
  RUNNING="$(curl -fsS "http://127.0.0.1:$PORT/status" 2>/dev/null \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); c=d.get("current"); print(c["project_name"] if c else "")' 2>/dev/null || true)"
  if [[ -n "$RUNNING" ]]; then
    echo "A build of $RUNNING is running; it keeps running through the upgrade and"
    echo "the new server picks it up again (it runs a snapshot of build.sh, so"
    echo "script changes apply from the next build)."
  fi
fi

# 1. Back up the irreplaceable files (config + registry + history).
mkdir -p "$BACKUP"
for f in "$CONFIG" "$REGISTRY"; do
  [[ -f "$f" ]] && cp "$f" "$BACKUP/$(basename "$f")" && echo "backed up $(basename "$f")"
done
# history.json lives under log_dir; back it up if it's the default location.
if [[ -f "$ROOT/logs/history.json" ]]; then
  cp "$ROOT/logs/history.json" "$BACKUP/history.json"
fi

# 2. Stop the server. Detect whether it's launchd-managed or nohup-managed and
#    use the right mechanism — otherwise a launchd KeepAlive would fight us by
#    respawning the old code while we swap files.
LABEL="com.voxelmage.buildserver"
DOMAIN="gui/$(id -u)"
LAUNCHD_MANAGED=0
if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  LAUNCHD_MANAGED=1
  echo "stopping server (launchd)..."
  # bootout removes it from launchd entirely so KeepAlive can't respawn mid-swap.
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
else
  echo "stopping server (nohup)..."
  "$HERE/stop.sh" || true
fi

# 3. Apply the new code.
if [[ "${1:-}" == "--git" ]]; then
  echo "git pull..."
  git pull --ff-only || { echo "git pull failed (not ff? resolve by hand)."; }
else
  ZIP="${ZIP:-$HOME/Downloads/unity-build-server.zip}"
  [[ -f "$ZIP" ]] || { echo "No zip at $ZIP. Set ZIP=/path/to/zip and retry."; exit 1; }
  echo "applying $ZIP (config/ and .venv preserved)..."
  # Unzip into a temp dir, then rsync everything EXCEPT config + venv over the top.
  TMP="$(mktemp -d)"
  unzip -q "$ZIP" -d "$TMP"
  SRC="$TMP/unity-build-server"
  [[ -d "$SRC" ]] || SRC="$TMP"
  rsync -a --delete \
    --exclude 'config/config.json' \
    --exclude 'config/projects.json' \
    --exclude '.venv/' \
    --exclude 'logs/' \
    --exclude 'dev/server.pid' \
    --exclude 'dev/server.log' \
    "$SRC"/ "$ROOT"/
  rm -rf "$TMP"
fi

# 4. Restore the protected files (in case the archive carried stale copies).
for f in config.json projects.json; do
  [[ -f "$BACKUP/$f" ]] && cp "$BACKUP/$f" "$ROOT/config/$f"
done
[[ -f "$BACKUP/history.json" ]] && mkdir -p "$ROOT/logs" && cp "$BACKUP/history.json" "$ROOT/logs/history.json"

# 5. Update deps if requirements changed, then start + health-check.
if [[ -x "$ROOT/.venv/bin/pip" ]]; then
  echo "syncing dependencies..."
  "$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt" || true
fi

echo "starting server..."
if [[ "$LAUNCHD_MANAGED" == "1" ]]; then
  # Re-bootstrap the (unchanged) plist and kickstart it.
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null || true
  launchctl kickstart -k "$DOMAIN/$LABEL" 2>/dev/null || true
else
  "$HERE/start.sh"
fi

# 6. Health check: hit /status locally.
sleep 2
PORT="${PORT:-8712}"
if command -v curl >/dev/null && curl -fsS "http://127.0.0.1:$PORT/status" >/dev/null 2>&1; then
  echo "OK — server is up and answering on :$PORT"
else
  echo "WARNING: server did not answer /status. Check ./dev/logs.sh"
fi
echo "Done. (Backups kept in $BACKUP)"