#!/bin/sh
# Detached RK-Enhanced updater. This script must run outside PluginLoader's cgroup.

set -eu

RKE_REPOSITORY="mrdidit/RK-Enhanced"
PLUGINS_DIR="/storage/homebrew/plugins"
PLUGIN_DIR="${PLUGINS_DIR}/RK-Enhanced"
BACKUP_ROOT="/storage/homebrew/plugin-backups"
STATUS_FILE="/storage/homebrew/settings/RK-Enhanced/update-status.txt"
INSTALLED_VERSION_FILE="/storage/homebrew/settings/RK-Enhanced/installed-version.txt"
requested_version="${1:-}"

mkdir -p "$(dirname "${STATUS_FILE}")" "${BACKUP_ROOT}"

write_status() {
    printf '%s\n' "$1" > "${STATUS_FILE}"
}

for command in curl jq unzip sha256sum systemctl; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        write_status "Update failed: missing ${command}"
        exit 1
    fi
done

work_dir="$(mktemp -d /tmp/rk-enhanced-update.XXXXXX)"
backup_dir=""
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
    fi
    rm -rf "${work_dir}"
    exit "${result}"
}
trap cleanup_failure EXIT INT TERM

if [ -n "${requested_version}" ]; then
    write_status "Downloading RK-Enhanced ${requested_version}…"
else
    write_status "Downloading the latest RK-Enhanced release…"
fi
metadata="${work_dir}/releases.json"
curl -fL "https://api.github.com/repos/${RKE_REPOSITORY}/releases?per_page=10" -o "${metadata}"
release_filter='[.[] | select(.draft == false) | . as $release | $release.assets[] | select(.name == "RK-Enhanced.zip") | {version: $release.tag_name, url: .browser_download_url, digest: (.digest // "")} | select($requested == "" or .version == $requested)] | first'
version="$(jq -r --arg requested "${requested_version}" "${release_filter} | .version // empty" "${metadata}")"
url="$(jq -r --arg requested "${requested_version}" "${release_filter} | .url // empty" "${metadata}")"
digest="$(jq -r --arg requested "${requested_version}" "${release_filter} | .digest // empty" "${metadata}")"

if [ -z "${version}" ] || [ -z "${url}" ]; then
    write_status "Update failed: no RK-Enhanced release asset found"
    exit 1
fi

# Keep the current detached updater when moving backwards through the
# published release order. Older plugin code can then safely return to the
# latest release without restoring obsolete lifecycle behavior.
preserve_updater=0
installed_version="$(cat "${INSTALLED_VERSION_FILE}" 2>/dev/null || true)"
if [ -n "${requested_version}" ] && [ -n "${installed_version}" ]; then
    installed_index="$(jq -r --arg version "${installed_version}" \
        '[.[] | select(.draft == false) | select(any(.assets[]; .name == "RK-Enhanced.zip")) | .tag_name] | index($version) // -1' \
        "${metadata}")"
    requested_index="$(jq -r --arg version "${requested_version}" \
        '[.[] | select(.draft == false) | select(any(.assets[]; .name == "RK-Enhanced.zip")) | .tag_name] | index($version) // -1' \
        "${metadata}")"
    if [ "${installed_index}" -ge 0 ] && [ "${requested_index}" -gt "${installed_index}" ]; then
        preserve_updater=1
    fi
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
   [ ! -f "${staged}/charging.py" ] || \
   [ ! -f "${staged}/runtime-restore.py" ] || \
   [ ! -f "${staged}/runtime-restore-guard.sh" ] || \
   [ ! -f "${staged}/dist/index.js" ] || [ ! -f "${staged}/updater.sh" ]; then
    write_status "Update failed: invalid release layout"
    exit 1
fi
if grep -q 'rgb\.py' "${staged}/main.py" && [ ! -f "${staged}/rgb.py" ]; then
    write_status "Update failed: release is missing its RGB backend"
    exit 1
fi
if [ "${preserve_updater}" -eq 1 ]; then
    cp "$0" "${staged}/updater.sh"
    chmod 755 "${staged}/updater.sh"
fi

write_status "Installing ${version}; Decky is reloading…"
systemctl stop plugin_loader.service >/dev/null 2>&1 || true
systemctl kill --kill-who=all --signal=SIGKILL plugin_loader.service >/dev/null 2>&1 || true

backup_dir="${BACKUP_ROOT}/RK-Enhanced-before-${version}-$(date +%Y%m%d-%H%M%S)"
if [ -d "${PLUGIN_DIR}" ]; then
    mv "${PLUGIN_DIR}" "${backup_dir}"
    plugin_moved=1
fi
mv "${staged}" "${PLUGIN_DIR}"
chmod 755 "${PLUGIN_DIR}/updater.sh" \
    "${PLUGIN_DIR}/runtime-restore.py" \
    "${PLUGIN_DIR}/runtime-restore-guard.sh"

systemctl start plugin_loader.service
printf '%s\n' "${version}" > "${INSTALLED_VERSION_FILE}"
write_status "Installed ${version}"

trap - EXIT INT TERM
rm -rf "${work_dir}"
exit 0
