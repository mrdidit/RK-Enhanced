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
SERVICE_FILE="${STORAGE_ROOT}/.config/system.d/plugin_loader.service"

if [ "$(id -u)" -ne 0 ]; then
    echo "RK-Enhanced installer must run as root." >&2
    exit 1
fi

for command in curl jq unzip sha256sum systemctl; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        echo "Required command is missing: ${command}" >&2
        exit 1
    fi
done

work_dir="$(mktemp -d /tmp/rk-enhanced-install.XXXXXX)"
trap 'rm -rf "${work_dir}"' EXIT INT TERM

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
   [ ! -f "${work_dir}/plugin/RK-Enhanced/dist/index.js" ]; then
    echo "RK-Enhanced release has an invalid plugin layout." >&2
    exit 1
fi

mkdir -p "${SERVICES_DIR}" "${PLUGINS_DIR}" "$(dirname "${SERVICE_FILE}")"
touch "${STORAGE_ROOT}/.steam/steam/.cef-enable-remote-debugging" 2>/dev/null || true

echo "Stopping Decky cleanly..."
systemctl stop plugin_loader.service 2>/dev/null || true
# Old ROCKNIX/Decky combinations have occasionally left PluginLoader workers.
if pgrep PluginLoader >/dev/null 2>&1; then
    pkill -9 PluginLoader || true
fi

if [ -f "${SERVICES_DIR}/PluginLoader" ]; then
    cp "${SERVICES_DIR}/PluginLoader" "${SERVICES_DIR}/PluginLoader.rollback"
fi
if [ -d "${PLUGINS_DIR}/RK-Enhanced" ]; then
    rm -rf "${PLUGINS_DIR}/RK-Enhanced.rollback"
    mv "${PLUGINS_DIR}/RK-Enhanced" "${PLUGINS_DIR}/RK-Enhanced.rollback"
fi

cp "${work_dir}/PluginLoader" "${SERVICES_DIR}/PluginLoader"
chmod 755 "${SERVICES_DIR}/PluginLoader"
printf '%s\n' "${decky_version}" > "${SERVICES_DIR}/.loader.version"
mv "${work_dir}/plugin/RK-Enhanced" "${PLUGINS_DIR}/RK-Enhanced"

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

systemctl daemon-reload
systemctl enable plugin_loader.service >/dev/null
systemctl start plugin_loader.service

if ! systemctl is-active --quiet plugin_loader.service; then
    echo "Decky failed to start. Rollback files were preserved in ${SERVICES_DIR} and ${PLUGINS_DIR}." >&2
    exit 1
fi

echo "Installed Decky ${decky_version} and RK-Enhanced ${rke_version}."
