#!/usr/bin/env bash
# _proc_lib.sh — shared process-identification helpers for start/stop/status.
#
# Required env vars (must be set by the sourcing script BEFORE 'source':
#   PROJECT_ROOT     absolute path to adapter/openai2claude
#   PROXY_PY_PATH    "$PROJECT_ROOT/proxy.py" (kept for backward compat)
#
# A process is considered "our" proxy iff it satisfies ALL of:
#   1. owned by the current user (cheap pre-filter in ps scan)
#   2. cmdline contains a python interpreter AND the substring 'proxy.py'
#   3. one of:
#        a) cmdline embeds the absolute $PROXY_PY_PATH, OR
#        b) the process current working directory equals $PROJECT_ROOT
#           (this lets us match relative-path launches such as
#            'cd <project> && python3 proxy.py' without false positives
#            on some unrelated proxy.py elsewhere on disk).
#
# This "PID file missing or stale" rescue path is the whole reason this
# library exists: stop.sh / restart.sh / status.sh can now reliably find
# and kill the right process even when the pid file is gone.

PROXY_PY_BASENAME="${PROXY_PY_BASENAME:-proxy.py}"

# Echo the cwd of $1 (PID), or empty + non-zero rc on failure.
process_cwd() {
    local pid="$1"
    [[ -z "$pid" ]] && return 1
    local cwd=""
    if command -v lsof >/dev/null 2>&1; then
        # -Fn prints fields prefixed by single-letter tags; 'n' = name.
        cwd="$(lsof -p "$pid" -a -d cwd -Fn 2>/dev/null \
                | awk '/^n/{ sub(/^n/, "", $0); print; exit }')"
    fi
    if [[ -z "$cwd" && -r "/proc/$pid/cwd" ]]; then
        cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
    fi
    [[ -n "$cwd" ]] || return 1
    printf '%s' "$cwd"
}

# Returns 0 if PID exists AND looks like our proxy process.
is_proxy_process() {
    local pid="$1"
    [[ -z "$pid" ]] && return 1
    kill -0 "$pid" 2>/dev/null || return 1
    local cmdline
    cmdline="$(ps -o command= -p "$pid" 2>/dev/null \
                || ps -o args= -p "$pid" 2>/dev/null \
                || true)"
    [[ -z "$cmdline" ]] && return 1

    # Must look like a python interpreter running proxy.py somewhere.
    case "$cmdline" in
        *python*"$PROXY_PY_BASENAME"*|*Python*"$PROXY_PY_BASENAME"*) ;;
        *) return 1 ;;
    esac

    # Strong signal: cmdline embeds the absolute project path.
    if [[ -n "${PROXY_PY_PATH:-}" && "$cmdline" == *"$PROXY_PY_PATH"* ]]; then
        return 0
    fi

    # Weaker signal: cmdline shows only 'proxy.py' (relative). In that case
    # require the process cwd to equal PROJECT_ROOT to avoid matching some
    # other unrelated proxy.py on the system.
    local cwd
    cwd="$(process_cwd "$pid" 2>/dev/null || true)"
    if [[ -n "$cwd" && -n "${PROJECT_ROOT:-}" && "$cwd" == "$PROJECT_ROOT" ]]; then
        return 0
    fi
    return 1
}

# Print a one-line "ps" status for a PID (or "(unavailable)" if gone).
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

# Echo one PID per line for any process matching is_proxy_process.
# Two-stage filter:
#   stage 1 (awk)   — same-user, cmdline contains 'proxy.py' AND 'python'
#   stage 2 (shell) — is_proxy_process() re-validates each candidate
#                     (this is where cwd == PROJECT_ROOT is enforced for
#                     relative-path launches).
scan_proxy_processes_by_ps() {
    local self_pid="$$"
    local user
    user="$(id -un)"
    local raw_pids
    raw_pids="$(
        ps -axo pid=,user=,command= 2>/dev/null \
            | awk -v u="$user" -v me="$self_pid" -v base="$PROXY_PY_BASENAME" '
                {
                    pid = $1
                    if (pid == me) next
                    usr = $2
                    cmd = ""
                    for (i = 3; i <= NF; i++) cmd = (cmd ? cmd " " : "") $i
                    if (usr != u) next
                    if (index(cmd, base) == 0) next
                    if (index(cmd, "python") == 0 && index(cmd, "Python") == 0) next
                    print pid
                }'
    )"
    local p
    for p in $raw_pids; do
        [[ -z "$p" ]] && continue
        if is_proxy_process "$p"; then
            printf '%s\n' "$p"
        fi
    done
}

# Read PID from $PID_FILE; if empty/dead/wrong-process, echo nothing and
# return non-zero. Otherwise echo the PID and return 0.
pid_from_file() {
    local pid_file="$1"
    [[ -f "$pid_file" ]] || return 1
    local fp
    fp="$(cat "$pid_file" 2>/dev/null || true)"
    [[ -z "$fp" ]] && return 1
    is_proxy_process "$fp" || return 1
    printf '%s' "$fp"
}

# Locate proxy PIDs combining pid-file + ps fallback.
# Echoes lines:
#     <pid>\t<source>      where source = pid-file | ps-scan
# When KILL_ALL=1 (env var) all ps matches are returned; otherwise only
# the first.
locate_proxy_pids() {
    local pid_file="$1"
    local kill_all="${2:-0}"
    local fp
    if fp="$(pid_from_file "$pid_file" 2>/dev/null)"; then
        printf '%s\tpid-file\n' "$fp"
        return 0
    fi
    local found_any=0
    while IFS= read -r p; do
        [[ -z "$p" ]] && continue
        printf '%s\tps-scan\n' "$p"
        found_any=1
        [[ "$kill_all" -eq 1 ]] || break
    done < <(scan_proxy_processes_by_ps || true)
    [[ "$found_any" -eq 1 ]] || return 1
    return 0
}
