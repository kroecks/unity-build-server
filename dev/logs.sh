#!/bin/bash
# Follow the running server's log. (This one you CAN leave; closing it or
# killing the terminal doesn't stop the server — it's just a viewer.)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGFILE="$HERE/server.log"
[[ -f "$LOGFILE" ]] || { echo "No log yet at $LOGFILE — start the server first."; exit 1; }
tail -n 50 -f "$LOGFILE"
