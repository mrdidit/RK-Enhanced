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
LAST_INSTALLED_VERSION_FILE="${SETTINGS_DIR}/last-installed-version.txt"
INSTALL_PROGRESS_FILE="${SETTINGS_DIR}/install-progress.json"
LOG_DIR="${HOMEBREW_DIR}/logs/RK-Enhanced"
INSTALL_JOURNAL_FILE="${LOG_DIR}/installer.log"
INSTALL_JOURNAL_LIMIT=524288
HEALTH_REQUEST_FILE="${SETTINGS_DIR}/install-health-request.json"
BACKEND_READY_FILE="${SETTINGS_DIR}/install-backend-ready.json"
FRONTEND_READY_FILE="${SETTINGS_DIR}/install-frontend-ready.json"
PLUGIN_LOADER_UNIT="plugin_loader.service"
RECOVERY_LOCK_PATH="${RUN_ROOT}/lock/rk-enhanced-plugin-loader-recovery.lock"
RECOVERY_MARKER_PATH="${RUN_ROOT}/rk-enhanced-plugin-loader-recovery.active"
TRANSACTION_LOCK_PATH="${RUN_ROOT}/lock/rk-enhanced-install-transaction.lock"
HEALTH_TIMEOUT="${RKE_HEALTH_TIMEOUT:-90}"
operation_kind="update"
conflict_removal_target=""
conflict_removal_approval=""
requested_version=""
frontend_requirement=""
remove_conflicting_control=0
if [ "${1:-}" = "--remove-rocknix-control" ]; then
    operation_kind="remove-conflict"
    conflict_removal_target="${2:-}"
    conflict_removal_approval="${3:-}"
    case "${conflict_removal_approval}" in
        ""|*[!0-9a-f]* )
            echo "Invalid exact conflict approval token." >&2
            exit 1
            ;;
    esac
    if [ -z "${conflict_removal_target}" ] || \
       [ "${#conflict_removal_approval}" -ne 64 ] || [ -n "${4:-}" ]; then
        echo "Usage: updater.sh --remove-rocknix-control EXACT_PLUGIN_DIRECTORY APPROVAL_SHA256" >&2
        exit 1
    fi
else
    requested_version="${1:-}"
    frontend_requirement="${2:-}"
    case "${3:-}" in
        "") ;;
        remove-conflicting-rocknix-control) remove_conflicting_control=1 ;;
        *)
            echo "Invalid updater conflict option." >&2
            exit 1
            ;;
    esac
    [ -z "${4:-}" ] || {
        echo "Too many updater arguments." >&2
        exit 1
    }
fi
case "${RKE_REMOVE_CONFLICTING_ROCKNIX_CONTROL:-0}" in
    1|true|yes) remove_conflicting_control=1 ;;
    0|false|no|"") ;;
    *)
        echo "Invalid RKE_REMOVE_CONFLICTING_ROCKNIX_CONTROL value." >&2
        exit 1
        ;;
esac
maintenance_active=0
transaction_active=0
health_created=0
transaction_committed=0
progress_transaction_id=""
progress_generation=0
progress_started_at=0
progress_source_version=""
progress_target_version=""
progress_decky_version=""
progress_terminal_written=0
progress_boot_id=""
progress_writer_start=0
legacy_control_removed=0

case "${frontend_requirement}" in
    ""|require-frontend) ;;
    *)
        echo "Invalid updater health requirement." >&2
        exit 1
        ;;
esac

mkdir -p "$(dirname "${STATUS_FILE}")" "${BACKUP_ROOT}" "${LOG_DIR}"

write_status() {
    printf '%s\n' "$1" > "${STATUS_FILE}"
}

rotate_installer_journal() {
    mkdir -p "${LOG_DIR}" || return 1
    if [ -f "${INSTALL_JOURNAL_FILE}" ]; then
        rke_journal_size="$(wc -c < "${INSTALL_JOURNAL_FILE}" 2>/dev/null | tr -d ' ')" || \
            rke_journal_size=0
        case "${rke_journal_size}" in
            ""|*[!0-9]*) rke_journal_size=0 ;;
        esac
        if [ "${rke_journal_size}" -ge "${INSTALL_JOURNAL_LIMIT}" ]; then
            mv -f "${INSTALL_JOURNAL_FILE}" "${INSTALL_JOURNAL_FILE}.1" || return 1
        fi
    fi
    return 0
}

append_installer_journal() {
    rke_journal_outcome="$1"
    rke_journal_phase="$2"
    rke_journal_message="$(printf '%s' "$3" | tr '\r\n' '  ')"
    rotate_installer_journal || return 1
    printf '%s [%s] [%s] [%s] [%s] %s\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" \
        "${progress_transaction_id:-unassigned}" "${operation_kind}" \
        "${rke_journal_phase}" "${rke_journal_outcome}" \
        "${rke_journal_message}" >> "${INSTALL_JOURNAL_FILE}" || return 1
    chmod 600 "${INSTALL_JOURNAL_FILE}" 2>/dev/null || true
}

