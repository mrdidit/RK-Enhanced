#!/bin/sh
set -eu

marker=$1
canonical=$2
target=$3
loader_pid=$(systemctl show --property=MainPID --value plugin_loader.service 2>/dev/null || true)

while [ -e "$marker" ] &&
      [ -n "$loader_pid" ] && [ "$loader_pid" != 0 ] &&
      [ "$(systemctl show --property=MainPID --value plugin_loader.service 2>/dev/null || true)" = "$loader_pid" ] &&
      systemctl is-active --quiet steam-bigpicture.scope; do
    sleep 2
done

[ -e "$marker" ] || exit 0
[ -f "$canonical" ] || exit 1
cp "$canonical" "$target"
rm -f "$marker"

if [ "$(get_setting cooling.profile 2>/dev/null || true)" = custom ]; then
    systemctl restart fancontrol.service
fi
