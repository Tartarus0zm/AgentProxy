#!/usr/bin/env bash
# restart.sh - Stop the proxy (if running) and start it again.
#
# Accepts a UNION of start.sh and stop.sh flags:
#   start.sh : --port, --config, --log-dir
#   stop.sh  : --log-dir, --timeout, --force, --all
#
# Flags forwarded as appropriate; --log-dir is shared by both.
#
# Usage:
#   ./bin/restart.sh [--port PORT] [--config CONFIG] [--log-dir DIR]
#                    [--timeout SECONDS] [--force] [--all]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

START_SH="$SCRIPT_DIR/start.sh"
STOP_SH="$SCRIPT_DIR/stop.sh"

if [[ ! -x "$START_SH" ]]; then
    echo "Error: $START_SH not found or not executable" >&2
    exit 1
fi
if [[ ! -x "$STOP_SH" ]]; then
    echo "Error: $STOP_SH not found or not executable" >&2
    exit 1
fi

# Args destined for start.sh / stop.sh respectively
START_ARGS=()
STOP_ARGS=()

usage() {
    cat <<EOF
Usage: $(basename "$0") [start.sh + stop.sh options]

Start.sh options (forwarded to start):
  -p, --port PORT       Port to listen on (default: 8080)
  -c, --config CONFIG   Path to config file (default: config.json)
  -l, --log-dir DIR     Directory for rotating logs (shared with stop)

Stop.sh options (forwarded to stop):
  -t, --timeout SECS    Seconds to wait for graceful shutdown (default: 10)
  -f, --force           Skip SIGTERM and send SIGKILL immediately
  -a, --all             Stop ALL detected proxy processes (not just one)

Common:
  -h, --help            Show this help

Environment variables PROXY_PORT, PROXY_CONFIG, PROXY_LOG_DIR are also honored.
EOF
}

# Parse flags. --log-dir goes to BOTH start and stop.
while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--port)
            START_ARGS+=(--port "$2"); shift 2 ;;
        --port=*)
            START_ARGS+=("--port=${1#*=}"); shift ;;
        -c|--config)
            START_ARGS+=(--config "$2"); shift 2 ;;
        --config=*)
            START_ARGS+=("--config=${1#*=}"); shift ;;
        -l|--log-dir)
            START_ARGS+=(--log-dir "$2"); STOP_ARGS+=(--log-dir "$2"); shift 2 ;;
        --log-dir=*)
            START_ARGS+=("--log-dir=${1#*=}"); STOP_ARGS+=("--log-dir=${1#*=}"); shift ;;
        -t|--timeout)
            STOP_ARGS+=(--timeout "$2"); shift 2 ;;
        --timeout=*)
            STOP_ARGS+=("--timeout=${1#*=}"); shift ;;
        -f|--force)
            STOP_ARGS+=(--force); shift ;;
        -a|--all)
            STOP_ARGS+=(--all); shift ;;
        -h|--help)
            usage; exit 0 ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 1 ;;
    esac
done

echo "===== Restart: stopping current proxy ====="
# Don't abort the restart if stop reports nothing-to-stop or partial issues;
# still attempt to start. But surface non-zero rc for visibility.
set +e
"$STOP_SH" ${STOP_ARGS[@]+"${STOP_ARGS[@]}"}
STOP_RC=$?
set -e
if [[ "$STOP_RC" -ne 0 ]]; then
    echo "Warning: stop.sh exited with code $STOP_RC (continuing to start anyway)" >&2
fi

echo
echo "===== Restart: starting proxy ====="
exec "$START_SH" ${START_ARGS[@]+"${START_ARGS[@]}"}
