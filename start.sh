#!/usr/bin/env bash
# One command: set up (first time only) and open the ECHO Inspection Mission.
set -euo pipefail
cd "$(dirname "$0")"
PORT="${ECHO_PORT:-8080}"
if [ ! -x .venv/bin/python ]; then
  echo "First start: creating .venv and installing requirements (once)..."
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
# Older .venvs may predate a dependency; install quietly if anything is missing.
.venv/bin/python -c "import aioquic, fastapi, uvicorn" 2>/dev/null || .venv/bin/pip install -q -r requirements.txt
URL="http://127.0.0.1:${PORT}"
( sleep 2; command -v open >/dev/null && open "$URL" || command -v xdg-open >/dev/null && xdg-open "$URL" ) >/dev/null 2>&1 &
echo "ECHO Inspection Mission -> $URL   (Ctrl+C to stop)"
ECHO_PORT="$PORT" exec .venv/bin/python dashboard/server.py
