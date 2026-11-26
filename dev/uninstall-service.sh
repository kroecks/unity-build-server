#!/bin/bash
# Remove the launchd LaunchAgent for the build server. Stops it and deletes the
# plist. (Does not touch config, logs, or the venv.) After this you can go back
# to running it manually with ./dev/start.sh.
set -euo pipefail

LABEL="com.voxelmage.buildserver"
DOMAIN="gui/$(id -u)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "Stopping and removing $LABEL ..."
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || echo "  (was not loaded)"
if [[ -f "$PLIST" ]]; then
  rm -f "$PLIST"
  echo "  removed $PLIST"
else
  echo "  no plist at $PLIST"
fi
echo "Done. The server is no longer managed by launchd."
echo "Run it manually again with: ./dev/start.sh"