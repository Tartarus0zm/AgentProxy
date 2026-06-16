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

# Shared helpers: is_proxy_process, scan_proxy_processes_by_ps,
# process_status_line, pid_from_file, locate_proxy_pids.
# Matches by absolute path OR (cmdline contains 'proxy.py' AND cwd == PROJECT_ROOT).
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_proc_lib.sh"

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
PID_FILE_HAD_VALUE=0
PID_FILE_VALUE=""

if [[ -f "$PID_FILE" ]]; then
    PID_FILE_VALUE="$(cat "$PID_FILE" 2>/dev/null || true)"
    PID_FILE_HAD_VALUE=1
    if [[ -n "$PID_FILE_VALUE" ]] && is_proxy_process "$PID_FILE_VALUE"; then
        echo "PID file points to running proxy: $PID_FILE_VALUE"
        TARGETS+=("$PID_FILE_VALUE")
    elif [[ -n "$PID_FILE_VALUE" ]] && kill -0 "$PID_FILE_VALUE" 2>/dev/null; then
        echo "PID file present but PID $PID_FILE_VALUE is alive yet NOT a proxy.py process —"
        echo "         treating pid file as stale; will fall back to ps scan."
    else
        echo "PID file present but PID '${PID_FILE_VALUE:-<empty>}' is not running —"
        echo "         falling back to ps scan to locate any live proxy."
    fi
else
    echo "No pid file at $PID_FILE — falling back to ps scan."
fi

# ─────────────────────────────────────────────────────────────────────────────
# 2) Fall back to ps scan (always run if no target yet)
# ─────────────────────────────────────────────────────────────────────────────
if [[ "${#TARGETS[@]}" -eq 0 ]]; then
    echo "Scanning processes via ps for proxy.py …"
    SCAN_TARGETS=()
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        SCAN_TARGETS+=("$line")
    done < <(scan_proxy_processes_by_ps || true)

    if [[ "${#SCAN_TARGETS[@]}" -eq 0 ]]; then
        echo "No running proxy.py process found."
        # Clean up any stray pid file
        if [[ -f "$PID_FILE" ]]; then
            rm -f "$PID_FILE"
            echo "Removed stale pid file: $PID_FILE"
        fi
        exit 0
    fi

    echo "Recovered via ps scan: PID(s) ${SCAN_TARGETS[*]}"

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