begin_progress_transaction() {
    mkdir -p "${SETTINGS_DIR}" || return 1
    progress_transaction_id="$(cat "${PROC_ROOT}/sys/kernel/random/uuid" 2>/dev/null)" || \
        return 1
    progress_boot_id="$(cat "${PROC_ROOT}/sys/kernel/random/boot_id" 2>/dev/null)" || \
        return 1
    progress_writer_start="$(process_start_time "$$")" || return 1
    [ -n "${progress_transaction_id}" ] && [ -n "${progress_boot_id}" ] || return 1
    rke_previous_generation="$(jq -r \
        'if .protocol == 1 and (.generation | type) == "number" then .generation else 0 end' \
        "${INSTALL_PROGRESS_FILE}" 2>/dev/null || printf '0')"
    case "${rke_previous_generation}" in
        ""|*[!0-9]*) rke_previous_generation=0 ;;
    esac
    progress_generation=$((rke_previous_generation + 1))
    progress_started_at="$(date +%s)"
    return 0
}

publish_install_progress() {
    rke_progress_outcome="$1"
    rke_progress_phase="$2"
    rke_progress_message="$3"
    rke_progress_error="${4:-}"
    case "${rke_progress_outcome}" in
        running)
            rke_progress_active=true
            rke_progress_terminal=false
            rke_progress_success=null
            rke_progress_rolled_back=false
            ;;
        succeeded)
            rke_progress_active=false
            rke_progress_terminal=true
            rke_progress_success=true
            rke_progress_rolled_back=false
            ;;
        rolled-back)
            rke_progress_active=false
            rke_progress_terminal=true
            rke_progress_success=false
            rke_progress_rolled_back=true
            ;;
        failed|blocked)
            rke_progress_active=false
            rke_progress_terminal=true
            rke_progress_success=false
            rke_progress_rolled_back=false
            ;;
        *) return 1 ;;
    esac
    rke_progress_temporary="$(mktemp \
        "${SETTINGS_DIR}/install-progress.XXXXXX")" || return 1
    if ! jq -n \
        --arg transaction_id "${progress_transaction_id}" \
        --argjson generation "${progress_generation}" \
        --arg kind "${operation_kind}" \
        --arg source_version "${progress_source_version}" \
        --arg target_version "${progress_target_version}" \
        --arg decky_version "${progress_decky_version}" \
        --arg phase "${rke_progress_phase}" \
        --arg message "${rke_progress_message}" \
        --arg outcome "${rke_progress_outcome}" \
        --arg error "${rke_progress_error}" \
        --arg boot_id "${progress_boot_id}" \
        --argjson writer_pid "$$" \
        --argjson writer_start_time_ticks "${progress_writer_start}" \
        --argjson active "${rke_progress_active}" \
        --argjson terminal "${rke_progress_terminal}" \
        --argjson success "${rke_progress_success}" \
        --argjson rolled_back "${rke_progress_rolled_back}" \
        --argjson started_at "${progress_started_at}" \
        --argjson updated_at "$(date +%s)" \
        '{protocol: 1, transaction_id: $transaction_id,
          generation: $generation, active: $active, terminal: $terminal,
          kind: $kind, source_version: $source_version,
          target_version: $target_version, decky_version: $decky_version,
          phase: $phase, message: $message, outcome: $outcome,
          started_at: $started_at, updated_at: $updated_at,
          writer: {pid: $writer_pid, start_time_ticks: $writer_start_time_ticks,
                   boot_id: $boot_id},
          success: $success, rolled_back: $rolled_back,
          error: (if $error == "" then null else $error end)}' \
        > "${rke_progress_temporary}"; then
        rm -f "${rke_progress_temporary}"
        return 1
    fi
    chmod 600 "${rke_progress_temporary}" || {
        rm -f "${rke_progress_temporary}"
        return 1
    }
    mv "${rke_progress_temporary}" "${INSTALL_PROGRESS_FILE}" || return 1
    if [ "${rke_progress_terminal}" = true ]; then
        progress_terminal_written=1
    fi
    return 0
}

record_install_event() {
    rke_event_outcome="$1"
    rke_event_phase="$2"
    rke_event_message="$3"
    rke_event_error="${4:-}"
    write_status "${rke_event_message}" || true
    publish_install_progress "${rke_event_outcome}" "${rke_event_phase}" \
        "${rke_event_message}" "${rke_event_error}" || true
    append_installer_journal "${rke_event_outcome}" "${rke_event_phase}" \
        "${rke_event_message}" || true
}

