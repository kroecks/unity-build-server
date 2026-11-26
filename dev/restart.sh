#!/bin/bash
# Stop then start — the one you'll use most while developing (picks up code
# changes to server.py / ui.py / registry.py). Passes HOST/PORT through.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$HERE/stop.sh"
HOST="${HOST:-}" PORT="${PORT:-}" "$HERE/start.sh"
