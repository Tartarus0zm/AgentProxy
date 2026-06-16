#!/usr/bin/env bash
# reload_config.sh - Trigger a hot-reload of the proxy configuration.
#
# Usage:
#   ./bin/reload_config.sh [--port PORT]
#
# Defaults (mirroring proxy.py):
#   port: 8080

set -euo pipefail

PORT="${PROXY_PORT:-8080}"
HOST="${PROXY_HOST:-127.0.0.1}"

usage() {
    cat <<EOF
Usage: $(basename "$0") [--port PORT]

Options:
  -p, --port PORT  Port the proxy is listening on (default: 8080)
  -H, --host HOST  Host the proxy is bound to       (default: 127.0.0.1)
  -h, --help       Show this help

Environment variables PROXY_PORT and PROXY_HOST are also honored.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--port)
            PORT="$2"
            shift 2
            ;;
        --port=*)
            PORT="${1#*=}"
            shift
            ;;
        -H|--host)
            HOST="$2"
            shift 2
            ;;
        --host=*)
            HOST="${1#*=}"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

URL="http://${HOST}:${PORT}/admin/reload"
echo "Reloading proxy config via: $URL"

# Capture status code separately so failures surface clearly.
HTTP_CODE=$(curl -sS -o /tmp/proxy_reload_response.$$.json -w "%{http_code}" "$URL" || true)
RESPONSE_FILE="/tmp/proxy_reload_response.$$.json"

if [[ -z "$HTTP_CODE" || "$HTTP_CODE" == "000" ]]; then
    echo "Error: could not reach proxy at $URL" >&2
    rm -f "$RESPONSE_FILE"
    exit 2
fi

echo "HTTP $HTTP_CODE"
if command -v python3 >/dev/null 2>&1; then
    python3 -m json.tool "$RESPONSE_FILE" || cat "$RESPONSE_FILE"
else
    cat "$RESPONSE_FILE"
fi
echo

rm -f "$RESPONSE_FILE"

# Non-2xx => exit non-zero so callers can detect failures.
if [[ "$HTTP_CODE" =~ ^2[0-9][0-9]$ ]]; then
    exit 0
else
    exit 1
fi
