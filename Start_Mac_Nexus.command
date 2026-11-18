#!/bin/zsh
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

HOST="127.0.0.1"
PORT="8790"
APP_URL="http://${HOST}:${PORT}"
HEALTH_URL="${APP_URL}/api/health"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="${SCRIPT_DIR:-/Users/egorlabutin/Projects/mac-nexus}"
STATE_DIR="$HOME/.local/state/mac-nexus"
LOG_FILE="$STATE_DIR/server.log"

if /usr/bin/curl -sf "$HEALTH_URL" >/dev/null 2>&1; then
  echo "Mac Nexus already running at ${APP_URL} — opening browser..."
  /usr/bin/open "$APP_URL"
  exit 0
fi

mkdir -p "$STATE_DIR"

PYTHON_BIN=""
for candidate in \
  "/opt/anaconda3/bin/python3" \
  "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3" \
  "/opt/homebrew/bin/python3" \
  "/usr/bin/python3"; do
  if [[ -x "$candidate" ]]; then
    PYTHON_BIN="$candidate"
    break
  fi
done

if [[ -z "$PYTHON_BIN" ]]; then
  echo "Error: No suitable python3 interpreter found." >&2
  exit 1
fi

echo "Starting Mac Nexus (${PYTHON_BIN}) at ${APP_URL}..."
nohup /usr/bin/env PYTHONPATH="${PROJECT_ROOT}/src" \
  "$PYTHON_BIN" -m mac_nexus.server --host "$HOST" --port "$PORT" \
  >> "$LOG_FILE" 2>&1 </dev/null &

attempt=0
while (( attempt < 50 )); do
  if /usr/bin/curl -sf "$HEALTH_URL" >/dev/null 2>&1; then
    echo "Mac Nexus is online at ${APP_URL} — opening browser..."
    /usr/bin/open "$APP_URL"
    exit 0
  fi
  /bin/sleep 0.1
  attempt=$((attempt + 1))
done

echo "Error: Mac Nexus server did not respond within 5s. Check ${LOG_FILE}" >&2
exit 1