normalized_plugin_name() {
    jq -r 'select((.name | type) == "string") | .name |
        gsub("^\\s+|\\s+$"; "") | ascii_downcase' "$1" 2>/dev/null
}

scan_legacy_rocknix_control() {
    rke_conflict_output="$1"
    : > "${rke_conflict_output}" || return 1
    [ -d "${PLUGINS_DIR}" ] || return 0
    for rke_conflict_path in "${PLUGINS_DIR}"/*; do
        [ -d "${rke_conflict_path}" ] || continue
        rke_conflict_manifest="${rke_conflict_path}/plugin.json"
        [ -f "${rke_conflict_manifest}" ] || continue
        if [ "$(normalized_plugin_name "${rke_conflict_manifest}")" = \
             "rocknix control" ]; then
            printf '%s\n' "${rke_conflict_path}" >> "${rke_conflict_output}" || return 1
        fi
    done
}

validate_legacy_rocknix_control_path() {
    rke_conflict_path="$1"
    rke_conflict_prefix="${PLUGINS_DIR}/"
    [ -d "${PLUGINS_DIR}" ] && [ ! -L "${PLUGINS_DIR}" ] || return 1
    case "${rke_conflict_path}" in
        "${rke_conflict_prefix}"*)
            rke_conflict_leaf="${rke_conflict_path#"${rke_conflict_prefix}"}"
            ;;
        *) return 1 ;;
    esac
    case "${rke_conflict_leaf}" in
        ""|.|..|*/*) return 1 ;;
    esac
    if printf '%s' "${rke_conflict_leaf}" | grep -q '[[:cntrl:]]'; then
        return 1
    fi
    [ "${rke_conflict_path}" = "${rke_conflict_prefix}${rke_conflict_leaf}" ] || return 1
    [ -d "${rke_conflict_path}" ] && [ ! -L "${rke_conflict_path}" ] || return 1
    rke_conflict_manifest="${rke_conflict_path}/plugin.json"
    [ -f "${rke_conflict_manifest}" ] && [ ! -L "${rke_conflict_manifest}" ] || return 1
    [ "$(normalized_plugin_name "${rke_conflict_manifest}")" = \
      "rocknix control" ]
}

fingerprint_legacy_rocknix_control() {
    rke_conflict_input="$1"
    rke_conflict_output="$2"
    : > "${rke_conflict_output}" || return 1
    while IFS= read -r rke_conflict_path; do
        [ -n "${rke_conflict_path}" ] || continue
        validate_legacy_rocknix_control_path "${rke_conflict_path}" || return 1
        rke_conflict_manifest="${rke_conflict_path}/plugin.json"
        rke_conflict_directory_identity="$(stat -c '%d:%i:%Z' \
            "${rke_conflict_path}")" || return 1
        rke_conflict_manifest_identity="$(stat -c '%d:%i:%Z:%s' \
            "${rke_conflict_manifest}")" || return 1
        rke_conflict_manifest_digest="$(sha256sum \
            "${rke_conflict_manifest}" | cut -d ' ' -f 1)" || return 1
        printf '%s\t%s\t%s\t%s\n' \
            "${rke_conflict_path}" \
            "${rke_conflict_directory_identity}" \
            "${rke_conflict_manifest_identity}" \
            "${rke_conflict_manifest_digest}" \
            >> "${rke_conflict_output}" || return 1
    done < "${rke_conflict_input}"
}

