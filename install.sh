#!/bin/sh
# SPDX-License-Identifier: MIT
# RK-Enhanced + stable Decky installer for ROCKNIX.

set -eu

DECKY_REPOSITORY="SteamDeckHomebrew/decky-loader"
RKE_REPOSITORY="mrdidit/RK-Enhanced"
STORAGE_ROOT="/storage"
HOMEBREW_DIR="${STORAGE_ROOT}/homebrew"
SERVICES_DIR="${HOMEBREW_DIR}/services"
PLUGINS_DIR="${HOMEBREW_DIR}/plugins"
BACKUP_ROOT="${HOMEBREW_DIR}/plugin-backups"
SERVICE_FILE="${STORAGE_ROOT}/.config/system.d/plugin_loader.service"
PLUGIN_LOADER_UNIT="plugin_loader.service"
RECOVERY_LOCK_PATH="/run/lock/rk-enhanced-plugin-loader-recovery.lock"
RECOVERY_MARKER_PATH="/run/rk-enhanced-plugin-loader-recovery.active"
maintenance_active=0

if [ "$(id -u)" -ne 0 ]; then
    echo "RK-Enhanced installer must run as root." >&2
    exit 1
fi

for command in curl flock jq timeout unzip sha256sum systemctl; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        echo "Required command is missing: ${command}" >&2
        exit 1
    fi
done

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

