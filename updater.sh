#!/bin/sh
# Detached RK-Enhanced updater. This script must run outside PluginLoader's cgroup.

set -eu

DECKY_REPOSITORY="SteamDeckHomebrew/decky-loader"
RKE_REPOSITORY="mrdidit/RK-Enhanced"
STORAGE_ROOT="${RKE_STORAGE_ROOT:-/storage}"
RUN_ROOT="${RKE_RUN_ROOT:-/run}"
PROC_ROOT="${RKE_PROC_ROOT:-/proc}"
HOMEBREW_DIR="${STORAGE_ROOT}/homebrew"
SERVICES_DIR="${HOMEBREW_DIR}/services"
PLUGINS_DIR="${HOMEBREW_DIR}/plugins"
PLUGIN_DIR="${PLUGINS_DIR}/RK-Enhanced"
PLUGIN_LOADER_PATH="${SERVICES_DIR}/PluginLoader"
PLUGIN_LOADER_VERSION_FILE="${SERVICES_DIR}/.loader.version"
BACKUP_ROOT="${HOMEBREW_DIR}/plugin-backups"
SETTINGS_DIR="${HOMEBREW_DIR}/settings/RK-Enhanced"
STATUS_FILE="${SETTINGS_DIR}/update-status.txt"
INSTALLED_VERSION_FILE="${SETTINGS_DIR}/installed-version.txt"
HEALTH_REQUEST_FILE="${SETTINGS_DIR}/install-health-request.json"
BACKEND_READY_FILE="${SETTINGS_DIR}/install-backend-ready.json"
FRONTEND_READY_FILE="${SETTINGS_DIR}/install-frontend-ready.json"
PLUGIN_LOADER_UNIT="plugin_loader.service"
RECOVERY_LOCK_PATH="${RUN_ROOT}/lock/rk-enhanced-plugin-loader-recovery.lock"
RECOVERY_MARKER_PATH="${RUN_ROOT}/rk-enhanced-plugin-loader-recovery.active"
TRANSACTION_LOCK_PATH="${RUN_ROOT}/lock/rk-enhanced-install-transaction.lock"
HEALTH_TIMEOUT="${RKE_HEALTH_TIMEOUT:-90}"
requested_version="${1:-}"
frontend_requirement="${2:-}"
maintenance_active=0
transaction_active=0
health_created=0
transaction_committed=0

case "${frontend_requirement}" in
    ""|require-frontend) ;;
    *)
        echo "Invalid updater health requirement." >&2
        exit 1
        ;;
esac

mkdir -p "$(dirname "${STATUS_FILE}")" "${BACKUP_ROOT}"

write_status() {
    printf '%s\n' "$1" > "${STATUS_FILE}"
}

systemctl_bounded() {
    timeout 5 systemctl "$@"
}

begin_install_transaction() {
    mkdir -p "$(dirname "${TRANSACTION_LOCK_PATH}")" || return 1
    exec 8>"${TRANSACTION_LOCK_PATH}"
    if ! chmod 600 "${TRANSACTION_LOCK_PATH}"; then
        exec 8>&-
        return 1
    fi
    if ! flock -n 8; then
        exec 8>&-
        return 1
    fi
    transaction_active=1
    return 0
}

end_install_transaction() {
    if [ "${transaction_active}" -eq 1 ]; then
        flock -u 8 || true
        exec 8>&-
        transaction_active=0
    fi
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
    mkdir -p "$(dirname "${RECOVERY_LOCK_PATH}")" || return 1
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
        "${RUN_ROOT}/rk-enhanced-plugin-loader-recovery.active.XXXXXX")" || {
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
    return 0
}

begin_plugin_loader_maintenance_bounded() {
    rke_maintenance_deadline=$(( $(date +%s) + $1 ))
    while [ "$(date +%s)" -lt "${rke_maintenance_deadline}" ]; do
        if begin_plugin_loader_maintenance; then
            return 0
        fi
        sleep 1
    done
    return 1
}

end_plugin_loader_maintenance() {
    if [ "${maintenance_active}" -eq 1 ]; then
        rm -f "${RECOVERY_MARKER_PATH}"
        flock -u 9 || true
        exec 9>&-
        maintenance_active=0
    fi
}

