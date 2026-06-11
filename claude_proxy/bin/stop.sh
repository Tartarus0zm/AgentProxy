#!/usr/bin/env bash
# stop.sh - Gracefully stop the Claude Code API Proxy server.
#
# Detection strategy (mirrors start.sh):
#   1) Read PID file → if alive AND command-line references proxy.py, use it.
#   2) If pid file missing OR pid is dead/wrong, fall back to 'ps' scan of
#      the current user's processes for python … proxy.py …
#   3) Send SIGTERM, wait (default 10s), escalate to SIGKILL on timeout.
#   4) Always clean up the pid file when done.
#
# Usage:
#   ./bin/stop.sh [--log-dir DIR] [--timeout SECONDS] [--force] [--all]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

LOG_DIR="${PROXY_LOG_DIR:-}"
TIMEOUT=10
FORCE=0
KILL_ALL=0

usage() {
    cat <<EOF
Usage: $(basename "$0") [--log-dir DIR] [--timeout SECONDS] [--force] [--all]

Options:
  -l, --log-dir DIR     Log directory holding proxy.pid (default: <project>/log)
  -t, --timeout SECS    Seconds to wait for graceful shutdown (default: 10)
  -f, --force           Skip SIGTERM and send SIGKILL immediately
  -a, --all             If multiple proxy processes are detected via ps scan,
                        stop them ALL (default: stop only the first match)
  -h, --help            Show this help

Environment variable PROXY_LOG_DIR is also honored.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -l|--log-dir) LOG_DIR="$2"; shift 2 ;;
        --log-dir=*)  LOG_DIR="${1#*=}"; shift ;;
        -t|--timeout) TIMEOUT="$2"; shift 2 ;;
        --timeout=*)  TIMEOUT="${1#*=}"; shift ;;
        -f|--force)   FORCE=1; shift ;;
        -a|--all)     KILL_ALL=1; shift ;;
        -h|--help)    usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
    esac
done

EFFECTIVE_LOG_DIR="${LOG_DIR:-$PROJECT_ROOT/log}"
if [[ "$EFFECTIVE_LOG_DIR" != /* ]]; then
    EFFECTIVE_LOG_DIR="$PROJECT_ROOT/$EFFECTIVE_LOG_DIR"
fi
PID_FILE="$EFFECTIVE_LOG_DIR/proxy.pid"
PROXY_PY_PATH="$PROJECT_ROOT/proxy.py"

# ─────────────────────────────────────────────────────────────────────────────
# Helpers (same semantics as start.sh)
# ─────────────────────────────────────────────────────────────────────────────
is_proxy_process() {
    local pid="$1"
    [[ -z "$pid" ]] && return 1
    kill -0 "$pid" 2>/dev/null || return 1
    local cmdline
    cmdline="$(ps -o command= -p "$pid" 2>/dev/null || ps -o args= -p "$pid" 2>/dev/null || true)"
    [[ -z "$cmdline" ]] && return 1
    if [[ "$cmdline" == *"$PROXY_PY_PATH"* ]] || [[ "$cmdline" == *"proxy.py"* ]]; then
        return 0
    fi
    return 1
}

process_status_line() {
    local pid="$1"
    local row
    row="$(ps -o pid=,state=,etime=,command= -p "$pid" 2>/dev/null || true)"
    if [[ -z "$row" ]]; then
        echo "  (process info unavailable for PID $pid)"
    else
        echo "  $row"
    fi
}

scan_proxy_processes_by_ps() {
    local self_pid="$$"
    local user
    user="$(id -un)"
    ps -axo pid=,user=,command= 2>/dev/null \
        | awk -v u="$user" -v me="$self_pid" -v path="$PROXY_PY_PATH" '
            {
                pid = $1
                if (pid == me) next
                usr = $2
                cmd = ""
                for (i = 3; i <= NF; i++) cmd = (cmd ? cmd " " : "") $i
                if (usr != u) next
                if (index(cmd, path) || index(cmd, "proxy.py")) print pid
            }'
}

stop_pid() {
    local pid="$1"
    echo "  stopping PID $pid"
    process_status_line "$pid"

    if [[ "$FORCE" -eq 1 ]]; then
        echo "  signal: SIGKILL (forced)"
        kill -KILL "$pid" 2>/dev/null || true
    else
        echo "  signal: SIGTERM (waiting up to ${TIMEOUT}s)"
        kill -TERM "$pid" 2>/dev/null || true
        local waited=0
        while kill -0 "$pid" 2>/dev/null; do
            if (( waited >= TIMEOUT )); then
                echo "  graceful shutdown timed out; sending SIGKILL"
                kill -KILL "$pid" 2>/dev/null || true
                break
            fi
            sleep 1
            waited=$((waited + 1))
        done
    fi

    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
        echo "  ERROR: PID $pid still alive after stop attempt." >&2
        return 1
    fi
    echo "  stopped PID $pid"
    return 0
}

# ─────────────────────────────────────────────────────────────────────────────
# 1) Try PID file
# ─────────────────────────────────────────────────────────────────────────────
TARGETS=()

if [[ -f "$PID_FILE" ]]; then
    file_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$file_pid" ]] && is_proxy_process "$file_pid"; then
        echo "PID file points to running proxy: $file_pid"
        TARGETS+=("$file_pid")
    else
        echo "PID file present but pid is missing/stale (file pid: '${file_pid:-<empty>}')."
    fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# 2) Fall back to ps scan (always run if no target yet)
# ─────────────────────────────────────────────────────────────────────────────
if [[ "${#TARGETS[@]}" -eq 0 ]]; then
    echo "Scanning processes via ps for proxy.py …"
    PS_PIDS=()
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        PS_PIDS+=("$line")
    done < <(scan_proxy_processes_by_ps || true)

    # Filter strictly (must be our proxy process)
    SCAN_TARGETS=()
    for p in "${PS_PIDS[@]:-}"; do
        [[ -z "$p" ]] && continue
        if is_proxy_process "$p"; then
            SCAN_TARGETS+=("$p")
        fi
    done

    if [[ "${#SCAN_TARGETS[@]}" -eq 0 ]]; then
        echo "No running proxy.py process found."
        # Clean up any stray pid file
        if [[ -f "$PID_FILE" ]]; then
            rm -f "$PID_FILE"
            echo "Removed stale pid file: $PID_FILE"
        fi
        exit 0
    fi

    if [[ "${#SCAN_TARGETS[@]}" -gt 1 && "$KILL_ALL" -eq 0 ]]; then
        echo "Warning: multiple proxy processes detected: ${SCAN_TARGETS[*]}"
        echo "         stopping ONLY the first (use --all to stop them all)"
        TARGETS+=("${SCAN_TARGETS[0]}")
    else
        TARGETS+=("${SCAN_TARGETS[@]}")
    fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# 3) Stop targets
# ─────────────────────────────────────────────────────────────────────────────
echo
echo "Stopping Claude Code API Proxy"
echo "  targets: ${TARGETS[*]}"
echo

OVERALL_RC=0
for pid in "${TARGETS[@]}"; do
    stop_pid "$pid" || OVERALL_RC=1
    echo
done

# ─────────────────────────────────────────────────────────────────────────────
# 4) Always clean up pid file at the end
# ─────────────────────────────────────────────────────────────────────────────
if [[ -f "$PID_FILE" ]]; then
    rm -f "$PID_FILE"
    echo "Removed pid file: $PID_FILE"
fi

if [[ "$OVERALL_RC" -eq 0 ]]; then
    echo "Done."
else
    echo "Done (with errors)." >&2
fi
exit "$OVERALL_RC"
