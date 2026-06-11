#!/usr/bin/env bash
# status.sh - Show the current status of the Claude Code API Proxy.
#
# Reports:
#   - Process info: PID (from pid file & ps), state, uptime, %CPU, %MEM, RSS, command line
#   - Listening port (from cmdline + verified via lsof / nc / /dev/tcp)
#   - Config path, log directory, log file sizes
#   - HTTP health: GET /v1/models (status + model count)
#   - Recent tail of proxy.log / proxy.err
#
# Usage:
#   ./bin/status.sh [--log-dir DIR] [--port PORT] [--host HOST] [--no-http] [--tail N]
#
# Exit codes:
#   0 = proxy running and healthy (HTTP /v1/models returned 2xx)
#   1 = proxy process running but HTTP unhealthy
#   2 = proxy process not found

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

LOG_DIR="${PROXY_LOG_DIR:-}"
PORT_ARG=""                 # if user overrides, we use this for HTTP probe
HOST="${PROXY_HOST:-127.0.0.1}"
DO_HTTP=1
TAIL_N=10

usage() {
    cat <<EOF
Usage: $(basename "$0") [--log-dir DIR] [--port PORT] [--host HOST] [--no-http] [--tail N]

Options:
  -l, --log-dir DIR   Log directory holding proxy.pid (default: <project>/log)
  -p, --port PORT     Port for HTTP health probe (default: auto-detect from cmdline, fallback 8080)
  -H, --host HOST     Host for HTTP probe (default: 127.0.0.1)
      --no-http       Skip HTTP /v1/models probe
  -t, --tail N        Show last N lines of proxy.log / proxy.err (default: 10, 0 = skip)
  -h, --help          Show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -l|--log-dir) LOG_DIR="$2"; shift 2 ;;
        --log-dir=*)  LOG_DIR="${1#*=}"; shift ;;
        -p|--port)    PORT_ARG="$2"; shift 2 ;;
        --port=*)     PORT_ARG="${1#*=}"; shift ;;
        -H|--host)    HOST="$2"; shift 2 ;;
        --host=*)     HOST="${1#*=}"; shift ;;
        --no-http)    DO_HTTP=0; shift ;;
        -t|--tail)    TAIL_N="$2"; shift 2 ;;
        --tail=*)     TAIL_N="${1#*=}"; shift ;;
        -h|--help)    usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