process_start_time() {
    rke_health_stat="$(cat "${PROC_ROOT}/$1/stat" 2>/dev/null)" || return 1
    rke_health_tail="${rke_health_stat##*) }"
    set -- ${rke_health_tail}
    [ "$#" -ge 20 ] || return 1
    printf '%s\n' "${20}"
}

clear_install_health() {
    rm -f "${HEALTH_REQUEST_FILE}" "${BACKEND_READY_FILE}" \
        "${FRONTEND_READY_FILE}"
}

frontend_integrity_id() {
    rke_integrity_root="$1"
    rke_integrity_index="${rke_integrity_root}/dist/index.js"
    rke_integrity_manifest="${rke_integrity_root}/dist/frontend-integrity.json"
    [ -f "${rke_integrity_index}" ] && \
        [ -f "${rke_integrity_manifest}" ] || return 1
    rke_integrity_id="$(jq -r \
        'select(.protocol == 1 and .algorithm == "sha256-normalized-v1") |
         .bundle_id // empty' "${rke_integrity_manifest}" 2>/dev/null)" || return 1
    rke_integrity_final="$(jq -r '.index_sha256 // empty' \
        "${rke_integrity_manifest}" 2>/dev/null)" || return 1
    rke_integrity_digest="${rke_integrity_id#rke-frontend-sha256-v1:}"
    [ "${rke_integrity_id}" = "rke-frontend-sha256-v1:${rke_integrity_digest}" ] || return 1
    case "${rke_integrity_digest}" in *[!0-9a-f]*|"") return 1 ;; esac
    [ "${#rke_integrity_digest}" -eq 64 ] || return 1
    case "${rke_integrity_final}" in *[!0-9a-f]*|"") return 1 ;; esac
    [ "${#rke_integrity_final}" -eq 64 ] || return 1
    [ "$(sha256sum "${rke_integrity_index}" | cut -d' ' -f1)" = \
        "${rke_integrity_final}" ] || return 1
    rke_integrity_count="$(grep -o "${rke_integrity_id}" \
        "${rke_integrity_index}" | wc -l | tr -d ' ')" || return 1
    [ "${rke_integrity_count}" = "1" ] || return 1
    rke_integrity_normalized="$(mktemp \
        "${work_dir}/frontend-integrity.XXXXXX")" || return 1
    rke_integrity_placeholder="rke-frontend-sha256-v1:0000000000000000000000000000000000000000000000000000000000000000"
    if ! sed "s/${rke_integrity_id}/${rke_integrity_placeholder}/" \
        "${rke_integrity_index}" > "${rke_integrity_normalized}"; then
        rm -f "${rke_integrity_normalized}"
        return 1
    fi
    rke_integrity_normalized_hash="$(sha256sum \
        "${rke_integrity_normalized}" | cut -d' ' -f1)" || {
        rm -f "${rke_integrity_normalized}"
        return 1
    }
    rm -f "${rke_integrity_normalized}" || return 1
    [ "${rke_integrity_normalized_hash}" = "${rke_integrity_digest}" ] || return 1
    printf '%s\n' "${rke_integrity_id}"
}

health_protocol_supported() {
    rke_health_root="$1"
    [ -f "${rke_health_root}/install-health.json" ] || return 1
    jq -e '.protocol == 1 and
           .frontend_integrity == "sha256-normalized-v1"' \
        "${rke_health_root}/install-health.json" \
        >/dev/null 2>&1 || return 1
    frontend_integrity_id "${rke_health_root}" >/dev/null
}

plugin_release_version() {
    rke_version_root="$1"
    rke_version_value="$(sed -n '1p' "${rke_version_root}/VERSION" 2>/dev/null || true)"
    if [ -z "${rke_version_value}" ]; then
        rke_version_value="$(jq -r '.version // empty' \
            "${rke_version_root}/plugin.json" 2>/dev/null || true)"
    fi
    [ -n "${rke_version_value}" ] || return 1
    printf '%s\n' "${rke_version_value}"
}

