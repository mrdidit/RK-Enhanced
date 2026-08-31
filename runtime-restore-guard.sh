#!/bin/sh
set -eu

marker=$1
restore_request="${marker}.restore-request"
state=$2
restore_tool=$3
canonical=$4
target=$5
owner_pid=$(cat "$marker" 2>/dev/null || true)
poll_seconds=2
inactive_limit=3
inactive_checks=0

valid_pid() {
    case "$1" in
        ''|0|*[!0-9]*) return 1 ;;
        *) return 0 ;;
    esac
}

process_start_ticks() {
    process_pid=$1
    valid_pid "$process_pid" || return 1
    process_stat=$(cat "/proc/${process_pid}/stat" 2>/dev/null) || return 1
    # Strip PID and the parenthesised comm field. The remaining field 20 is
    # Linux procfs stat field 22: the process start time in clock ticks.
    process_stat=${process_stat##*) }
    set -- $process_stat
    [ "$#" -ge 20 ] || return 1
    case "${20}" in
        ''|*[!0-9]*) return 1 ;;
        *) printf '%s\n' "${20}" ;;
    esac
}

same_process() {
    expected_pid=$1
    expected_start=$2
    valid_pid "$expected_pid" || return 1
    [ -n "$expected_start" ] || return 1
    kill -0 "$expected_pid" 2>/dev/null || return 1
    [ "$(process_start_ticks "$expected_pid" 2>/dev/null || true)" = "$expected_start" ]
}

loader_pid=$(systemctl show --property=MainPID --value plugin_loader.service 2>/dev/null || true)
owner_start=$(process_start_ticks "$owner_pid" 2>/dev/null || true)
loader_start=$(process_start_ticks "$loader_pid" 2>/dev/null || true)

while [ -e "$marker" ]; do
    # Explicit unload, owner death, and Loader replacement are authoritative.
    # They must not wait for the Steam-exit debounce.
    [ ! -e "$restore_request" ] || break
    same_process "$owner_pid" "$owner_start" || break
    current_loader_pid=$(systemctl show --property=MainPID --value plugin_loader.service 2>/dev/null || true)
    [ "$current_loader_pid" = "$loader_pid" ] || break
    same_process "$loader_pid" "$loader_start" || break

    if systemctl is-active --quiet steam-bigpicture.scope; then
        inactive_checks=0
    else
        inactive_checks=$((inactive_checks + 1))
        [ "$inactive_checks" -lt "$inactive_limit" ] || break
    fi
    sleep "$poll_seconds"
done

[ -e "$marker" ] || exit 0
[ -x "$restore_tool" ] || exit 1

attempt=1
while [ "$attempt" -le 3 ]; do
    if "$restore_tool" "$marker" "$state" "$canonical" "$target"; then
        rm -f "$restore_request"
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 1
done
exit 1
