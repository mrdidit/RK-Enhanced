#!/bin/sh
# Detached RK-Enhanced updater. This script must run outside PluginLoader's cgroup.

set -eu

RKE_REPOSITORY="mrdidit/RK-Enhanced"
PLUGINS_DIR="/storage/homebrew/plugins"
PLUGIN_DIR="${PLUGINS_DIR}/RK-Enhanced"
BACKUP_ROOT="/storage/homebrew/plugin-backups"
STATUS_FILE="/storage/homebrew/settings/RK-Enhanced/update-status.txt"
INSTALLED_VERSION_FILE="/storage/homebrew/settings/RK-Enhanced/installed-version.txt"
PLUGIN_LOADER_UNIT="plugin_loader.service"
RECOVERY_LOCK_PATH="/run/lock/rk-enhanced-plugin-loader-recovery.lock"
RECOVERY_MARKER_PATH="/run/rk-enhanced-plugin-loader-recovery.active"
requested_version="${1:-}"
maintenance_active=0

mkdir -p "$(dirname "${STATUS_FILE}")" "${BACKUP_ROOT}"

write_status() {
    printf '%s\n' "$1" > "${STATUS_FILE}"
}

systemctl_bounded() {
    timeout 5 systemctl "$@"
}

wait_for_plugin_loader_stop() {
    rke_stop_deadline=$(( $(date +%s) + $1 ))
    while [ "$(date +%s)" -lt "${rke_stop_deadline}" ]; do
        rke_stop_state="$(systemctl_bounded show --property=ActiveState --value \
            "${PLUGIN_LOADER_UNIT}" 2>/dev/null || true)"
        case "${rke_stop_state}" in
            inactive|failed)
                return 0
                ;;
        esac
        sleep 1
    done
    return 1
}

stop_plugin_loader_bounded() {
    systemctl_bounded stop --no-block \
        "${PLUGIN_LOADER_UNIT}" >/dev/null 2>&1 || true
    if wait_for_plugin_loader_stop 15; then
        return 0
    fi
    systemctl_bounded kill --kill-who=all --signal=SIGTERM \
        "${PLUGIN_LOADER_UNIT}" >/dev/null 2>&1 || true
    if wait_for_plugin_loader_stop 3; then
        return 0
    fi
    systemctl_bounded kill --kill-who=all --signal=SIGKILL \
        "${PLUGIN_LOADER_UNIT}" >/dev/null 2>&1 || true
    wait_for_plugin_loader_stop 3
}

wait_for_plugin_loader_start() {
    rke_start_deadline=$(( $(date +%s) + $1 ))
    while [ "$(date +%s)" -lt "${rke_start_deadline}" ]; do
        if systemctl_bounded is-active --quiet "${PLUGIN_LOADER_UNIT}"; then
            return 0
        fi
        sleep 1
    done
    return 1
}

begin_plugin_loader_maintenance() {
    mkdir -p "$(dirname "${RECOVERY_LOCK_PATH}")"
    exec 9>"${RECOVERY_LOCK_PATH}"
    if ! chmod 600 "${RECOVERY_LOCK_PATH}"; then
        exec 9>&-
        return 1
    fi
    if ! flock -n 9; then
        exec 9>&-
        return 1
    fi
    rke_maintenance_tmp="$(mktemp \
        /run/rk-enhanced-plugin-loader-recovery.active.XXXXXX)" || {
        flock -u 9 || true
        exec 9>&-
        return 1
    }
    if ! chmod 600 "${rke_maintenance_tmp}"; then
        rm -f "${rke_maintenance_tmp}"
        flock -u 9 || true
        exec 9>&-
        return 1
    fi
    if ! printf '{"action":"update","pid":%s,"service":"%s","started_at":%s}\n' \
        "$$" "${PLUGIN_LOADER_UNIT}" "$(date +%s)" > "${rke_maintenance_tmp}" || \
       ! mv "${rke_maintenance_tmp}" "${RECOVERY_MARKER_PATH}"; then
        rm -f "${rke_maintenance_tmp}"
        flock -u 9 || true
        exec 9>&-
        return 1
    fi
    maintenance_active=1
}

end_plugin_loader_maintenance() {
    if [ "${maintenance_active}" -eq 1 ]; then
        rm -f "${RECOVERY_MARKER_PATH}"
        flock -u 9 || true
        exec 9>&-
        maintenance_active=0
    fi
}

for command in curl flock jq timeout unzip sha256sum systemctl; do
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
        systemctl_bounded start \
            "${PLUGIN_LOADER_UNIT}" >/dev/null 2>&1 || true
    fi
    end_plugin_loader_maintenance
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
if grep -q 'plugin_loader_recovery\.py' "${staged}/main.py" && \
   [ ! -f "${staged}/plugin_loader_recovery.py" ]; then
    write_status "Update failed: release is missing its PluginLoader recovery helper"
    exit 1
fi
if [ "${preserve_updater}" -eq 1 ]; then
    cp "$0" "${staged}/updater.sh"
    chmod 755 "${staged}/updater.sh"
fi

write_status "Installing ${version}; Decky is reloading…"
if ! begin_plugin_loader_maintenance; then
    write_status "Update failed: another PluginLoader maintenance action is running"
    exit 1
fi
if ! stop_plugin_loader_bounded; then
    write_status "Update failed: Decky did not stop within the bounded timeout"
    exit 1
fi

backup_dir="${BACKUP_ROOT}/RK-Enhanced-before-${version}-$(date +%Y%m%d-%H%M%S)"
if [ -d "${PLUGIN_DIR}" ]; then
    mv "${PLUGIN_DIR}" "${backup_dir}"
    plugin_moved=1
fi
mv "${staged}" "${PLUGIN_DIR}"
chmod 755 "${PLUGIN_DIR}/updater.sh" \
    "${PLUGIN_DIR}/runtime-restore.py" \
    "${PLUGIN_DIR}/runtime-restore-guard.sh"
if [ -f "${PLUGIN_DIR}/plugin_loader_recovery.py" ]; then
    chmod 755 "${PLUGIN_DIR}/plugin_loader_recovery.py"
fi

systemctl_bounded start "${PLUGIN_LOADER_UNIT}"
if ! wait_for_plugin_loader_start 15; then
    write_status "Update failed: Decky did not start within the bounded timeout"
    exit 1
fi
printf '%s\n' "${version}" > "${INSTALLED_VERSION_FILE}"
write_status "Installed ${version}"
end_plugin_loader_maintenance

trap - EXIT INT TERM
rm -rf "${work_dir}"
exit 0