legacy_release_allowed() {
    case "$1" in
        v0.1.0-alpha.[1-6]|0.1.0-alpha.[1-6]|v0.2.0-beta.[1-7]|0.2.0-beta.[1-7])
            return 0
            ;;
    esac
    return 1
}

write_install_health_request() {
    rke_health_version="$1"
    rke_health_frontend="$2"
    rke_health_nonce="$(cat "${PROC_ROOT}/sys/kernel/random/uuid" 2>/dev/null)" || return 1
    rke_health_boot_id="$(cat "${PROC_ROOT}/sys/kernel/random/boot_id" 2>/dev/null)" || return 1
    rke_health_main_hash="$(sha256sum "${PLUGIN_DIR}/main.py" | cut -d' ' -f1)" || return 1
    rke_health_dist_hash="$(sha256sum "${PLUGIN_DIR}/dist/index.js" | cut -d' ' -f1)" || return 1
    rke_health_bundle_id="$(frontend_integrity_id "${PLUGIN_DIR}")" || return 1
    [ -n "${rke_health_nonce}" ] && [ -n "${rke_health_boot_id}" ] || return 1
    case "${rke_health_frontend}" in
        require-frontend) rke_health_frontend_json=true ;;
        *) rke_health_frontend_json=false ;;
    esac
    rke_health_temporary="$(mktemp "${SETTINGS_DIR}/install-health-request.XXXXXX")" || return 1
    if ! jq -n \
        --arg nonce "${rke_health_nonce}" \
        --arg version "${rke_health_version}" \
        --arg boot_id "${rke_health_boot_id}" \
        --arg main_sha256 "${rke_health_main_hash}" \
        --arg dist_sha256 "${rke_health_dist_hash}" \
        --arg frontend_bundle_id "${rke_health_bundle_id}" \
        --argjson require_frontend "${rke_health_frontend_json}" \
        '{protocol: 1, nonce: $nonce, version: $version, boot_id: $boot_id,
          main_sha256: $main_sha256, dist_sha256: $dist_sha256,
          frontend_bundle_id: $frontend_bundle_id,
          require_frontend: $require_frontend}' > "${rke_health_temporary}"; then
        rm -f "${rke_health_temporary}"
        return 1
    fi
    health_created=1
    if ! chmod 600 "${rke_health_temporary}"; then
        rm -f "${rke_health_temporary}"
        return 1
    fi
    rm -f "${BACKEND_READY_FILE}" "${FRONTEND_READY_FILE}" || return 1
    mv "${rke_health_temporary}" "${HEALTH_REQUEST_FILE}" || return 1
    HEALTH_NONCE="${rke_health_nonce}"
    HEALTH_VERSION="${rke_health_version}"
    HEALTH_BOOT_ID="${rke_health_boot_id}"
    HEALTH_MAIN_HASH="${rke_health_main_hash}"
    HEALTH_DIST_HASH="${rke_health_dist_hash}"
    HEALTH_BUNDLE_ID="${rke_health_bundle_id}"
    HEALTH_REQUIRE_FRONTEND="${rke_health_frontend_json}"
    return 0
}