wait_for_native_fancontrol() {
    rke_fan_deadline=$(( $(date +%s) + $1 ))
    while [ "$(date +%s)" -lt "${rke_fan_deadline}" ]; do
        if systemctl_bounded is-active --quiet fancontrol.service; then
            return 0
        fi
        sleep 1
    done
    return 1
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

for command in cmp curl cut flock grep jq sed sha256sum stat systemctl timeout tr unzip wc; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        write_status "Update failed: missing ${command}"
        exit 1
    fi
done

remove_all_legacy_rocknix_control() {
    rke_conflict_approved="$1"
    rke_conflict_approved_fingerprint="$2"
    rke_conflict_current_fingerprint="${work_dir}/rocknix-control-remove.fingerprint"
    fingerprint_legacy_rocknix_control \
        "${rke_conflict_approved}" \
        "${rke_conflict_current_fingerprint}" || return 1
    cmp -s "${rke_conflict_approved_fingerprint}" \
        "${rke_conflict_current_fingerprint}" || return 1
    # All approved paths have now been revalidated. Delete only that exact set.
    while IFS= read -r rke_conflict_path; do
        [ -n "${rke_conflict_path}" ] || continue
        rm -rf "${rke_conflict_path}" || return 1
        legacy_control_removed=1
        append_installer_journal running removing-conflict \
            "Removed conflicting ROCKNIX Control plugin at ${rke_conflict_path}; no backup was created." || true
    done < "${rke_conflict_approved}"
    systemctl_bounded start fancontrol.service >/dev/null 2>&1 || return 1
    wait_for_native_fancontrol 10 || return 1
    return 0
}

verify_standalone_conflict_approval() {
    rke_conflict_approved_path="${work_dir}/standalone-conflict-approved.txt"
    rke_conflict_approved_fingerprint="${work_dir}/standalone-conflict.fingerprint"
    printf '%s\n' "${conflict_removal_target}" \
        > "${rke_conflict_approved_path}" || return 1
    fingerprint_legacy_rocknix_control \
        "${rke_conflict_approved_path}" \
        "${rke_conflict_approved_fingerprint}" || return 1
    rke_conflict_current_approval="$(sha256sum \
        "${rke_conflict_approved_fingerprint}" | cut -d ' ' -f 1)" || return 1
    [ "${rke_conflict_current_approval}" = \
      "${conflict_removal_approval}" ]
}

run_standalone_conflict_removal() {
    work_dir="$(mktemp -d /tmp/rk-enhanced-conflict-removal.XXXXXX)"
    conflict_service_was_active=0
    conflict_loader_stop_attempted=0
    conflict_removal_committed=0

    cleanup_standalone_conflict_removal() {
        rke_conflict_result=$?
        trap - EXIT HUP INT TERM
        set +e
        if [ "${rke_conflict_result}" -ne 0 ] && \
           [ "${conflict_removal_committed}" -ne 1 ]; then
            if [ "${conflict_loader_stop_attempted}" -eq 1 ]; then
                systemctl_bounded start fancontrol.service >/dev/null 2>&1 || true
                if [ "${conflict_service_was_active}" -eq 1 ]; then
                    systemctl_bounded start "${PLUGIN_LOADER_UNIT}" >/dev/null 2>&1 || true
                    wait_for_plugin_loader_start 15 || true
                fi
            fi
            end_plugin_loader_maintenance
            if [ "${progress_terminal_written}" -ne 1 ] && \
               [ -n "${progress_transaction_id}" ]; then
                record_install_event failed failed \
                    "ROCKNIX Control removal failed; PluginLoader recovery was attempted." \
                    "Removal exited with status ${rke_conflict_result}."
            fi
        fi
        end_plugin_loader_maintenance
        end_install_transaction
        rm -rf "${work_dir}"
        exit "${rke_conflict_result}"
    }
    trap cleanup_standalone_conflict_removal EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    if ! begin_install_transaction; then
        write_status "ROCKNIX Control removal blocked: another RK-Enhanced transaction is active"
        append_installer_journal blocked blocked \
            "ROCKNIX Control removal blocked because another RK-Enhanced transaction is active." || true
        exit 1
    fi
    if ! begin_progress_transaction; then
        write_status "ROCKNIX Control removal failed: progress state could not be initialized"
        exit 1
    fi
    progress_source_version="$(sed -n '1p' "${INSTALLED_VERSION_FILE}" 2>/dev/null || true)"
    progress_target_version="${progress_source_version}"
    record_install_event running starting \
        "ROCKNIX Control removal started."

    if ! verify_standalone_conflict_approval; then
        record_install_event blocked blocked \
            "ROCKNIX Control removal was blocked; the approved plugin identity no longer matches." \
            "Request removal again to approve the current exact path and manifest."
        exit 1
    fi
    record_install_event running preflight \
        "Validated conflicting ROCKNIX Control at ${conflict_removal_target}; removal is permanent and creates no backup."
    if systemctl_bounded is-active --quiet "${PLUGIN_LOADER_UNIT}"; then
        conflict_service_was_active=1
    fi
    if ! begin_plugin_loader_maintenance; then
        record_install_event failed failed \
            "ROCKNIX Control removal failed because another PluginLoader maintenance action is active." \
            "PluginLoader maintenance lock is busy."
        exit 1
    fi
    conflict_loader_stop_attempted=1
    record_install_event running stopping-decky \
        "Stopping Decky before removing ROCKNIX Control."
    if ! stop_plugin_loader_bounded; then
        record_install_event failed failed \
            "ROCKNIX Control removal failed because Decky did not stop within the bounded timeout." \
            "PluginLoader stop timed out."
        exit 1
    fi
    if ! verify_standalone_conflict_approval; then
        record_install_event failed failed \
            "ROCKNIX Control changed during maintenance; nothing was deleted." \
            "Exact path and manifest identity revalidation failed after Decky stopped."
        exit 1
    fi
    record_install_event running removing-conflict \
        "Removing ${conflict_removal_target} without a backup."
    rm -rf "${conflict_removal_target}"
    systemctl_bounded start fancontrol.service >/dev/null 2>&1
    if ! wait_for_native_fancontrol 10; then
        record_install_event failed failed \
            "ROCKNIX Control was removed, but native fancontrol did not become active." \
            "fancontrol.service failed to start."
        exit 1
    fi
    if [ "${conflict_service_was_active}" -eq 1 ]; then
        record_install_event running starting-decky \
            "Restarting Decky after conflict removal."
        systemctl_bounded start "${PLUGIN_LOADER_UNIT}"
        if ! wait_for_plugin_loader_start 15; then
            record_install_event failed failed \
                "ROCKNIX Control was removed and fancontrol restored, but Decky did not restart." \
                "PluginLoader start timed out."
            exit 1
        fi
    fi
    end_plugin_loader_maintenance
    record_install_event succeeded completed \
        "ROCKNIX Control was removed without a backup; native fancontrol is active."
    conflict_removal_committed=1
    end_install_transaction
    exit 0
}

if [ "${operation_kind}" = "remove-conflict" ]; then
    run_standalone_conflict_removal
fi

work_dir="$(mktemp -d /tmp/rk-enhanced-update.XXXXXX)"
backup_dir=""
plugin_had_current=0
plugin_install_attempted=0
recovery_dir=""
preserve_recovery=0
loader_backup=""
loader_version_backup=""
installed_version_backup=""
last_installed_version_backup=""
loader_existed=0
loader_had_version=0
installed_version_existed=0
last_installed_version_existed=0
last_installed_version_changed=0
installed_version_temporary=""
last_installed_version_temporary=""
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
            record_install_event running rolling-back \
                "Update failed; restoring the previous RK-Enhanced and Decky state."
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
                        rm -f "${backup_dir}/.rke-backup-created.json"
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
                    if [ "${last_installed_version_changed}" -eq 1 ]; then
                        if [ "${last_installed_version_existed}" -eq 1 ]; then
                            if ! cp -p "${last_installed_version_backup}" \
                                "${LAST_INSTALLED_VERSION_FILE}"; then
                                rollback_ok=0
                            fi
                        else
                            rm -f "${LAST_INSTALLED_VERSION_FILE}"
                        fi
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
                rollback_conflict_note=""
                if [ "${legacy_control_removed}" -eq 1 ]; then
                    rollback_conflict_note=" Conflicting ROCKNIX Control remains permanently removed."
                fi
                if [ "${rollback_health_verified}" -eq 1 ]; then
                    record_install_event rolled-back rolled-back \
                        "Update failed; previous RK-Enhanced and Decky restored; backend verified.${rollback_conflict_note}" \
                        "The requested release did not pass verification."
                elif [ "${rollback_legacy}" -eq 1 ]; then
                    record_install_event rolled-back rolled-back \
                        "Update failed; previous RK-Enhanced and Decky restored; legacy backend unverified.${rollback_conflict_note}" \
                        "The requested release did not pass verification."
                else
                    record_install_event rolled-back rolled-back \
                        "Update failed; previous Decky state and RK-Enhanced files restored.${rollback_conflict_note}" \
                        "The requested release did not complete."
                fi
            else
                if [ "${rollback_lock}" -eq 0 ]; then
                    record_install_event failed failed \
                        "Update failed; rollback lock was busy and no unlocked restoration was attempted." \
                        "Manual recovery is required."
                else
                    record_install_event failed failed \
                        "Update failed and rollback needs manual recovery." \
                        "Automatic rollback did not complete."
                fi
                preserve_recovery=1
            fi
        else
            if [ "${progress_terminal_written}" -ne 1 ]; then
                record_install_event failed failed \
                    "Update failed before installed files or Decky state changed." \
                    "Updater exited with status ${result}."
            fi
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
    if [ -n "${last_installed_version_temporary}" ]; then
        rm -f "${last_installed_version_temporary}"
    fi
    if [ -n "${recovery_dir}" ] && [ "${preserve_recovery}" -eq 0 ]; then
        rm -rf "${recovery_dir}"
    elif [ -n "${recovery_dir}" ] && [ "${preserve_recovery}" -eq 1 ]; then
        record_install_event failed failed \
            "Update failed; manual recovery files: ${recovery_dir}" \
            "Manual recovery files were preserved."
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
    append_installer_journal blocked blocked \
        "Update blocked because another RK-Enhanced transaction is active." || true
    exit 1
fi

if ! begin_progress_transaction; then
    write_status "Update failed: progress state could not be initialized"
    exit 1
fi
progress_source_version="$(sed -n '1p' "${INSTALLED_VERSION_FILE}" 2>/dev/null || true)"
if [ -z "${progress_source_version}" ] && [ -d "${PLUGIN_DIR}" ]; then
    progress_source_version="$(plugin_release_version \
        "${PLUGIN_DIR}" 2>/dev/null || true)"
fi
if [ -n "${progress_source_version}" ]; then
    record_install_event running starting \
        "RK-Enhanced transaction started from ${progress_source_version}."
else
    record_install_event running starting \
        "RK-Enhanced transaction started without installed-version metadata."
fi

legacy_conflicts="${work_dir}/rocknix-control-conflicts.txt"
legacy_conflict_fingerprint="${work_dir}/rocknix-control-conflicts.fingerprint"
if ! scan_legacy_rocknix_control "${legacy_conflicts}"; then
    record_install_event failed failed \
        "Update failed because installed plugins could not be scanned safely." \
        "Plugin conflict scan failed."
    exit 1
fi
if [ -s "${legacy_conflicts}" ]; then
    if [ "${remove_conflicting_control}" -ne 1 ]; then
        record_install_event blocked blocked \
            "Conflicting ROCKNIX Control detected; update was blocked before files changed." \
            "Remove the original ROCKNIX Control or Rocknix Control Enhanced installation first."
        exit 1
    fi
    while IFS= read -r rke_conflict_path; do
        [ -n "${rke_conflict_path}" ] || continue
        if ! validate_legacy_rocknix_control_path "${rke_conflict_path}"; then
            record_install_event blocked blocked \
                "ROCKNIX Control was detected, but its plugin path is unsafe for automatic deletion." \
                "Remove the symlinked or otherwise unsafe conflicting plugin manually."
            exit 1
        fi
    done < "${legacy_conflicts}"
    if ! fingerprint_legacy_rocknix_control \
        "${legacy_conflicts}" "${legacy_conflict_fingerprint}"; then
        record_install_event blocked blocked \
            "ROCKNIX Control removal approval could not be recorded safely." \
            "No conflicting plugin was removed."
        exit 1
    fi
    record_install_event running preflight \
        "Conflicting ROCKNIX Control detected; explicit permanent removal was approved."
else
    : > "${legacy_conflict_fingerprint}"
    record_install_event running preflight "Plugin conflict preflight passed."
fi

record_install_event running checking-releases \
    "Checking the newest published Decky release."
decky_metadata="${work_dir}/decky.json"
curl -fL "https://api.github.com/repos/${DECKY_REPOSITORY}/releases?per_page=20" \
    -o "${decky_metadata}"
decky_release_filter='[.[] | select(.draft == false) | . as $release | $release.assets[] | select(.name == "PluginLoader") | {version: $release.tag_name, url: .browser_download_url, digest: (.digest // "")}] | first'
decky_version="$(jq -r "${decky_release_filter} | .version // empty" \
    "${decky_metadata}")"