systemctl_bounded() {
    timeout 5 systemctl "$@"
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
    if ! printf '{"action":"install","pid":%s,"service":"%s","started_at":%s}\n' \
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

work_dir="$(mktemp -d /tmp/rk-enhanced-install.XXXXXX)"
cleanup_install() {
    result=$?
    trap - EXIT INT TERM
    end_plugin_loader_maintenance
    rm -rf "${work_dir}"
    exit "${result}"
}
trap cleanup_install EXIT INT TERM

echo "Reading stable Decky release metadata..."
decky_metadata="${work_dir}/decky.json"
curl -fL "https://api.github.com/repos/${DECKY_REPOSITORY}/releases/latest" -o "${decky_metadata}"
decky_version="$(jq -r '.tag_name' "${decky_metadata}")"
decky_url="$(jq -r '.assets[] | select(.name == "PluginLoader") | .browser_download_url' "${decky_metadata}")"
decky_digest="$(jq -r '.assets[] | select(.name == "PluginLoader") | .digest // empty' "${decky_metadata}")"

if [ -z "${decky_version}" ] || [ "${decky_version}" = "null" ] || [ -z "${decky_url}" ]; then
    echo "Could not resolve the latest stable Decky release." >&2
    exit 1
fi

echo "Downloading Decky ${decky_version}..."
curl -fL "${decky_url}" -o "${work_dir}/PluginLoader"
if [ -n "${decky_digest}" ]; then
    expected="${decky_digest#sha256:}"
    actual="$(sha256sum "${work_dir}/PluginLoader" | cut -d' ' -f1)"
    if [ "${actual}" != "${expected}" ]; then
        echo "Decky checksum verification failed." >&2
        exit 1
    fi
fi

echo "Reading RK-Enhanced release metadata..."
rke_metadata="${work_dir}/rke.json"
# GitHub's /releases/latest endpoint excludes pre-releases. RK-Enhanced is
# currently distributed as a pre-release, so deliberately select the newest
# published release from the ordered releases list.
curl -fL "https://api.github.com/repos/${RKE_REPOSITORY}/releases?per_page=1" -o "${rke_metadata}"
rke_version="$(jq -r '.[0].tag_name' "${rke_metadata}")"
rke_url="$(jq -r '.[0].assets[] | select(.name == "RK-Enhanced.zip") | .browser_download_url' "${rke_metadata}")"
rke_digest="$(jq -r '.[0].assets[] | select(.name == "RK-Enhanced.zip") | .digest // empty' "${rke_metadata}")"

if [ -z "${rke_version}" ] || [ "${rke_version}" = "null" ] || [ -z "${rke_url}" ]; then
    echo "Could not find RK-Enhanced.zip in the latest GitHub release." >&2
    exit 1
fi

echo "Downloading RK-Enhanced ${rke_version}..."
curl -fL "${rke_url}" -o "${work_dir}/RK-Enhanced.zip"
if [ -n "${rke_digest}" ]; then
    expected="${rke_digest#sha256:}"
    actual="$(sha256sum "${work_dir}/RK-Enhanced.zip" | cut -d' ' -f1)"
    if [ "${actual}" != "${expected}" ]; then
        echo "RK-Enhanced checksum verification failed." >&2
        exit 1
    fi
fi
unzip -q "${work_dir}/RK-Enhanced.zip" -d "${work_dir}/plugin"
if [ ! -f "${work_dir}/plugin/RK-Enhanced/plugin.json" ] || \
   [ ! -f "${work_dir}/plugin/RK-Enhanced/main.py" ] || \
   [ ! -f "${work_dir}/plugin/RK-Enhanced/charging.py" ] || \
   [ ! -f "${work_dir}/plugin/RK-Enhanced/runtime-restore.py" ] || \
   [ ! -f "${work_dir}/plugin/RK-Enhanced/runtime-restore-guard.sh" ] || \
   [ ! -f "${work_dir}/plugin/RK-Enhanced/dist/index.js" ]; then
    echo "RK-Enhanced release has an invalid plugin layout." >&2
    exit 1
fi

if grep -q 'rgb\.py' "${work_dir}/plugin/RK-Enhanced/main.py" && \
   [ ! -f "${work_dir}/plugin/RK-Enhanced/rgb.py" ]; then
    echo "RK-Enhanced release is missing its RGB backend." >&2
    exit 1
fi
if grep -q 'plugin_loader_recovery\.py' \
   "${work_dir}/plugin/RK-Enhanced/main.py" && \
   [ ! -f "${work_dir}/plugin/RK-Enhanced/plugin_loader_recovery.py" ]; then
    echo "RK-Enhanced release is missing its PluginLoader recovery helper." >&2
    exit 1
fi

mkdir -p "${SERVICES_DIR}" "${PLUGINS_DIR}" "${BACKUP_ROOT}" "$(dirname "${SERVICE_FILE}")"
touch "${STORAGE_ROOT}/.steam/steam/.cef-enable-remote-debugging" 2>/dev/null || true

echo "Stopping Decky cleanly..."
if ! begin_plugin_loader_maintenance; then
    echo "Another PluginLoader maintenance action is running." >&2
    exit 1
fi
if ! stop_plugin_loader_bounded; then
    echo "Decky did not stop within the bounded timeout." >&2
    exit 1
fi

if [ -f "${SERVICES_DIR}/PluginLoader" ]; then
    cp "${SERVICES_DIR}/PluginLoader" "${SERVICES_DIR}/PluginLoader.rollback"
fi
if [ -d "${PLUGINS_DIR}/RK-Enhanced" ]; then
    plugin_backup="${BACKUP_ROOT}/RK-Enhanced-before-${rke_version}-$(date +%Y%m%d-%H%M%S)"
    mv "${PLUGINS_DIR}/RK-Enhanced" "${plugin_backup}"
fi

cp "${work_dir}/PluginLoader" "${SERVICES_DIR}/PluginLoader"
chmod 755 "${SERVICES_DIR}/PluginLoader"
printf '%s\n' "${decky_version}" > "${SERVICES_DIR}/.loader.version"
mv "${work_dir}/plugin/RK-Enhanced" "${PLUGINS_DIR}/RK-Enhanced"
chmod 755 "${PLUGINS_DIR}/RK-Enhanced/updater.sh" \
    "${PLUGINS_DIR}/RK-Enhanced/runtime-restore.py" \
    "${PLUGINS_DIR}/RK-Enhanced/runtime-restore-guard.sh"
if [ -f "${PLUGINS_DIR}/RK-Enhanced/plugin_loader_recovery.py" ]; then
    chmod 755 "${PLUGINS_DIR}/RK-Enhanced/plugin_loader_recovery.py"
fi

cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=SteamDeck Plugin Loader
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Restart=always
KillMode=control-group
TimeoutStopSec=15
ExecStart=${SERVICES_DIR}/PluginLoader
WorkingDirectory=${SERVICES_DIR}
Environment=UNPRIVILEGED_PATH=${HOMEBREW_DIR}
Environment=PRIVILEGED_PATH=${HOMEBREW_DIR}
Environment=PLUGIN_PATH=${PLUGINS_DIR}
Environment=LOG_LEVEL=INFO

[Install]
WantedBy=multi-user.target
EOF

systemctl_bounded daemon-reload
systemctl_bounded enable "${PLUGIN_LOADER_UNIT}" >/dev/null
systemctl_bounded start "${PLUGIN_LOADER_UNIT}"

if ! wait_for_plugin_loader_start 15; then
    echo "Decky failed to start. Rollback files were preserved in ${SERVICES_DIR} and ${PLUGINS_DIR}." >&2
    exit 1
fi

mkdir -p "${HOMEBREW_DIR}/settings/RK-Enhanced"
printf '%s\n' "${rke_version}" > "${HOMEBREW_DIR}/settings/RK-Enhanced/installed-version.txt"
end_plugin_loader_maintenance

echo "Installed Decky ${decky_version} and RK-Enhanced ${rke_version}."
