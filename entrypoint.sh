#!/bin/sh

# Auto-generate yahoo_oauth.json from env vars if the file doesn't exist.
# The yahoo-oauth library reads credentials from this file and writes
# refreshed tokens back to it, so it must remain a writable file on disk.
OAUTH_FILE="${OAUTH_FILE:-/app/config/yahoo_oauth.json}"

if [ ! -f "$OAUTH_FILE" ]; then
  if [ -n "$YAHOO_CONSUMER_KEY" ] && [ -n "$YAHOO_CONSUMER_SECRET" ]; then
    echo "Generating $OAUTH_FILE from YAHOO_CONSUMER_KEY/YAHOO_CONSUMER_SECRET env vars"
    cat > "$OAUTH_FILE" <<EOF
{
    "consumer_key": "$YAHOO_CONSUMER_KEY",
    "consumer_secret": "$YAHOO_CONSUMER_SECRET"
}
EOF
  else
    echo "WARNING: $OAUTH_FILE not found and YAHOO_CONSUMER_KEY/YAHOO_CONSUMER_SECRET not set."
    echo "Yahoo API calls will fail until credentials are configured."
  fi
fi

python3 /app/scripts/api-server.py &
PYTHON_PID=$!

PYTHON_API_HOST="${PYTHON_API_HOST:-127.0.0.1}"
PYTHON_API_PORT="${PYTHON_API_PORT:-8766}"
PYTHON_READY_TIMEOUT="${PYTHON_READY_TIMEOUT_SECONDS:-30}"
PYTHON_READY=0
ELAPSED=0

while [ "$ELAPSED" -lt "$PYTHON_READY_TIMEOUT" ]; do
  if python3 -c "import socket; s = socket.create_connection((\"$PYTHON_API_HOST\", int(\"$PYTHON_API_PORT\")), 1); s.close()" >/dev/null 2>&1; then
    PYTHON_READY=1
    break
  fi

  if ! kill -0 "$PYTHON_PID" >/dev/null 2>&1; then
    echo "Python API exited before becoming ready"
    wait "$PYTHON_PID"
    exit 1
  fi

  sleep 1
  ELAPSED=$((ELAPSED + 1))
done

if [ "$PYTHON_READY" -ne 1 ]; then
  echo "Python API did not become ready within ${PYTHON_READY_TIMEOUT}s"
  exit 1
fi

exec node /app/mcp-apps/dist/main.js