decky_url="$(jq -r "${decky_release_filter} | .url // empty" \
    "${decky_metadata}")"
decky_digest="$(jq -r "${decky_release_filter} | .digest // empty" \
    "${decky_metadata}")"
progress_decky_version="${decky_version}"
if [ -z "${decky_version}" ] || [ -z "${decky_url}" ]; then
    record_install_event failed failed \
        "Update failed: newest published Decky could not be resolved." \
        "Decky release metadata did not contain a usable PluginLoader asset."
    exit 1
fi
record_install_event running downloading "Downloading Decky ${decky_version}."
curl -fL "${decky_url}" -o "${work_dir}/PluginLoader"
if [ -n "${decky_digest}" ]; then
    expected="${decky_digest#sha256:}"
    actual="$(sha256sum "${work_dir}/PluginLoader" | cut -d' ' -f1)"
    if [ "${actual}" != "${expected}" ]; then
        record_install_event failed failed \
            "Update failed: Decky checksum mismatch." \
            "Downloaded PluginLoader did not match its published digest."
        exit 1
    fi
fi

if [ -n "${requested_version}" ]; then
    record_install_event running checking-releases \
        "Resolving RK-Enhanced ${requested_version}."
else
    record_install_event running checking-releases \
        "Resolving the latest RK-Enhanced release."
