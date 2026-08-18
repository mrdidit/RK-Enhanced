#!/bin/sh
# Detached RK-Enhanced updater. This script must run outside PluginLoader's cgroup.

set -eu

RKE_REPOSITORY="mrdidit/RK-Enhanced"
PLUGINS_DIR="/storage/homebrew/plugins"
PLUGIN_DIR="${PLUGINS_DIR}/RK-Enhanced"
BACKUP_ROOT="/storage/homebrew/plugin-backups"
STATUS_FILE="/storage/homebrew/settings/RK-Enhanced/update-status.txt"
INSTALLED_VERSION_FILE="/storage/homebrew/settings/RK-Enhanced/installed-version.txt"
STEAM_COMMAND="/usr/bin/start_steam_arm64.sh"
STEAM_DESKTOP="/storage/.local/share/applications/Steam.desktop"

mkdir -p "$(dirname "${STATUS_FILE}")" "${BACKUP_ROOT}"

write_status() {
    printf '%s\n' "$1" > "${STATUS_FILE}"
}

relaunch_steam() {
    # Stopping Steam's gamescope scope can leave ROCKNIX's Sway session in
    # transition. The native Steam launcher reads its output geometry from
    # Sway, so wait until that query works before invoking it.
    systemctl start essway.service >/dev/null 2>&1 || true
    attempt=0
    while [ "${attempt}" -lt 15 ]; do
        if swaymsg -t get_outputs 2>/dev/null | jq -e \
            'any(.[]; .focused == true and .current_mode.width > 0 and .current_mode.height > 0)' \
            >/dev/null 2>&1; then
            break
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    if [ "${attempt}" -ge 15 ]; then
        return 1
    fi
    relaunch_id="$(date +%s)"
    systemd-run --unit="rk-enhanced-steam-relaunch-${relaunch_id}" --collect \
        "${STEAM_COMMAND}" "${STEAM_DESKTOP}" steam >/dev/null 2>&1
}

for command in curl jq unzip sha256sum systemctl systemd-run swaymsg; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        write_status "Update failed: missing ${command}"
        exit 1
    fi
done

work_dir="$(mktemp -d /tmp/rk-enhanced-update.XXXXXX)"
backup_dir=""
steam_was_active=0
plugin_moved=0

cleanup_failure() {
    result=$?
    trap - EXIT INT TERM
    if [ "${result}" -ne 0 ]; then
        write_status "Update failed; restoring the previous installation"
        if [ "${plugin_moved}" -eq 1 ] && [ -n "${backup_dir}" ] && [ -d "${backup_dir}" ]; then
            rm -rf "${PLUGIN_DIR}"
            mv "${backup_dir}" "${PLUGIN_DIR}"
        fi
        systemctl start plugin_loader.service >/dev/null 2>&1 || true
        if [ "${steam_was_active}" -eq 1 ]; then
            relaunch_steam
        fi
    fi
    rm -rf "${work_dir}"
    exit "${result}"
}
trap cleanup_failure EXIT INT TERM

write_status "Downloading the latest RK-Enhanced release…"
metadata="${work_dir}/releases.json"
curl -fL "https://api.github.com/repos/${RKE_REPOSITORY}/releases?per_page=10" -o "${metadata}"
version="$(jq -r '[.[] | select(.draft == false) | . as $release | $release.assets[] | select(.name == "RK-Enhanced.zip") | {version: $release.tag_name, url: .browser_download_url, digest: (.digest // "")}] | first | .version // empty' "${metadata}")"
url="$(jq -r '[.[] | select(.draft == false) | .assets[] | select(.name == "RK-Enhanced.zip") | .browser_download_url] | first // empty' "${metadata}")"
digest="$(jq -r '[.[] | select(.draft == false) | .assets[] | select(.name == "RK-Enhanced.zip") | (.digest // "")] | first // empty' "${metadata}")"

if [ -z "${version}" ] || [ -z "${url}" ]; then
    write_status "Update failed: no RK-Enhanced release asset found"
    exit 1
fi

curl -fL "${url}" -o "${work_dir}/RK-Enhanced.zip"
if [ -n "${digest}" ]; then
    expected="${digest#sha256:}"
    actual="$(sha256sum "${work_dir}/RK-Enhanced.zip" | cut -d' ' -f1)"
    if [ "${actual}" != "${expected}" ]; then
        write_status "Update failed: release checksum mismatch"
        exit 1
    fi
fi

unzip -q "${work_dir}/RK-Enhanced.zip" -d "${work_dir}/release"
staged="${work_dir}/release/RK-Enhanced"
if [ ! -f "${staged}/plugin.json" ] || [ ! -f "${staged}/main.py" ] || \
   [ ! -f "${staged}/dist/index.js" ] || [ ! -f "${staged}/updater.sh" ]; then
    write_status "Update failed: invalid release layout"
    exit 1
fi

if systemctl is-active --quiet steam-bigpicture.scope; then
    steam_was_active=1
fi

write_status "Installing ${version}; Steam is restarting…"
systemctl stop steam-bigpicture.scope >/dev/null 2>&1 || true
systemctl stop plugin_loader.service >/dev/null 2>&1 || true
systemctl kill --kill-who=all --signal=SIGKILL plugin_loader.service >/dev/null 2>&1 || true

backup_dir="${BACKUP_ROOT}/RK-Enhanced-before-${version}-$(date +%Y%m%d-%H%M%S)"
if [ -d "${PLUGIN_DIR}" ]; then
    mv "${PLUGIN_DIR}" "${backup_dir}"
    plugin_moved=1
fi
mv "${staged}" "${PLUGIN_DIR}"
chmod 755 "${PLUGIN_DIR}/updater.sh"

systemctl start plugin_loader.service
printf '%s\n' "${version}" > "${INSTALLED_VERSION_FILE}"
if [ "${steam_was_active}" -eq 1 ]; then
    write_status "Installed ${version}; relaunching Steam…"
    if ! relaunch_steam; then
        write_status "Installed ${version}, but Steam could not be relaunched automatically"
    fi
else
    write_status "Installed ${version}"
fi

trap - EXIT INT TERM
rm -rf "${work_dir}"
exit 0
