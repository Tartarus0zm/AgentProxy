#!/usr/bin/env bash
# start.sh - Start the Claude Code API Proxy server (nohup, with strict pre-checks).
#
# Pre-flight check strategy (strict, avoids false negatives/positives):
#   1) Read PID file → if its PID is alive AND looks like our proxy process
#      (matches 'proxy.py'), refuse to start.
#   2) Even if the PID file is missing or stale, run a 'ps'-based scan to
#      detect any 'python … proxy.py …' process owned by the current user.
#   3) Additionally check whether the chosen port is already in use by some
#      other process (lsof / nc / bash /dev/tcp fallback).
#   4) Only after launching successfully (process still alive after a short
#      grace period) do we write the pid file.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PORT="${PROXY_PORT:-8080}"
CONFIG="${PROXY_CONFIG:-config.json}"
LOG_DIR="${PROXY_LOG_DIR:-}"

usage() {
    cat <<EOF
Usage: $(basename "$0") [--port PORT] [--config CONFIG] [--log-dir DIR]

Options:
  -p, --port PORT      Port to listen on (default: 8080)
  -c, --config CONFIG  Path to config file (default: config.json)
  -l, --log-dir DIR    Directory for rotating logs (default: <project>/log)
  -h, --help           Show this help

Environment variables PROXY_PORT, PROXY_CONFIG, PROXY_LOG_DIR are also honored.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--port)    PORT="$2"; shift 2 ;;
        --port=*)     PORT="${1#*=}"; shift ;;
        -c|--config)  CONFIG="$2"; shift 2 ;;
        --config=*)   CONFIG="${1#*=}"; shift ;;
        -l|--log-dir) LOG_DIR="$2"; shift 2 ;;
        --log-dir=*)  LOG_DIR="${1#*=}"; shift ;;
        -h|--help)    usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
    esac
done