fi
metadata="${work_dir}/releases.json"
curl -fL "https://api.github.com/repos/${RKE_REPOSITORY}/releases?per_page=10" -o "${metadata}"
release_filter='[.[] | select(.draft == false) | . as $release | $release.assets[] | select(.name == "RK-Enhanced.zip") | {version: $release.tag_name, url: .browser_download_url, digest: (.digest // "")} | select($requested == "" or .version == $requested)] | first'
version="$(jq -r --arg requested "${requested_version}" "${release_filter} | .version // empty" "${metadata}")"
url="$(jq -r --arg requested "${requested_version}" "${release_filter} | .url // empty" "${metadata}")"
digest="$(jq -r --arg requested "${requested_version}" "${release_filter} | .digest // empty" "${metadata}")"
progress_target_version="${version}"

if [ -z "${version}" ] || [ -z "${url}" ]; then
    record_install_event failed failed \
        "Update failed: no RK-Enhanced release asset was found." \
        "The requested published release does not contain RK-Enhanced.zip."
    exit 1
fi
record_install_event running checking-releases \
    "Selected RK-Enhanced ${version} and Decky ${decky_version}."

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

record_install_event running downloading "Downloading RK-Enhanced ${version}."
curl -fL "${url}" -o "${work_dir}/RK-Enhanced.zip"
if [ -n "${digest}" ]; then
    expected="${digest#sha256:}"
    actual="$(sha256sum "${work_dir}/RK-Enhanced.zip" | cut -d' ' -f1)"
    if [ "${actual}" != "${expected}" ]; then
        record_install_event failed failed \
            "Update failed: RK-Enhanced release checksum mismatch." \
            "Downloaded RK-Enhanced.zip did not match its published digest."
        exit 1
    fi