health_response_matches() {
    rke_health_file="$1"
    rke_health_loader_pid="$2"
    [ -f "${rke_health_file}" ] || return 1
    [ "$(sha256sum "${PLUGIN_DIR}/main.py" | cut -d' ' -f1)" = \
        "${HEALTH_MAIN_HASH}" ] || return 1
    [ "$(sha256sum "${PLUGIN_DIR}/dist/index.js" | cut -d' ' -f1)" = \
        "${HEALTH_DIST_HASH}" ] || return 1
    [ "$(frontend_integrity_id "${PLUGIN_DIR}")" = \
        "${HEALTH_BUNDLE_ID}" ] || return 1
    case "${rke_health_loader_pid}" in
        ""|*[!0-9]*) return 1 ;;
    esac
    [ "${rke_health_loader_pid}" -gt 0 ] || return 1
    rke_health_loader_start="$(process_start_time "${rke_health_loader_pid}")" || return 1
    if ! jq -e \
        --arg nonce "${HEALTH_NONCE}" \
        --arg version "${HEALTH_VERSION}" \
        --arg boot_id "${HEALTH_BOOT_ID}" \
        --arg main_sha256 "${HEALTH_MAIN_HASH}" \
        --arg dist_sha256 "${HEALTH_DIST_HASH}" \
        --arg frontend_bundle_id "${HEALTH_BUNDLE_ID}" \
        --argjson require_frontend "${HEALTH_REQUIRE_FRONTEND}" \
        --argjson loader_pid "${rke_health_loader_pid}" \
        --argjson loader_start "${rke_health_loader_start}" \
        '.protocol == 1 and .nonce == $nonce and .version == $version and
         .boot_id == $boot_id and .main_sha256 == $main_sha256 and
         .dist_sha256 == $dist_sha256 and
         .frontend_bundle_id == $frontend_bundle_id and
         .require_frontend == $require_frontend and
         (.lifecycle_token | type) == "string" and
         (.lifecycle_token | test("^[0-9a-f]{32}$")) and
         .loader.pid == $loader_pid and
         .loader.start_time_ticks == $loader_start and
         (.backend.pid | type) == "number" and
         (.backend.start_time_ticks | type) == "number"' \
        "${rke_health_file}" >/dev/null 2>&1; then
        return 1
    fi
    rke_health_backend_pid="$(jq -r '.backend.pid' "${rke_health_file}")"
    rke_health_backend_start="$(jq -r '.backend.start_time_ticks' "${rke_health_file}")"
    rke_health_live_backend_start="$(process_start_time "${rke_health_backend_pid}")" || return 1
    [ "${rke_health_backend_start}" = "${rke_health_live_backend_start}" ] || return 1
    HEALTH_RESPONSE_FINGERPRINT="${rke_health_loader_pid}:${rke_health_loader_start}:${rke_health_backend_pid}:${rke_health_backend_start}"
    return 0
}

wait_for_rke_health() {
    rke_health_frontend_requirement="$1"
    rke_health_deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
    rke_health_stable=0
    rke_health_previous_fingerprint=""
    while [ "$(date +%s)" -lt "${rke_health_deadline}" ]; do
        rke_health_loader_pid="$(systemctl_bounded show --property=MainPID --value \
            "${PLUGIN_LOADER_UNIT}" 2>/dev/null || true)"
        rke_health_sample=""
        if systemctl_bounded is-active --quiet "${PLUGIN_LOADER_UNIT}" && \
           health_response_matches "${BACKEND_READY_FILE}" "${rke_health_loader_pid}"; then
            rke_health_backend_fingerprint="${HEALTH_RESPONSE_FINGERPRINT}"
            if [ "${rke_health_frontend_requirement}" != "require-frontend" ]; then
                rke_health_sample="${rke_health_backend_fingerprint}"
            elif health_response_matches \
                    "${FRONTEND_READY_FILE}" "${rke_health_loader_pid}" && \
                 [ "${HEALTH_RESPONSE_FINGERPRINT}" = \
                    "${rke_health_backend_fingerprint}" ]; then
                rke_health_sample="${rke_health_backend_fingerprint}"
            fi
        fi
        if [ -n "${rke_health_sample}" ]; then
            if [ "${rke_health_sample}" = "${rke_health_previous_fingerprint}" ]; then
                rke_health_stable=$((rke_health_stable + 1))
            else
                rke_health_stable=1
                rke_health_previous_fingerprint="${rke_health_sample}"
            fi
            if [ "${rke_health_stable}" -ge 2 ]; then
                return 0
            fi
        else
            rke_health_stable=0
            rke_health_previous_fingerprint=""
        fi
        sleep 1
    done
    return 1
}

for command in curl cut flock grep jq sed sha256sum systemctl timeout tr unzip wc; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        write_status "Update failed: missing ${command}"
        exit 1
    fi
done