EFFECTIVE_LOG_DIR="${LOG_DIR:-$PROJECT_ROOT/log}"
if [[ "$EFFECTIVE_LOG_DIR" != /* ]]; then
    EFFECTIVE_LOG_DIR="$PROJECT_ROOT/$EFFECTIVE_LOG_DIR"
fi
PID_FILE="$EFFECTIVE_LOG_DIR/proxy.pid"
LOG_FILE="$EFFECTIVE_LOG_DIR/proxy.log"
OUT_FILE="$EFFECTIVE_LOG_DIR/proxy.out"
ERR_FILE="$EFFECTIVE_LOG_DIR/proxy.err"
PROXY_PY_PATH="$PROJECT_ROOT/proxy.py"

# ─── Helpers (mirror start.sh / stop.sh) ─────────────────────────────────────
is_proxy_process() {
    local pid="$1"
    [[ -z "$pid" ]] && return 1
    kill -0 "$pid" 2>/dev/null || return 1
    local cmdline
    cmdline="$(ps -o command= -p "$pid" 2>/dev/null || ps -o args= -p "$pid" 2>/dev/null || true)"
    [[ -z "$cmdline" ]] && return 1
    [[ "$cmdline" == *"$PROXY_PY_PATH"* || "$cmdline" == *"proxy.py"* ]]
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

human_size() {
    # bytes -> human readable
    local b="$1"
    if (( b < 1024 ));        then echo "${b}B"
    elif (( b < 1048576 ));   then printf "%.1fK" "$(echo "$b/1024" | bc -l)"
    elif (( b < 1073741824 )); then printf "%.1fM" "$(echo "$b/1048576" | bc -l)"
    else                          printf "%.2fG" "$(echo "$b/1073741824" | bc -l)"
    fi
}

file_size_bytes() {
    [[ -f "$1" ]] || { echo 0; return; }
    # macOS uses 'stat -f%z', Linux uses 'stat -c%s'
    stat -f%z "$1" 2>/dev/null || stat -c%s "$1" 2>/dev/null || echo 0
}

extract_arg_value() {
    # Extract value of a --flag from a command line string.
    # Supports '--flag value' and '--flag=value'.
    local cmd="$1" flag="$2"
    # Try '--flag=VAL'
    local v
    v="$(echo "$cmd" | sed -nE "s/.*${flag}=([^ ]+).*/\1/p")"
    if [[ -n "$v" ]]; then echo "$v"; return; fi
    # Try '--flag VAL'
    v="$(echo "$cmd" | sed -nE "s/.*${flag}[[:space:]]+([^ ]+).*/\1/p")"
    echo "$v"
}

port_holder_brief() {
    local port="$1"
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null
    fi
}

probe_port() {
    local port="$1"
    if command -v nc >/dev/null 2>&1; then
        nc -z -w 1 "$HOST" "$port" >/dev/null 2>&1 && return 0 || return 1
    fi
    (echo >"/dev/tcp/$HOST/$port") >/dev/null 2>&1 && return 0 || return 1
}

# ─── 1) Locate process ───────────────────────────────────────────────────────
PID=""
PID_SRC=""

if [[ -f "$PID_FILE" ]]; then
    fp="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$fp" ]] && is_proxy_process "$fp"; then
        PID="$fp"; PID_SRC="pid file"
    fi
fi

if [[ -z "$PID" ]]; then
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        if is_proxy_process "$line"; then
            PID="$line"; PID_SRC="ps scan"
            break
        fi
    done < <(scan_proxy_processes_by_ps || true)
fi

# ─── 2) Header ────────────────────────────────────────────────────────────────
echo "========================================================="
echo " Claude Code API Proxy — Status"
echo "========================================================="
echo " project    : $PROJECT_ROOT"
echo " log dir    : $EFFECTIVE_LOG_DIR"
echo " pid file   : $PID_FILE $([[ -f "$PID_FILE" ]] && echo "(exists)" || echo "(missing)")"
echo

if [[ -z "$PID" ]]; then
    echo " state      : NOT RUNNING"
    echo "---------------------------------------------------------"
    if [[ -f "$PID_FILE" ]]; then
        echo " Note: stale pid file present. Run './bin/stop.sh' or remove it manually."
    fi
    exit 2
fi

echo " state      : RUNNING"
echo " PID        : $PID  (detected via $PID_SRC)"

# ─── 3) Process details ──────────────────────────────────────────────────────
PS_ROW="$(ps -o pid=,user=,state=,etime=,%cpu=,%mem=,rss=,command= -p "$PID" 2>/dev/null || true)"
if [[ -n "$PS_ROW" ]]; then
    # shellcheck disable=SC2086
    set -- $PS_ROW
    P_PID="$1"; P_USER="$2"; P_STATE="$3"; P_ETIME="$4"; P_CPU="$5"; P_MEM="$6"; P_RSS="$7"
    shift 7
    P_CMD="$*"
    echo " user       : $P_USER"
    echo " state      : $P_STATE"
    echo " uptime     : $P_ETIME"
    echo " %CPU/%MEM  : ${P_CPU}% / ${P_MEM}%"
    echo " RSS        : $(human_size "$((P_RSS * 1024))")"
    echo " cmdline    : $P_CMD"
else
    P_CMD=""
fi

# ─── 4) Inferred runtime config from cmdline ─────────────────────────────────
if [[ -n "$P_CMD" ]]; then
    CFG_PORT="$(extract_arg_value "$P_CMD" "--port")"
    CFG_FILE="$(extract_arg_value "$P_CMD" "--config")"
    CFG_LOG="$(extract_arg_value "$P_CMD" "--log-dir")"
    echo
    echo " runtime config (from cmdline):"
    echo "   --port    : ${CFG_PORT:-<unknown>}"
    echo "   --config  : ${CFG_FILE:-<unknown>}"
    echo "   --log-dir : ${CFG_LOG:-<unknown>}"