fi

unzip -q "${work_dir}/RK-Enhanced.zip" -d "${work_dir}/release"
record_install_event running validating \
    "Validating downloaded Decky and RK-Enhanced files."
staged="${work_dir}/release/RK-Enhanced"
if [ ! -f "${staged}/plugin.json" ] || [ ! -f "${staged}/main.py" ] || \
   [ ! -f "${staged}/charging.py" ] || \
   [ ! -f "${staged}/runtime-restore.py" ] || \
   [ ! -f "${staged}/runtime-restore-guard.sh" ] || \
   [ ! -f "${staged}/dist/index.js" ] || [ ! -f "${staged}/updater.sh" ]; then
    record_install_event failed failed \
        "Update failed: invalid RK-Enhanced release layout." \
        "Required plugin files are missing."
    exit 1
fi
if grep -q 'rgb\.py' "${staged}/main.py" && [ ! -f "${staged}/rgb.py" ]; then
    record_install_event failed failed \
        "Update failed: release is missing its RGB backend." \
        "The staged backend imports rgb.py but the file is absent."
    exit 1
fi
if grep -q 'plugin_loader_recovery\.py' "${staged}/main.py" && \
   [ ! -f "${staged}/plugin_loader_recovery.py" ]; then
    record_install_event failed failed \
        "Update failed: release is missing its PluginLoader recovery helper." \
        "The staged backend imports plugin_loader_recovery.py but the file is absent."
    exit 1
fi
health_supported=0
if [ -e "${staged}/install-health.json" ] || \
   [ -e "${staged}/dist/frontend-integrity.json" ]; then
    if ! health_protocol_supported "${staged}"; then
        record_install_event failed failed \
            "Update failed: invalid install-health metadata or frontend integrity." \
            "The staged release did not pass frontend integrity validation."
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
    record_install_event failed failed \
        "Update failed: release is missing required install-health metadata." \
        "Only explicitly allowlisted legacy releases may omit health metadata."
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
last_installed_version_backup="${recovery_dir}/last-installed-version.previous"
record_install_event running backing-up \
    "Creating rollback files for the current RK-Enhanced and Decky installation."

if systemctl_bounded is-active --quiet "${PLUGIN_LOADER_UNIT}"; then
    service_was_active=1
fi
if [ ! -f "${PLUGIN_LOADER_PATH}" ]; then
    record_install_event failed failed \
        "Update failed: the current Decky executable is missing." \
        "PluginLoader could not be backed up."
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
if [ -f "${LAST_INSTALLED_VERSION_FILE}" ]; then
    last_installed_version_existed=1
    cp -p "${LAST_INSTALLED_VERSION_FILE}" "${last_installed_version_backup}"
fi
if [ -d "${PLUGIN_DIR}" ]; then
    plugin_had_current=1
    backup_dir="$(mktemp -d \
        "${BACKUP_ROOT}/RK-Enhanced-before-${version}-$(date +%Y%m%d-%H%M%S).XXXXXX")"
    rmdir "${backup_dir}"
fi

if [ -z "${frontend_requirement}" ] && \
   systemctl_bounded is-active --quiet steam-bigpicture.scope; then
    frontend_requirement="require-frontend"
fi

record_install_event running stopping-decky \
    "Stopping Decky through bounded PluginLoader maintenance."
if ! begin_plugin_loader_maintenance; then
    record_install_event failed failed \
        "Update failed: another PluginLoader maintenance action is running." \
        "PluginLoader maintenance lock is busy."
    exit 1