work_dir="$(mktemp -d /tmp/rk-enhanced-update.XXXXXX)"
backup_dir=""
plugin_had_current=0
plugin_install_attempted=0
recovery_dir=""
preserve_recovery=0
loader_backup=""
loader_version_backup=""
installed_version_backup=""
loader_existed=0
loader_had_version=0
installed_version_existed=0
installed_version_temporary=""
service_was_active=0
loader_stop_attempted=0
files_mutated=0

cleanup_failure() {
    result=$?
    trap - EXIT HUP INT TERM
    set +e
    if [ "${result}" -ne 0 ] && [ "${transaction_committed}" -ne 1 ]; then
        rollback_ok=1
        rollback_lock=1
        rollback_health=0
        rollback_health_verified=0
        rollback_legacy=0
        if [ "${loader_stop_attempted}" -eq 1 ] || [ "${files_mutated}" -eq 1 ]; then
            write_status "Update failed; rolling back RK-Enhanced and Decky"
            if [ "${maintenance_active}" -ne 1 ] && \
               ! begin_plugin_loader_maintenance_bounded 10; then
                rollback_lock=0
                rollback_ok=0
            fi
            if [ "${rollback_lock}" -eq 1 ]; then
                if ! stop_plugin_loader_bounded; then
                    rollback_ok=0
                fi
                if [ "${files_mutated}" -eq 1 ]; then
                    clear_install_health
                    if [ -n "${backup_dir}" ] && [ -d "${backup_dir}" ]; then
                        rm -rf "${PLUGIN_DIR}"
                        if ! mv "${backup_dir}" "${PLUGIN_DIR}"; then
                            rollback_ok=0
                        fi
                    elif [ "${plugin_had_current}" -eq 0 ] && \
                         [ "${plugin_install_attempted}" -eq 1 ]; then
                        rm -rf "${PLUGIN_DIR}"
                    fi
                    if [ "${loader_existed}" -eq 1 ]; then
                        if ! cp -p "${loader_backup}" "${PLUGIN_LOADER_PATH}"; then
                            rollback_ok=0
                        fi
                    else
                        rm -f "${PLUGIN_LOADER_PATH}"
                    fi
                    if [ "${loader_had_version}" -eq 1 ]; then
                        if ! cp -p "${loader_version_backup}" \
                            "${PLUGIN_LOADER_VERSION_FILE}"; then
                            rollback_ok=0
                        fi
                    else
                        rm -f "${PLUGIN_LOADER_VERSION_FILE}"
                    fi
                    if [ "${installed_version_existed}" -eq 1 ]; then
                        if ! cp -p "${installed_version_backup}" \
                            "${INSTALLED_VERSION_FILE}"; then
                            rollback_ok=0
                        fi
                    else
                        rm -f "${INSTALLED_VERSION_FILE}"
                    fi
                fi
                if [ "${service_was_active}" -eq 1 ] && [ "${loader_existed}" -eq 1 ]; then
                    if [ "${plugin_had_current}" -eq 1 ]; then
                        rollback_version="$(plugin_release_version \
                            "${PLUGIN_DIR}")" || rollback_ok=0
                        if [ "${rollback_ok}" -eq 1 ] && \
                           { [ -e "${PLUGIN_DIR}/install-health.json" ] || \
                             [ -e "${PLUGIN_DIR}/dist/frontend-integrity.json" ]; }; then
                            if health_protocol_supported "${PLUGIN_DIR}" && \
                               write_install_health_request \
                                   "${rollback_version}" ""; then
                                rollback_health=1
                            else
                                rollback_ok=0
                            fi
                        elif [ "${rollback_ok}" -eq 1 ] && \
                             legacy_release_allowed "${rollback_version}"; then
                            rollback_legacy=1
                        else
                            rollback_ok=0
                        fi
                    fi
                    if ! systemctl_bounded start "${PLUGIN_LOADER_UNIT}" >/dev/null 2>&1 || \
                       ! wait_for_plugin_loader_start 15; then
                        rollback_ok=0
                    fi
                fi
                end_plugin_loader_maintenance
                if [ "${rollback_health}" -eq 1 ]; then
                    if wait_for_rke_health ""; then
                        rollback_health_verified=1
                    else
                        rollback_ok=0
                    fi
                fi
                clear_install_health
                health_created=0
            fi
            if [ "${rollback_ok}" -eq 1 ]; then
                if [ "${rollback_health_verified}" -eq 1 ]; then
                    write_status "Update failed; previous RK-Enhanced and Decky restored; backend verified"
                elif [ "${rollback_legacy}" -eq 1 ]; then
                    write_status "Update failed; previous RK-Enhanced and Decky restored; legacy backend unverified"
                else
                    write_status "Update failed; previous Decky state and RK-Enhanced files restored"
                fi
            else
                if [ "${rollback_lock}" -eq 0 ]; then
                    write_status "Update failed; rollback lock busy, no unlocked restoration attempted"
                else
                    write_status "Update failed and rollback needs manual recovery"
                fi
                preserve_recovery=1
            fi
        else
            write_status "Update failed before installed files or Decky state changed"
        fi
    fi
    if [ "${health_created}" -eq 1 ]; then
        clear_install_health
    fi
    end_plugin_loader_maintenance
    end_install_transaction
    if [ -n "${installed_version_temporary}" ]; then
        rm -f "${installed_version_temporary}"
    fi
    if [ -n "${recovery_dir}" ] && [ "${preserve_recovery}" -eq 0 ]; then
        rm -rf "${recovery_dir}"
    elif [ -n "${recovery_dir}" ] && [ "${preserve_recovery}" -eq 1 ]; then
        write_status "Update failed; manual recovery files: ${recovery_dir}"
    fi
    rm -rf "${work_dir}"
    exit "${result}"
}
trap cleanup_failure EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if ! begin_install_transaction; then
    write_status "Update failed: another RK-Enhanced install or update is running"
    exit 1