fi

# Decide which port to probe
PROBE_PORT="${PORT_ARG:-${CFG_PORT:-8080}}"

# ─── 5) Port / network ───────────────────────────────────────────────────────
echo
echo " network:"
echo "   probe target : http://$HOST:$PROBE_PORT"
if probe_port "$PROBE_PORT"; then
    echo "   TCP listen   : OK"
else
    echo "   TCP listen   : NOT REACHABLE"
fi
HOLDER="$(port_holder_brief "$PROBE_PORT" 2>/dev/null || true)"
if [[ -n "$HOLDER" ]]; then
    echo "   port holders :"
    echo "$HOLDER" | sed 's/^/     /'
fi

# ─── 6) Log files ────────────────────────────────────────────────────────────
echo
echo " log files:"
for f in "$LOG_FILE" "$OUT_FILE" "$ERR_FILE"; do
    if [[ -f "$f" ]]; then
        sz="$(file_size_bytes "$f")"
        echo "   $(basename "$f")  $(human_size "$sz")  $f"
    else
        echo "   $(basename "$f")  (missing)  $f"
    fi
done
# Show rotated proxy.log.* if any
shopt -s nullglob 2>/dev/null || true
for rf in "$LOG_FILE".*; do
    sz="$(file_size_bytes "$rf")"
    echo "   $(basename "$rf")  $(human_size "$sz")"
done

# ─── 7) HTTP health probe ────────────────────────────────────────────────────
HTTP_OK=0
if [[ "$DO_HTTP" -eq 1 ]]; then
    echo
    echo " HTTP health probe:"
    if command -v curl >/dev/null 2>&1; then
        tmpf="/tmp/proxy_status_$$.json"
        code="$(curl -sS -o "$tmpf" -w "%{http_code}" -m 5 \
                 -H "x-api-key: status-probe" \
                 "http://$HOST:$PROBE_PORT/v1/models" 2>/dev/null || echo "000")"
        echo "   GET /v1/models -> HTTP $code"
        if [[ "$code" =~ ^2[0-9][0-9]$ ]]; then
            HTTP_OK=1
            if command -v python3 >/dev/null 2>&1; then
                count="$(python3 -c '
import json,sys
try:
    d=json.load(open(sys.argv[1]))
    data=d.get("data",[])
    print(len(data))
    for m in data:
        print("     -", m.get("id"), "(", m.get("display_name",""), ")")
except Exception as e:
    print("parse-error:", e)
' "$tmpf" 2>/dev/null || true)"
                if [[ -n "$count" ]]; then
                    first_line="$(echo "$count" | head -n1)"
                    echo "   model count    : $first_line"
                    echo "$count" | tail -n +2
                fi
            fi
        else
            echo "   (response body):"
            head -c 500 "$tmpf" 2>/dev/null | sed 's/^/     /'
            echo
        fi
        rm -f "$tmpf"
    else
        echo "   (curl not found; skipping)"
    fi
fi

# ─── 8) Recent log tail ──────────────────────────────────────────────────────
if [[ "$TAIL_N" -gt 0 ]]; then
    if [[ -f "$LOG_FILE" ]]; then
        echo
        echo " last $TAIL_N lines of proxy.log:"
        tail -n "$TAIL_N" "$LOG_FILE" | sed 's/^/   /'
    fi
    if [[ -s "$ERR_FILE" ]]; then
        echo
        echo " last $TAIL_N lines of proxy.err (non-empty!):"
        tail -n "$TAIL_N" "$ERR_FILE" | sed 's/^/   /'
    fi
fi

echo
echo "========================================================="

if [[ "$DO_HTTP" -eq 1 && "$HTTP_OK" -eq 0 ]]; then
    exit 1
fi
exit 0
