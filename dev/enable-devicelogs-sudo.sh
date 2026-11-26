#!/bin/bash
# One-time setup so the build server can run `log collect` on an attached iOS
# device WITHOUT a password prompt. Collecting the unified log from a tethered
# device requires root; this grants passwordless sudo for ONLY that one command.
#
# Run this ONCE on the mini:   sudo ./dev/enable-devicelogs-sudo.sh
# Undo any time:               sudo rm /etc/sudoers.d/unity-build-devicelogs
#
# Security note: this permits `/usr/bin/log collect` (and its arguments) as root
# with no password, for the user you run it for. `log collect` only reads logs;
# it can't modify the system. Nothing else is granted.
set -euo pipefail

if [[ "$(id -u)" != "0" ]]; then
  echo "Run with sudo:  sudo $0" >&2
  exit 1
fi

# The user the build server runs as. Defaults to the user who invoked sudo.
TARGET_USER="${SUDO_USER:-${1:-}}"
if [[ -z "$TARGET_USER" ]]; then
  echo "Could not determine the target user. Pass it explicitly:" >&2
  echo "  sudo $0 <username>" >&2
  exit 1
fi

LOG_BIN="/usr/bin/log"
if [[ ! -x "$LOG_BIN" ]]; then
  echo "WARNING: $LOG_BIN not found/executable; using it anyway (macOS default)." >&2
fi

DROPIN="/etc/sudoers.d/unity-build-devicelogs"
# Trailing * lets the real device UDID / flags through. Only `log collect`.
RULE="$TARGET_USER ALL=(root) NOPASSWD: $LOG_BIN collect *"

TMP="$(mktemp)"
printf '%s\n' "$RULE" > "$TMP"

# Validate BEFORE installing — a bad sudoers file can lock you out of sudo.
if visudo -cf "$TMP" >/dev/null 2>&1; then
  install -m 0440 "$TMP" "$DROPIN"
  rm -f "$TMP"
  echo "Installed: $DROPIN"
  echo "  $RULE"
  echo
  echo "Verifying passwordless access as $TARGET_USER..."
  if sudo -u "$TARGET_USER" sudo -n "$LOG_BIN" collect --help >/dev/null 2>&1; then
    echo "OK — '$TARGET_USER' can now run 'sudo log collect' without a password."
  else
    echo "Installed, but the non-interactive check didn't confirm. It should still"
    echo "work for the real command; try the 'Last run logs' button."
  fi
else
  rm -f "$TMP"
  echo "Refused: the generated sudoers rule failed validation. Nothing changed." >&2
  echo "Rule was: $RULE" >&2
  exit 1
fi