fi

write_status "Checking the latest stable Decky release…"
decky_metadata="${work_dir}/decky.json"
curl -fL "https://api.github.com/repos/${DECKY_REPOSITORY}/releases/latest" \
    -o "${decky_metadata}"
decky_version="$(jq -r '.tag_name // empty' "${decky_metadata}")"
decky_url="$(jq -r '.assets[] | select(.name == "PluginLoader") | .browser_download_url' \
    "${decky_metadata}")"
decky_digest="$(jq -r '.assets[] | select(.name == "PluginLoader") | .digest // empty' \
    "${decky_metadata}")"
if [ -z "${decky_version}" ] || [ -z "${decky_url}" ]; then
    write_status "Update failed: latest stable Decky could not be resolved"
    exit 1
fi
curl -fL "${decky_url}" -o "${work_dir}/PluginLoader"
if [ -n "${decky_digest}" ]; then
    expected="${decky_digest#sha256:}"
    actual="$(sha256sum "${work_dir}/PluginLoader" | cut -d' ' -f1)"
    if [ "${actual}" != "${expected}" ]; then
        write_status "Update failed: Decky checksum mismatch"
        exit 1
    fi
fi

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
health_supported=0
if [ -e "${staged}/install-health.json" ] || \
   [ -e "${staged}/dist/frontend-integrity.json" ]; then
    if ! health_protocol_supported "${staged}"; then
        write_status "Update failed: invalid install-health metadata or frontend integrity"
        exit 1
    fi
    health_supported=1
elif [ -n "${requested_version}" ] && \
     legacy_release_allowed "${version}"; then
    # Only immutable, explicitly known pre-protocol releases may use the
    # service-active legacy path. Never infer legacy status from release order.
    preserve_updater=1
else
    write_status "Update failed: release is missing required install-health metadata"
    exit 1
fi
if [ "${preserve_updater}" -eq 1 ]; then
    cp "$0" "${staged}/updater.sh"
    chmod 755 "${staged}/updater.sh"
fi

recovery_dir="$(mktemp -d \
    "${BACKUP_ROOT}/update-recovery-${version}-$(date +%Y%m%d-%H%M%S).XXXXXX")"
chmod 700 "${recovery_dir}"
loader_backup="${recovery_dir}/PluginLoader.previous"
loader_version_backup="${recovery_dir}/loader-version.previous"
installed_version_backup="${recovery_dir}/installed-version.previous"