# Resolve config / log dir paths
if [[ "$CONFIG" != /* ]]; then
    CONFIG="$PROJECT_ROOT/$CONFIG"
fi
if [[ ! -f "$CONFIG" ]]; then
    echo "Error: config file not found: $CONFIG" >&2
    exit 1
fi

EFFECTIVE_LOG_DIR="${LOG_DIR:-$PROJECT_ROOT/log}"
if [[ "$EFFECTIVE_LOG_DIR" != /* ]]; then
    EFFECTIVE_LOG_DIR="$PROJECT_ROOT/$EFFECTIVE_LOG_DIR"
fi
mkdir -p "$EFFECTIVE_LOG_DIR"

OUT_FILE="$EFFECTIVE_LOG_DIR/proxy.out"
ERR_FILE="$EFFECTIVE_LOG_DIR/proxy.err"
LOG_FILE="$EFFECTIVE_LOG_DIR/proxy.log"
PID_FILE="$EFFECTIVE_LOG_DIR/proxy.pid"

PROXY_PY_PATH="$PROJECT_ROOT/proxy.py"

# ─────────────────────────────────────────────────────────────────────────────
# Helper: classify a process by its argv. Echoes "yes" iff the process exists
# AND its command line references our proxy.py file path (or filename).
# ─────────────────────────────────────────────────────────────────────────────
is_proxy_process() {
    local pid="$1"
    [[ -z "$pid" ]] && return 1
    kill -0 "$pid" 2>/dev/null || return 1

    # Read command line. On macOS: 'ps -o command='; on Linux: 'ps -o args='.
    local cmdline
    cmdline="$(ps -o command= -p "$pid" 2>/dev/null || ps -o args= -p "$pid" 2>/dev/null || true)"
    [[ -z "$cmdline" ]] && return 1

    # Must reference proxy.py — accept either full path or just 'proxy.py'.
    if [[ "$cmdline" == *"$PROXY_PY_PATH"* ]] || [[ "$cmdline" == *"proxy.py"* ]]; then
        return 0
    fi
    return 1
}

# Echo a one-line human-readable status (state + start time + cmdline)
process_status_line() {
    local pid="$1"
    # state etime command (portable-ish across BSD/Linux)
    local row
    row="$(ps -o pid=,state=,etime=,command= -p "$pid" 2>/dev/null || true)"
    if [[ -z "$row" ]]; then
        echo "  (process info unavailable for PID $pid)"
        return
    fi
    echo "  $row"
}

# Detect running proxy processes via 'ps' (independent of pid file).
# Echoes one PID per line for any matching process owned by the current user.
scan_proxy_processes_by_ps() {
    local self_pid="$$"
    # -ax: all processes; -o pid=,command=: minimal columns; user filter via -u
    # 'whoami' to keep it to current user (avoids matching other users on shared host).
    local user
    user="$(id -un)"

    # Use grep -F for the unique full path to avoid catching unrelated 'proxy.py' on the system
    # but ALSO fall back to plain 'proxy.py' to detect renamed paths.
    ps -axo pid=,user=,command= 2>/dev/null \
        | awk -v u="$user" -v me="$self_pid" -v path="$PROXY_PY_PATH" '
            {
                pid = $1
                if (pid == me) next
                # Reconstruct user and command (col 2 is user, rest is command)
                usr = $2
                cmd = ""
                for (i = 3; i <= NF; i++) cmd = (cmd ? cmd " " : "") $i
                if (usr != u) next
                if (index(cmd, path) || index(cmd, "proxy.py")) print pid
            }'
}

# Check whether $PORT is already bound by SOMEONE (any process).
port_in_use() {
    local port="$1"
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 && return 0 || return 1
    fi
    if command -v nc >/dev/null 2>&1; then
        # -z: scan only, no I/O; -w 1: 1s timeout
        nc -z -w 1 127.0.0.1 "$port" >/dev/null 2>&1 && return 0 || return 1
    fi
    # Bash /dev/tcp fallback
    (echo >"/dev/tcp/127.0.0.1/$port") >/dev/null 2>&1 && return 0 || return 1
}

port_holder_info() {
    local port="$1"
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | tail -n +1
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight: detect existing proxy instance
# ─────────────────────────────────────────────────────────────────────────────

EXISTING_PID=""
DETECT_SOURCE=""

# Step 1: trust the pid file first
if [[ -f "$PID_FILE" ]]; then
    file_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$file_pid" ]] && is_proxy_process "$file_pid"; then
        EXISTING_PID="$file_pid"
        DETECT_SOURCE="pid file ($PID_FILE)"
    elif [[ -n "$file_pid" ]] && kill -0 "$file_pid" 2>/dev/null; then
        # PID is alive but is NOT our proxy → pid file is wrong/stale
        echo "Warning: pid file references PID $file_pid which is alive but" >&2
        echo "         not a proxy.py process. Treating pid file as stale." >&2
    fi
fi

# Step 2: regardless of pid file, double-check with ps
if [[ -z "$EXISTING_PID" ]]; then
    while IFS= read -r ps_pid; do
        [[ -z "$ps_pid" ]] && continue
        if is_proxy_process "$ps_pid"; then
            EXISTING_PID="$ps_pid"
            DETECT_SOURCE="ps scan"
            break
        fi
    done < <(scan_proxy_processes_by_ps || true)
fi

if [[ -n "$EXISTING_PID" ]]; then
    echo "Proxy already running — refusing to start a second instance."
    echo "  detected via: $DETECT_SOURCE"
    echo "  PID         : $EXISTING_PID"
    echo "  status      :"
    process_status_line "$EXISTING_PID"
    echo
    echo "Use './bin/stop.sh' to stop it, or './bin/reload_config.sh' to reload config."
    exit 0
fi

# Stale pid file with no live process? Clean up before continuing.
if [[ -f "$PID_FILE" ]]; then
    rm -f "$PID_FILE"
fi

# Step 3: port-in-use check (different process may already hold the port)
if port_in_use "$PORT"; then
    echo "Error: port $PORT is already in use by another process." >&2
    holder="$(port_holder_info "$PORT" || true)"
    if [[ -n "$holder" ]]; then
        echo "Holder details:" >&2
        echo "$holder" | sed 's/^/  /' >&2
    fi
    exit 1
fi

# ─────────────────────────────────────────────────────────────────────────────
# Launch
# ─────────────────────────────────────────────────────────────────────────────

PYTHON_BIN="${PYTHON:-python3}"

cd "$PROJECT_ROOT"
echo "Starting Claude Code API Proxy (nohup)"
echo "  python : $PYTHON_BIN"
echo "  port   : $PORT"
echo "  config : $CONFIG"
echo "  logdir : $EFFECTIVE_LOG_DIR"
echo "  stdout : $OUT_FILE"
echo "  stderr : $ERR_FILE"
echo "  pidfile: $PID_FILE"
echo

CMD=("$PYTHON_BIN" "$PROXY_PY_PATH" --port "$PORT" --config "$CONFIG" --log-dir "$EFFECTIVE_LOG_DIR")

# Launch in background with nohup, capturing stdout/stderr separately.
# Truncate (overwrite) previous content on each start instead of appending.
nohup "${CMD[@]}" >"$OUT_FILE" 2>"$ERR_FILE" </dev/null &
NEW_PID=$!

# Grace period for the process to actually bind/init.
# Only write the pid file AFTER we've verified it is alive AND is our proxy.
GRACE_SECS=2
slept=0
healthy=0
while (( slept < GRACE_SECS )); do
    sleep 1
    slept=$((slept + 1))
    if is_proxy_process "$NEW_PID"; then
        healthy=1
        break
    fi
done

if [[ "$healthy" -eq 1 ]]; then
    echo "$NEW_PID" >"$PID_FILE"
    echo "Proxy started successfully."
    echo "  PID    : $NEW_PID"
    echo "  status :"
    process_status_line "$NEW_PID"
    echo
    echo "Tail logs with:"
    echo "  tail -f $LOG_FILE"
    echo "  tail -f $OUT_FILE"
    echo "  tail -f $ERR_FILE"
    exit 0
else
    echo "Error: proxy failed to start (process not running after ${GRACE_SECS}s)." >&2
    echo "       Check $ERR_FILE for details." >&2
    # Do NOT write the pid file on failure.
    exit 1
fi
