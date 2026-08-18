#!/bin/sh
set -eu

marker=$1
state=$2
restore_tool=$3
canonical=$4
target=$5
loader_pid=$(systemctl show --property=MainPID --value plugin_loader.service 2>/dev/null || true)
owner_pid=$(cat "$marker" 2>/dev/null || true)

while [ -e "$marker" ] &&
      [ -n "$owner_pid" ] && kill -0 "$owner_pid" 2>/dev/null &&
      [ -n "$loader_pid" ] && [ "$loader_pid" != 0 ] &&
      [ "$(systemctl show --property=MainPID --value plugin_loader.service 2>/dev/null || true)" = "$loader_pid" ] &&
      systemctl is-active --quiet steam-bigpicture.scope; do
    sleep 2
done

[ -e "$marker" ] || exit 0
[ -x "$restore_tool" ] || exit 1

attempt=1
while [ "$attempt" -le 3 ]; do
    if "$restore_tool" "$marker" "$state" "$canonical" "$target"; then
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 1
done
exit 1