if systemctl_bounded is-active --quiet "${PLUGIN_LOADER_UNIT}"; then
    service_was_active=1
fi
if [ ! -f "${PLUGIN_LOADER_PATH}" ]; then
    write_status "Update failed: the current Decky executable is missing"
    exit 1
fi
loader_existed=1
cp -p "${PLUGIN_LOADER_PATH}" "${loader_backup}"
if [ -f "${PLUGIN_LOADER_VERSION_FILE}" ]; then
    loader_had_version=1
    cp -p "${PLUGIN_LOADER_VERSION_FILE}" "${loader_version_backup}"
fi
if [ -f "${INSTALLED_VERSION_FILE}" ]; then
    installed_version_existed=1
    cp -p "${INSTALLED_VERSION_FILE}" "${installed_version_backup}"
fi
if [ -d "${PLUGIN_DIR}" ]; then
    plugin_had_current=1
    backup_dir="$(mktemp -d \
        "${BACKUP_ROOT}/RK-Enhanced-before-${version}-$(date +%Y%m%d-%H%M%S).XXXXXX")"
    rmdir "${backup_dir}"
fi

write_status "Installing ${version}; Decky is reloading…"
if ! begin_plugin_loader_maintenance; then
    write_status "Update failed: another PluginLoader maintenance action is running"
    exit 1
fi
loader_stop_attempted=1
if ! stop_plugin_loader_bounded; then
    write_status "Update failed: Decky did not stop within the bounded timeout"
    exit 1
fi

files_mutated=1
if [ "${plugin_had_current}" -eq 1 ]; then
    mv "${PLUGIN_DIR}" "${backup_dir}"
fi
plugin_install_attempted=1
mv "${staged}" "${PLUGIN_DIR}"
chmod 755 "${PLUGIN_DIR}/updater.sh" \
    "${PLUGIN_DIR}/runtime-restore.py" \
    "${PLUGIN_DIR}/runtime-restore-guard.sh"
if [ -f "${PLUGIN_DIR}/plugin_loader_recovery.py" ]; then
    chmod 755 "${PLUGIN_DIR}/plugin_loader_recovery.py"
fi
cp "${work_dir}/PluginLoader" "${PLUGIN_LOADER_PATH}"
chmod 755 "${PLUGIN_LOADER_PATH}"
printf '%s\n' "${decky_version}" > "${PLUGIN_LOADER_VERSION_FILE}"
if [ "${health_supported}" -eq 1 ]; then
    if ! write_install_health_request "${version}" "${frontend_requirement}"; then
        write_status "Update failed: could not create a fresh install health challenge"
        exit 1
    fi
fi

systemctl_bounded start "${PLUGIN_LOADER_UNIT}"
if ! wait_for_plugin_loader_start 15; then
    write_status "Update failed: Decky did not start within the bounded timeout"
    exit 1
fi
# Lifecycle publication uses the same lock as maintenance. Release it only
# after the tentative unit is active, then require the nonce-bound backend and
# frontend responses while the separate transaction lock prevents overlap.
end_plugin_loader_maintenance
if [ "${health_supported}" -eq 1 ]; then
    write_status "Verifying RK-Enhanced ${version} with Decky ${decky_version}…"
    if ! wait_for_rke_health "${frontend_requirement}"; then
        write_status "Update failed: RK-Enhanced did not pass backend/frontend readiness"
        exit 1
    fi
    if [ "${frontend_requirement}" = "require-frontend" ]; then
        install_result="backend and frontend verified"
    else
        install_result="backend verified"
    fi
else
    install_result="legacy release; backend readiness unavailable"
fi
installed_version_temporary="$(mktemp \
    "${SETTINGS_DIR}/installed-version.XXXXXX")"
printf '%s\n' "${version}" > "${installed_version_temporary}"
chmod 600 "${installed_version_temporary}"
mv "${installed_version_temporary}" "${INSTALLED_VERSION_FILE}"
write_status "Installed ${version} with Decky ${decky_version}; ${install_result}"
if [ "${health_created}" -eq 1 ]; then
    clear_install_health
    health_created=0
fi
transaction_committed=1
end_install_transaction
exit 0
