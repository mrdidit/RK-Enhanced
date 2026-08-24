"""Strict RK-Enhanced boundary for ROCKNIX charging helper APIs."""

from contextlib import contextmanager
from copy import deepcopy
import fcntl
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path


BATTERY_HELPER = Path("/usr/bin/charging_mode")
PUMP_HELPER = Path("/usr/bin/kpfe_fast_charge")
ALLOWED_LIMITS = (50, 60, 70, 80, 90, 100)
BATTERY_MODES = ("normal", "bypass", "limit")
PUMP_PROFILES = ("normal", "slow", "fast")
PUMP_STATES = ("idle", "pump-init", "pump", "error")
CHARGE_BEHAVIOURS = ("auto", "inhibit-charge")
STATUS_TIMEOUT = 5
MUTATION_TIMEOUT = 30
TRUSTED_ENVIRONMENT = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C",
    "LC_ALL": "C",
}


@contextmanager
def _exclusive_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _helper_available(path):
    path = Path(path)
    return path.is_file() and os.access(path, os.X_OK)


def _execute(path, arguments, timeout):
    """Execute one allowlisted helper command without a shell."""
    path = Path(path)
    command = [str(path), *arguments]
    result = {
        "command": command,
        "started": False,
        "ok": False,
        "timed_out": False,
        "exit_status": None,
        "stdout": "",
        "stderr": "",
    }
    if not _helper_available(path):
        result["stderr"] = f"{path} is missing or not executable"
        return result
    try:
        process = subprocess.Popen(
            command,
            cwd="/",
            env=dict(TRUSTED_ENVIRONMENT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            close_fds=True,
            start_new_session=True,
        )
        result["started"] = True
    except OSError as reason:
        result["stderr"] = str(reason)
        return result
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        result["timed_out"] = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
    result.update({
        "exit_status": process.returncode,
        "stdout": stdout.strip(),
        "stderr": stderr.strip(),
        "ok": not result["timed_out"] and process.returncode == 0,
    })
    return result


def _fields(output):
    values = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        if "=" not in line:
            raise ValueError(f"malformed status line: {line}")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or key in values:
            raise ValueError(f"invalid or duplicate status field: {key or '<empty>'}")
        values[key] = value
    return values


def _required(values, key):
    value = values.get(key, "")
    if not value:
        raise ValueError(f"missing or empty status field: {key}")
    return value


def _integer(values, key, minimum=None, maximum=None, signed=False):
    value = _required(values, key)
    pattern = r"-?\d+" if signed else r"\d+"
    if not re.fullmatch(pattern, value):
        raise ValueError(f"invalid integer status field: {key}")
    number = int(value)
    if minimum is not None and number < minimum:
        raise ValueError(f"status field below minimum: {key}")
    if maximum is not None and number > maximum:
        raise ValueError(f"status field above maximum: {key}")
    return number


def _boolean(values, key):
    value = _required(values, key)
    if value not in ("0", "1"):
        raise ValueError(f"invalid boolean status field: {key}")
    return value == "1"


def _selected(values, key, allowlist=None):
    raw = _required(values, key)
    selected = re.findall(r"\[([^\[\]]+)\]", raw)
    if len(selected) != 1 or not selected[0].strip():
        raise ValueError(f"status field has no unique selected value: {key}")
    value = selected[0].strip()
    if allowlist is not None and value not in allowlist:
        raise ValueError(f"unsupported selected status value: {key}")
    return value


def _base_status(available, captured_at, command):
    return {
        "available": available,
        "valid": False,
        "stale": False,
        "transitional": False,
        "captured_at": captured_at,
        "error": "",
        "refresh_error": "",
        "command": command,
    }


def _command_error(result):
    if not result["started"]:
        return result["stderr"] or "helper is unavailable"
    if result["timed_out"]:
        return "helper timed out; observed status is indeterminate"
    return result["stderr"] or f"helper exited with status {result['exit_status']}"


def _reported_unsupported(result):
    return (not result["ok"] and
            "not supported on this device" in result["stderr"].lower())


def _live_bypass(battery):
    return bool(
        battery and battery.get("available") and battery.get("valid") and
        not battery.get("stale") and not battery.get("transitional") and
        battery.get("mode") == "bypass")


def _parse_battery(result, available, captured_at):
    parsed = _base_status(available, captured_at, result)
    if not result["ok"]:
        parsed["error"] = _command_error(result)
        return parsed
    try:
        values = _fields(result["stdout"])
        mode = _required(values, "mode")
        if mode not in BATTERY_MODES:
            raise ValueError("unsupported battery mode")
        limit = None
        if mode == "limit":
            limit = _integer(values, "limit", 0, 100)
            if limit not in ALLOWED_LIMITS:
                raise ValueError("unsupported battery limit")
        capacity = _integer(values, "capacity", 0, 100)
        behaviour = _selected(values, "charge_behaviour", CHARGE_BEHAVIOURS)
        start = _integer(values, "start_threshold", 0, 100)
        end = _integer(values, "end_threshold", 0, 100)
        battery_status = _required(values, "status")

        contradictions = []
        if mode == "normal":
            if behaviour != "auto":
                contradictions.append("Normal policy is not observing auto behaviour")
            if (start, end) != (95, 100):
                contradictions.append("Normal policy thresholds are still transitioning")
        elif mode == "bypass" and behaviour != "inhibit-charge":
            contradictions.append("Bypass policy is not observing inhibited behaviour")
        elif mode == "limit":
            expected_start = max(50, limit - 5)
            expected_end = max(55, limit)
            if (start, end) != (expected_start, expected_end):
                contradictions.append("Limit thresholds do not match the configured limit")
            if capacity >= limit and behaviour != "inhibit-charge":
                contradictions.append("Limit endpoint has not inhibited charging")
            elif capacity <= limit - 5 and behaviour != "auto":
                contradictions.append("Limit hysteresis restart has not returned to auto")

        parsed.update({
            "valid": True,
            "mode": mode,
            "limit": limit,
            "capacity": capacity,
            "charge_behaviour": behaviour,
            "start_threshold": start,
            "end_threshold": end,
            "battery_status": battery_status,
            "transitional": bool(contradictions),
            "transition_reason": "; ".join(contradictions),
        })
    except ValueError as reason:
        parsed["error"] = str(reason)
    return parsed


def _parse_pump(result, available, captured_at):
    parsed = _base_status(available, captured_at, result)
    if not result["ok"]:
        parsed["error"] = _command_error(result)
        return parsed
    try:
        values = _fields(result["stdout"])
        enabled = _boolean(values, "enabled")
        profile = _required(values, "profile")
        if profile not in PUMP_PROFILES:
            raise ValueError("unsupported pump profile")
        state = _required(values, "state")
        if state not in PUMP_STATES:
            raise ValueError("unsupported pump state")
        last_error = _integer(values, "last_error", signed=True)
        last_end_reason = _required(values, "last_end_reason")
        requested_voltage_uv = _integer(
            values, "requested_voltage_uv", minimum=0)
        usb_online = _boolean(values, "usb_online")
        usb_type = _selected(values, "usb_type")
        charge_behaviour = _selected(
            values, "charge_behaviour", CHARGE_BEHAVIOURS)
        master_online = _boolean(values, "master_online")
        master_health = _required(values, "master_health")
        slave_online = _boolean(values, "slave_online")
        slave_health = _required(values, "slave_health")

        if state == "error" or last_error != 0:
            phase = "error"
        elif (not enabled and profile == "normal" and state == "idle" and
              not master_online and not slave_online):
            phase = "off"
        elif enabled and profile in ("slow", "fast") and state in ("idle", "pump-init"):
            phase = "starting"
        elif (enabled and profile in ("slow", "fast") and state == "pump" and
              master_online and slave_online and master_health == "Good" and
              slave_health == "Good"):
            phase = "active"
        else:
            phase = "transitional"

        parsed.update({
            "valid": True,
            "enabled": enabled,
            "profile": profile,
            "state": state,
            "phase": phase,
            "last_error": last_error,
            "last_end_reason": last_end_reason,
            "requested_voltage_uv": requested_voltage_uv,
            "usb_online": usb_online,
            "usb_type": usb_type,
            "charge_behaviour": charge_behaviour,
            "master_online": master_online,
            "master_health": master_health,
            "slave_online": slave_online,
            "slave_health": slave_health,
            "transitional": phase == "transitional",
            "transition_reason": (
                "Coordinator and pump fields do not form an Off, Starting, Active, or Error state"
                if phase == "transitional" else ""
            ),
        })
        _enforce_pump_active_invariants(parsed)
    except ValueError as reason:
        parsed["error"] = str(reason)
    return parsed


def _append_transition(component, reason):
    component["transitional"] = True
    current = component.get("transition_reason", "")
    component["transition_reason"] = f"{current}; {reason}" if current else reason


def _pump_active_issues(pump):
    """Return violations which make an observed Active phase impossible."""
    issues = []
    if (not pump.get("enabled") or pump.get("profile") not in ("slow", "fast") or
            pump.get("state") != "pump"):
        issues.append("Active pumps do not report an enabled pump coordinator")
    if pump.get("last_error") != 0:
        issues.append("Active pumps report a coordinator error")
    if not pump.get("usb_online"):
        issues.append("Active pumps are reporting source loss")
    if pump.get("usb_type") != "PD_PPS":
        issues.append("Active pumps do not report a selected PD-PPS source")
    if pump.get("charge_behaviour") != "auto":
        issues.append("Active pumps do not report auto charging behaviour")
    if (not pump.get("master_online") or pump.get("master_health") != "Good" or
            not pump.get("slave_online") or pump.get("slave_health") != "Good"):
        issues.append("Active pumps do not report both pumps online and healthy")
    return issues


def _enforce_pump_active_invariants(pump):
    """Never expose Active unless the pump snapshot is intrinsically coherent."""
    if not pump.get("valid") or pump.get("phase") != "active":
        return
    issues = _pump_active_issues(pump)
    if issues:
        _append_transition(pump, "; ".join(issues))
        pump["phase"] = "transitional"


def _reconcile_status_pair(battery, pump):
    """Reject coherent-looking fields that contradict across helper snapshots."""
    # Pump-intrinsic Active requirements never depend on the battery helper.
    # Keep this guard here as well as in parsing so an invalid battery snapshot
    # cannot bypass the final pair-level safety boundary.
    _enforce_pump_active_invariants(pump)
    if not battery.get("valid") or not pump.get("valid"):
        return

    contradictions = []
    battery_behaviour = battery.get("charge_behaviour")
    pump_behaviour = pump.get("charge_behaviour")
    pump_phase = pump.get("phase")

    if (battery_behaviour and pump_behaviour and
            battery_behaviour != pump_behaviour):
        contradictions.append(
            "Battery and pump snapshots report different charging behaviour")

    if pump_phase in ("starting", "active"):
        if battery.get("mode") == "bypass" or battery_behaviour != "auto":
            contradictions.append(
                "Pump activity conflicts with an inhibited battery policy")
        if pump_phase == "active" and not pump.get("usb_online"):
            contradictions.append(
                "Active pumps are reporting source loss")
        if pump_phase == "active" and pump.get("usb_type") != "PD_PPS":
            contradictions.append(
                "Active pumps do not report a selected PD-PPS source")

    if not contradictions:
        return
    reason = "; ".join(contradictions)
    _append_transition(battery, reason)
    _append_transition(pump, reason)
    if pump_phase in ("starting", "active"):
        pump["phase"] = "transitional"


class ChargingController:
    """Serializes and validates all RKE charging helper interactions."""

    def __init__(self, settings_dir, battery_helper=BATTERY_HELPER,
                 pump_helper=PUMP_HELPER):
        self.battery_helper = Path(battery_helper)
        self.pump_helper = Path(pump_helper)
        self.lock_path = Path(settings_dir) / "charging-control.lock"
        self.thread_lock = threading.RLock()
        self.last_good_battery = None
        self.last_good_pump = None
        self.latest_status = None

    def _retain_or_stale(self, current, previous):
        if current["valid"]:
            if not current["transitional"]:
                return current, deepcopy(current)
            return current, previous
        if previous is None:
            return current, previous
        stale = deepcopy(previous)
        stale.update({
            "available": current["available"],
            "stale": True,
            "refresh_error": current["error"],
            "command": current["command"],
        })
        return stale, previous

    def _status_locked(self, operation=None):
        captured_at = time.time()
        battery_command = _execute(
            self.battery_helper, ["status"], STATUS_TIMEOUT)
        pump_command = _execute(
            self.pump_helper, ["status"], STATUS_TIMEOUT)
        battery_available = (
            _helper_available(self.battery_helper) and
            not _reported_unsupported(battery_command))
        pump_available = (
            _helper_available(self.pump_helper) and
            not _reported_unsupported(pump_command))
        battery = _parse_battery(
            battery_command, battery_available, captured_at)
        pump = _parse_pump(
            pump_command, pump_available, captured_at)
        _reconcile_status_pair(battery, pump)
        battery, self.last_good_battery = self._retain_or_stale(
            battery, self.last_good_battery)
        pump, self.last_good_pump = self._retain_or_stale(
            pump, self.last_good_pump)
        status = {
            "captured_at": captured_at,
            "battery": battery,
            "pump": pump,
            "coherent": bool(
                battery.get("available") and battery.get("valid") and
                not battery.get("stale") and not battery.get("transitional") and
                pump.get("available") and pump.get("valid") and
                not pump.get("stale") and not pump.get("transitional")),
            "operation": operation,
        }
        self.latest_status = deepcopy(status)
        return status

    def get_status(self):
        with self.thread_lock, _exclusive_lock(self.lock_path):
            return self._status_locked()

    def cached_battery(self):
        with self.thread_lock:
            if not self.latest_status:
                return None
            return deepcopy(self.latest_status["battery"])

    def cached_bypass_active(self):
        with self.thread_lock:
            status = self.latest_status
            return bool(
                status and status.get("coherent") and
                _live_bypass(status.get("battery")))

    def set_battery_policy(self, mode, limit=None):
        if not isinstance(mode, str) or mode not in BATTERY_MODES:
            raise ValueError("battery policy must be normal, bypass, or limit")
        if mode == "limit":
            if isinstance(limit, bool):
                raise ValueError("battery limit must be numeric")
            if isinstance(limit, int):
                parsed_limit = limit
            elif isinstance(limit, str) and re.fullmatch(r"\d+", limit):
                parsed_limit = int(limit)
            else:
                raise ValueError("battery limit must be numeric") from None
            if parsed_limit not in ALLOWED_LIMITS:
                raise ValueError("battery limit must be 50, 60, 70, 80, 90, or 100")
            arguments = ["limit", str(parsed_limit)]
        else:
            if limit is not None:
                raise ValueError("normal and bypass policies do not accept a limit")
            arguments = [mode]
        with self.thread_lock, _exclusive_lock(self.lock_path):
            operation = _execute(
                self.battery_helper, arguments, MUTATION_TIMEOUT)
            operation["kind"] = "battery-policy"
            operation["requested"] = (
                f"limit-{parsed_limit}" if mode == "limit" else mode)
            return self._status_locked(operation)

    def set_pump_profile(self, profile, experimental_risk_confirmed):
        if not isinstance(profile, str) or profile not in PUMP_PROFILES:
            raise ValueError("pump profile must be normal, slow, or fast")
        if profile == "normal":
            if experimental_risk_confirmed not in (False, None):
                raise ValueError("normal pump profile does not accept risk confirmation")
            arguments = ["disable"]
        else:
            if experimental_risk_confirmed is not True:
                raise ValueError("fresh experimental-risk confirmation is required")
            arguments = [
                "enable", profile, "--acknowledge-experimental-risk"]
        with self.thread_lock, _exclusive_lock(self.lock_path):
            operation = _execute(
                self.pump_helper, arguments, MUTATION_TIMEOUT)
            operation["kind"] = "pump-profile"
            operation["requested"] = profile
            return self._status_locked(operation)