fi
loader_stop_attempted=1
if ! stop_plugin_loader_bounded; then
    record_install_event failed failed \
        "Update failed: Decky did not stop within the bounded timeout." \
        "PluginLoader stop timed out."
    exit 1
fi

late_conflicts="${work_dir}/rocknix-control-late-conflicts.txt"
late_conflict_fingerprint="${work_dir}/rocknix-control-late-conflicts.fingerprint"
if ! scan_legacy_rocknix_control "${late_conflicts}"; then
    record_install_event failed failed \
        "Update failed: plugin conflict revalidation could not be completed." \
        "No installed plugin files were changed."
    exit 1
fi
if ! fingerprint_legacy_rocknix_control \
    "${late_conflicts}" "${late_conflict_fingerprint}" || \
   ! cmp -s "${legacy_conflict_fingerprint}" \
        "${late_conflict_fingerprint}"; then
    record_install_event failed failed \
        "Update blocked because the approved ROCKNIX Control conflict set changed." \
        "Rerun the update to review and approve the current exact paths."
    exit 1
fi
if [ -s "${late_conflicts}" ]; then
    record_install_event running removing-conflict \
        "Revalidating and removing conflicting ROCKNIX Control installations without a backup."
    if ! remove_all_legacy_rocknix_control \
        "${legacy_conflicts}" "${legacy_conflict_fingerprint}"; then
        record_install_event failed failed \
            "Update failed while removing ROCKNIX Control or restoring native fancontrol." \
            "Conflict removal did not complete safely."
        exit 1
    fi
    record_install_event running removing-conflict \
        "ROCKNIX Control removal completed; native fancontrol is active."
fi

files_mutated=1
record_install_event running installing \
    "Installing RK-Enhanced ${version} and Decky ${decky_version}."
if [ "${plugin_had_current}" -eq 1 ]; then
    mv "${PLUGIN_DIR}" "${backup_dir}"
    jq -n \
        --argjson created_at "$(date +%s)" \
        --arg transaction_id "${progress_transaction_id}" \
        '{protocol: 1, created_at: $created_at, transaction_id: $transaction_id}' \
        > "${backup_dir}/.rke-backup-created.json"
    chmod 600 "${backup_dir}/.rke-backup-created.json"
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
        record_install_event failed failed \
            "Update failed: could not create a fresh install health challenge." \
            "Nonce-bound readiness challenge creation failed."
        exit 1
    fi
fi

record_install_event running starting-decky \
    "Starting Decky with the tentative RK-Enhanced installation."
systemctl_bounded start "${PLUGIN_LOADER_UNIT}"
if ! wait_for_plugin_loader_start 15; then
    record_install_event failed failed \
        "Update failed: Decky did not start within the bounded timeout." \
        "PluginLoader start timed out."
    exit 1
fi
# Lifecycle publication uses the same lock as maintenance. Release it only
# after the tentative unit is active, then require the nonce-bound backend and
# frontend responses while the separate transaction lock prevents overlap.
end_plugin_loader_maintenance
if [ "${health_supported}" -eq 1 ]; then
    record_install_event running verifying \
        "Verifying RK-Enhanced ${version} with Decky ${decky_version}."
    if ! wait_for_rke_health "${frontend_requirement}"; then
        record_install_event failed failed \
            "Update failed: RK-Enhanced did not pass required readiness checks." \
            "Backend or frontend readiness verification timed out."
        exit 1
    fi
    if [ "${frontend_requirement}" = "require-frontend" ]; then
        install_result="backend and frontend verified"
    else
        install_result="backend verified; frontend not tested because Steam is inactive"
    fi
else
    install_result="legacy release; backend readiness unavailable"
fi
installed_version_temporary="$(mktemp \
    "${SETTINGS_DIR}/installed-version.XXXXXX")"
printf '%s\n' "${version}" > "${installed_version_temporary}"
chmod 600 "${installed_version_temporary}"
mv "${installed_version_temporary}" "${INSTALLED_VERSION_FILE}"
installed_version_temporary=""
if [ -n "${progress_source_version}" ] && \
   [ "${progress_source_version}" != "${version}" ]; then
    last_installed_version_temporary="$(mktemp \
        "${SETTINGS_DIR}/last-installed-version.XXXXXX")"
    printf '%s\n' "${progress_source_version}" > "${last_installed_version_temporary}"
    chmod 600 "${last_installed_version_temporary}"
    last_installed_version_changed=1
    mv "${last_installed_version_temporary}" "${LAST_INSTALLED_VERSION_FILE}"
    last_installed_version_temporary=""
fi
record_install_event succeeded completed \
    "Installed ${version} with Decky ${decky_version}; ${install_result}."
if [ "${health_created}" -eq 1 ]; then
    clear_install_health
    health_created=0
fi
transaction_committed=1
end_install_transaction
exit 0
