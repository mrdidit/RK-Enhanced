"""Root Decky backend for RK-Enhanced on ROCKNIX."""

import asyncio
from contextlib import contextmanager
import fcntl
import json
import os
import signal
import shutil
import subprocess
import threading
import time
from pathlib import Path
from tempfile import NamedTemporaryFile

import decky

DEFAULT_PRESET = "RK-E Default"
LEGACY_DEFAULT_PRESETS = ("Rocknix Custom", "ROCKNIX Default", "Steam Default")
SETTINGS_FILE = "settings.json"
UPDATE_STATUS_FILE = "update-status.txt"
INSTALLED_VERSION_FILE = "installed-version.txt"
UPDATE_REPOSITORY = "mrdidit/RK-Enhanced"
FAN_CONFIG = Path("/storage/.config/fancontrol.conf")
CPU_ROOT = Path("/sys/devices/system/cpu/cpufreq")
GPU_ROOT = Path("/sys/class/devfreq")
KGSL_GPU_ROOT = Path("/sys/class/kgsl/kgsl-3d0")
CHARGE_BEHAVIOUR = Path("/sys/class/power_supply/battery/charge_behaviour")


def _read(path, default=""):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def _read_int(path, default=0):
    try:
        return int(_read(path))
    except (TypeError, ValueError):
        return default


def _read_ints(path):
    try:
        return sorted({int(value) for value in _read(path).split() if int(value) > 0})
    except ValueError:
        return []


def _run(command, check=True, timeout=15):
    environment = os.environ.copy()
    # PyInstaller-based Decky builds prepend their private libraries to child
    # processes. System tools such as curl must use ROCKNIX's own OpenSSL.
    for variable in ("LD_LIBRARY_PATH", "LD_PRELOAD"):
        original = environment.pop(f"{variable}_ORIG", None)
        if original:
            environment[variable] = original
        else:
            environment.pop(variable, None)
    process = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, timeout=timeout, check=False,
                             env=environment)
    if check and process.returncode:
        raise RuntimeError(process.stderr.strip() or f"command failed: {command[0]}")
    return process.stdout.strip()


def _rocknix_env(name):
    # Device paths are created by ROCKNIX quirk scripts and loaded by /etc/profile.
    output = _run(["/bin/sh", "-c", f'. /etc/profile >/dev/null 2>&1; printf "%s" "${{{name}}}"'], check=False)
    return output


def _get_setting(name, default=""):
    if shutil.which("get_setting"):
        value = _run(["get_setting", name], check=False)
        return value or default
    config = Path("/storage/.config/system/configs/system.cfg")
    for line in _read(config).splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1]
    return default


def _set_setting(name, value):
    if shutil.which("set_setting"):
        _run(["set_setting", name, value])
        return
    config = Path("/storage/.config/system/configs/system.cfg")
    config.parent.mkdir(parents=True, exist_ok=True)
    lines = _read(config).splitlines()
    replacement = f"{name}={value}"
    lines = [replacement if line.startswith(name + "=") else line for line in lines]
    if not any(line.startswith(name + "=") for line in _read(config).splitlines()):
        lines.append(replacement)
    _atomic_text(config, "\n".join(lines) + "\n")


def _atomic_text(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


@contextmanager
def _exclusive_file_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _active_charge_behaviour(path=CHARGE_BEHAVIOUR):
    value = _read(path)
    for option in value.split():
        if option.startswith("[") and option.endswith("]"):
            return option[1:-1]
    return value


def _cpu_capabilities():
    result = []
    policies = list(CPU_ROOT.glob("policy*"))
    policies.sort(key=lambda item: int(item.name[6:]))
    for policy in policies:
        frequencies = _read_ints(policy / "scaling_available_frequencies")
        boost_frequencies = _read_ints(policy / "scaling_boost_frequencies")
        boost_enabled = bool(_read_int(
            policy / "boost", _read_int(CPU_ROOT / "boost")))
        if not frequencies:
            frequencies = sorted({value for value in (
                _read_int(policy / "cpuinfo_min_freq"),
                _read_int(policy / "cpuinfo_max_freq"),
            ) if value})
        if not frequencies:
            continue
        cpuinfo_maximum = _read_int(policy / "cpuinfo_max_freq")
        if boost_enabled and cpuinfo_maximum > max(frequencies):
            boost_frequencies = sorted(set(boost_frequencies + [cpuinfo_maximum]))
        maximum_frequencies = sorted(set(
            frequencies + (boost_frequencies if boost_enabled else [])
        ))
        cpus = _read(policy / "affected_cpus", policy.name[6:]).split()
        result.append({
            "id": policy.name[6:], "cpus": cpus, "frequencies": frequencies,
            "boost_frequencies": boost_frequencies,
            "boost_enabled": boost_enabled,
            "maximum_frequencies": maximum_frequencies,
            "governors": sorted(set(_read(policy / "scaling_available_governors").split())),
            "current": _read_int(policy / "scaling_cur_freq"),
            "minimum": _read_int(policy / "scaling_min_freq"),
            "maximum": _read_int(policy / "scaling_max_freq"),
            "effective_maximum": max(maximum_frequencies),
        })
    return result


def _gpu_path():
    for device in sorted(GPU_ROOT.glob("*")):
        if "gpu" in device.name.lower() and (device / "available_frequencies").exists():
            return device
    return None


def _gpu_capability():
    device = _gpu_path()
    if not device:
        return {"available": False, "frequencies": [], "governors": [],
                "current": 0, "minimum": 0, "maximum": 0}
    return {
        "available": True,
        "frequencies": _read_ints(device / "available_frequencies"),
        "governors": sorted(set(_read(device / "available_governors").split())),
        "current": _read_int(device / "cur_freq"),
        "minimum": _read_int(device / "min_freq"),
        "maximum": _read_int(device / "max_freq"),
    }


def _normalize_fan_curve(points):
    cleaned = {}
    for point in points or []:
        try:
            temp, pwm = int(point[0]), int(point[1])
        except (TypeError, ValueError, IndexError):
            continue
        if temp > 0:
            cleaned[temp] = pwm
    visible = [[temp, cleaned[temp]] for temp in sorted(cleaned)]
    if not visible:
        return [[55000, 51], [85000, 255]]
    if len(visible) == 1:
        temp, pwm = visible[0]
        if temp <= 100000:
            visible.append([min(120000, temp + 20000), pwm])
        else:
            visible.insert(0, [max(10000, temp - 20000), pwm])
    return visible[:16]


def _fan_curve():
    speeds, temps = [], []
    for line in _read(FAN_CONFIG).splitlines():
        line = line.strip()
        if line.startswith("SPEEDS=(") and line.endswith(")"):
            try:
                speeds = [int(value) for value in line[8:-1].split()]
            except ValueError:
                speeds = []
        elif line.startswith("TEMPS=(") and line.endswith(")"):
            try:
                temps = [int(value) for value in line[7:-1].split()]
            except ValueError:
                temps = []
    # One speed per temperature; the last configured value wins.
    return _normalize_fan_curve([[temp, speed] for temp, speed in zip(temps, speeds)])


def _fan_pwm_path():
    configured = _rocknix_env("DEVICE_PWM_FAN")
    if configured and Path(configured).is_file():
        return Path(configured)
    for candidate in sorted(Path("/sys/class/hwmon").glob("hwmon*/pwm1")):
        if candidate.is_file():
            return candidate
    return None


def _write_fan_curve(points):
    descending = sorted(points, key=lambda point: point[0], reverse=True)
    if not any(point[0] == 0 for point in descending):
        descending.append([0, 0])
    speeds = " ".join(str(point[1]) for point in descending)
    temps = " ".join(str(point[0]) for point in descending)
    _atomic_text(FAN_CONFIG, f"SPEEDS=({speeds})\nTEMPS=({temps})\n")


def _fancontrol_main_pid(exclude=None):
    cgroups = (
        Path("/sys/fs/cgroup/systemd/system.slice/fancontrol.service/cgroup.procs"),
        Path("/sys/fs/cgroup/pids/system.slice/fancontrol.service/cgroup.procs"),
        Path("/sys/fs/cgroup/unified/system.slice/fancontrol.service/cgroup.procs"),
        Path("/sys/fs/cgroup/system.slice/fancontrol.service/cgroup.procs"),
    )
    for cgroup in cgroups:
        for value in _read(cgroup).splitlines():
            try:
                pid = int(value)
            except ValueError:
                continue
            if pid == exclude:
                continue
            if _read(Path("/proc") / str(pid) / "comm") == "fancontrol":
                return pid
    return None


def _restart_fancontrol():
    # PluginLoader runs through FEX on ROCKNIX. A systemctl subprocess launched
    # from that worker hangs even though the same command works over SSH. The
    # native service has Restart=on-failure, so terminate its main process and
    # let systemd immediately recreate it with the newly written configuration.
    previous = _fancontrol_main_pid()
    if previous is None:
        raise RuntimeError("native fancontrol.service is not running")
    # SIGTERM is considered a clean exit for this shell service and does not
    # trigger Restart=on-failure. SIGKILL reliably asks systemd to recover it.
    os.kill(previous, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = _fancontrol_main_pid(exclude=previous)
        if current is not None:
            return current
        time.sleep(0.1)
    raise RuntimeError("native fancontrol did not restart within 5 seconds")


def _scheduler_capabilities():
    result = ["kernel"]
    unit_exists = any(path.exists() for path in (
        Path("/etc/systemd/system/scx_lavd.service"),
        Path("/usr/lib/systemd/system/scx_lavd.service"),
        Path("/storage/.config/system.d/scx_lavd.service"),
    ))
    if unit_exists and Path("/sys/kernel/sched_ext").exists():
        result.append("lavd")
    return result


def _service_active(unit):
    return _run(["systemctl", "is-active", unit], check=False) == "active"


def _capabilities():
    cpu = _cpu_capabilities()
    common = sorted(set.intersection(*(set(item["governors"]) for item in cpu))) if cpu else []
    return {
        "cpu": cpu, "cpu_governors": common, "gpu": _gpu_capability(),
        "schedulers": _scheduler_capabilities(),
        "fan_available": bool(_rocknix_env("DEVICE_PWM_FAN") or FAN_CONFIG.exists()),
    }


def _current_profile(capabilities=None):
    capabilities = capabilities or _capabilities()
    cpu = capabilities["cpu"]
    gpu = capabilities["gpu"]
    governors = [_read(CPU_ROOT / f"policy{item['id']}" / "scaling_governor") for item in cpu]
    profile = {
        "cpu_governor": governors[0] if governors else "",
        "cpu_min": {item["id"]: item["minimum"] for item in cpu},
        "cpu_max": {item["id"]: item["maximum"] for item in cpu},
        "cooling_profile": "custom",
        "fan_curve": _fan_curve(),
        "cpu_scheduler": "lavd" if (
            "lavd" in capabilities["schedulers"] and
            _read("/sys/kernel/sched_ext/state") == "enabled"
        ) else "kernel",
    }
    if gpu["available"]:
        device = _gpu_path()
        profile.update({"gpu_governor": _read(device / "governor"),
                        "gpu_min": _read_int(device / "min_freq"),
                        "gpu_max": _read_int(device / "max_freq")})
    return profile


def _validate_name(name):
    clean = str(name).strip()
    if not clean or len(clean) > 80 or any(char in clean for char in "\r\n\0"):
        raise ValueError("preset name must contain 1-80 valid characters")
    return clean


def _validate_profile(profile, capabilities):
    if not isinstance(profile, dict):
        raise ValueError("profile must be an object")
    clean = dict(profile)
    if clean.get("cpu_governor") not in capabilities["cpu_governors"]:
        raise ValueError("unsupported CPU governor")
    clean_min, clean_max = {}, {}
    for policy in capabilities["cpu"]:
        pid, frequencies = policy["id"], policy["frequencies"]
        maximum_frequencies = policy["maximum_frequencies"]
        low = int(clean.get("cpu_min", {}).get(pid, -1))
        high = int(clean.get("cpu_max", {}).get(pid, -1))
        if low not in frequencies or high not in maximum_frequencies or low > high:
            raise ValueError(f"invalid CPU range for policy {pid}")
        clean_min[pid], clean_max[pid] = low, high
    clean["cpu_min"], clean["cpu_max"] = clean_min, clean_max
    gpu = capabilities["gpu"]
    if gpu["available"]:
        if clean.get("gpu_governor") not in gpu["governors"]:
            raise ValueError("unsupported GPU governor")
        minimum = int(clean.get("gpu_min", gpu["frequencies"][0]))
        maximum = int(clean.get("gpu_max", -1))
        if minimum not in gpu["frequencies"] or maximum not in gpu["frequencies"] or minimum > maximum:
            raise ValueError("invalid GPU frequency range")
        clean["gpu_min"], clean["gpu_max"] = minimum, maximum
    else:
        clean.pop("gpu_governor", None)
        clean.pop("gpu_min", None)
        clean.pop("gpu_max", None)
    # RK-E presets always contain an independent curve. ROCKNIX's effective
    # cooling mode decides whether that curve may be installed; RK-E never
    # changes cooling.profile itself.
    clean["cooling_profile"] = "custom"
    curve = clean.get("fan_curve")
    if not isinstance(curve, list) or not 2 <= len(curve) <= 16:
        raise ValueError("fan curve must contain 2-16 points")
    previous_temp, previous_pwm, clean_curve = -1, -1, []
    for point in curve:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError("invalid fan curve point")
        temp, pwm = int(point[0]), int(point[1])
        if not 10000 <= temp <= 120000 or not 0 <= pwm <= 255 or temp <= previous_temp:
            raise ValueError("editable fan temperatures must be 10-120°C and strictly rise; PWM must be 0-255")
        if pwm < previous_pwm:
            raise ValueError("fan PWM cannot decrease as temperature rises")
        previous_temp, previous_pwm = temp, pwm
        clean_curve.append([temp, pwm])
    clean["fan_curve"] = clean_curve
    if clean.get("cpu_scheduler") not in capabilities["schedulers"]:
        raise ValueError("unsupported CPU scheduler")
    return clean


def _write_range(path, low, high):
    current_low = _read_int(path / "scaling_min_freq")
    current_high = _read_int(path / "scaling_max_freq")
    if high < current_low:
        (path / "scaling_min_freq").write_text(str(low))
        (path / "scaling_max_freq").write_text(str(high))
    elif low > current_high:
        (path / "scaling_max_freq").write_text(str(high))
        (path / "scaling_min_freq").write_text(str(low))
    else:
        (path / "scaling_min_freq").write_text(str(low))
        (path / "scaling_max_freq").write_text(str(high))


def _write_gpu_range(path, low, high):
    current_low = _read_int(path / "min_freq")
    current_high = _read_int(path / "max_freq")
    if high < current_low:
        (path / "min_freq").write_text(str(low))
        (path / "max_freq").write_text(str(high))
    elif low > current_high:
        (path / "max_freq").write_text(str(high))
        (path / "min_freq").write_text(str(low))
    else:
        (path / "min_freq").write_text(str(low))
        (path / "max_freq").write_text(str(high))


class Plugin:
    def __init__(self):
        settings = os.environ.get("DECKY_PLUGIN_SETTINGS_DIR")
        self.settings_dir = Path(settings) if settings else Path(__file__).resolve().parent / "settings"
        self.settings_path = self.settings_dir / SETTINGS_FILE
        self.legacy_fan_guard_marker = self.settings_dir / "fan-curve-session.active"
        self.runtime_marker = self.settings_dir / "runtime-session.active"
        self.runtime_state_path = self.settings_dir / "runtime-session.json"
        self.runtime_lock_path = self.settings_dir / "runtime-session.lock"
        self.runtime_restore_path = self.settings_dir / "runtime-restore.py"
        self.runtime_guard_path = self.settings_dir / "runtime-restore-guard.sh"
        self.canonical_fan_config = self.settings_dir / "rocknix-custom-fancontrol.conf"
        self.active_preset = DEFAULT_PRESET
        self.active_appid = ""
        self.last_cpu_sample = None
        self.last_gpu_sample = None
        self.gamescope_pid = None
        self.gpu_fdinfo_paths = []
        self.gpu_fdinfo_refresh = 0.0
        self.battery_discharge_ema = None
        self.battery_discharge_samples = 0
        self.battery_discharge_last_sample = 0.0
        self.log_offsets = {}
        self.latest_release_cache = (0.0, [])
        self.session_lock = threading.RLock()
        self.lock = None
        self.game_watch_task = None

    def _load(self):
        try:
            data = json.loads(self.settings_path.read_text())
            if not isinstance(data, dict):
                raise ValueError
        except (OSError, ValueError, json.JSONDecodeError):
            data = {"presets": {}, "game_profiles": {}}
        data.setdefault("presets", {})
        data.setdefault("game_profiles", {})
        data.setdefault("experimental_unlocked", False)
        changed = False
        previous_schema = int(data.get("preset_schema", 0) or 0)
        if previous_schema < 2 and self.settings_path.exists():
            migration_backup = self.settings_dir / "settings-before-preset-schema-2.json"
            if not migration_backup.exists():
                shutil.copy2(self.settings_path, migration_backup)
        legacy = next((name for name in LEGACY_DEFAULT_PRESETS if name in data["presets"]), None)
        if "system_fan_curve" not in data:
            source = data["presets"].get(legacy, {}) if legacy else {}
            data["system_fan_curve"] = _normalize_fan_curve(source.get("fan_curve", _fan_curve()))
            changed = True
        if legacy and DEFAULT_PRESET not in data["presets"]:
            data["presets"][DEFAULT_PRESET] = json.loads(json.dumps(data["presets"][legacy]))
            data["presets"][DEFAULT_PRESET]["fan_curve"] = json.loads(
                json.dumps(data["system_fan_curve"]))
            changed = True
        for old_name in LEGACY_DEFAULT_PRESETS:
            if old_name in data["presets"]:
                del data["presets"][old_name]
                changed = True
        migrated_profiles = {
            appid: DEFAULT_PRESET if preset in LEGACY_DEFAULT_PRESETS else preset
            for appid, preset in data["game_profiles"].items()
        }
        if migrated_profiles != data["game_profiles"]:
            data["game_profiles"] = migrated_profiles
            changed = True
        if DEFAULT_PRESET not in data["presets"]:
            data["presets"][DEFAULT_PRESET] = _current_profile()
            changed = True
        if previous_schema < 2:
            # The new model seeds RK-E Default from the protected system curve
            # exactly once, then lets both copies evolve independently.
            data["presets"][DEFAULT_PRESET]["fan_curve"] = json.loads(
                json.dumps(data["system_fan_curve"]))
            data["presets"][DEFAULT_PRESET]["cooling_profile"] = "custom"
            data["steam_default_original"] = json.loads(
                json.dumps(data["presets"][DEFAULT_PRESET]))
            data["preset_schema"] = 2
            changed = True
        if not isinstance(data.get("steam_default_original"), dict):
            data["steam_default_original"] = json.loads(json.dumps(data["presets"][DEFAULT_PRESET]))
            changed = True
        if data.get("steam_default") not in data["presets"]:
            data["steam_default"] = DEFAULT_PRESET
            changed = True
        for preset in data["presets"].values():
            if preset.get("cooling_profile") != "custom":
                preset["cooling_profile"] = "custom"
                changed = True
            normalized = _normalize_fan_curve(preset.get("fan_curve", []))
            if preset.get("fan_curve") != normalized:
                preset["fan_curve"] = normalized
                changed = True
        normalized_system = _normalize_fan_curve(data.get("system_fan_curve", []))
        if data.get("system_fan_curve") != normalized_system:
            data["system_fan_curve"] = normalized_system
            changed = True
        if changed:
            self._save(data)
        self._write_canonical_fan_config(data["system_fan_curve"])
        if not FAN_CONFIG.exists():
            _write_fan_curve(data["system_fan_curve"])
        return data

    def _write_canonical_fan_config(self, curve):
        descending = sorted(curve, key=lambda point: point[0], reverse=True)
        if not any(point[0] == 0 for point in descending):
            descending.append([0, 0])
        speeds = " ".join(str(point[1]) for point in descending)
        temps = " ".join(str(point[0]) for point in descending)
        _atomic_text(self.canonical_fan_config,
                     f"SPEEDS=({speeds})\nTEMPS=({temps})\n")

    def _runtime_state(self):
        try:
            state = json.loads(self.runtime_state_path.read_text())
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def _capture_runtime_state(self, capabilities):
        cpu = []
        for policy in capabilities["cpu"]:
            path = CPU_ROOT / f"policy{policy['id']}"
            cpu.append({
                "id": policy["id"],
                "path": str(path),
                "baseline": {
                    "governor": _read(path / "scaling_governor"),
                    "minimum": _read_int(path / "scaling_min_freq"),
                    "maximum": _read_int(path / "scaling_max_freq"),
                },
                "applied": {},
            })
        gpu = None
        if capabilities["gpu"]["available"]:
            path = _gpu_path()
            gpu = {
                "path": str(path),
                "baseline": {
                    "governor": _read(path / "governor"),
                    "minimum": _read_int(path / "min_freq"),
                    "maximum": _read_int(path / "max_freq"),
                },
                "applied": {},
            }
        scheduler = None
        if "lavd" in capabilities["schedulers"]:
            scheduler = {
                "unit": "scx_lavd.service",
                "baseline": _service_active("scx_lavd.service"),
                "applied": None,
            }
        charging = None
        if CHARGE_BEHAVIOUR.exists():
            charging = {
                "path": str(CHARGE_BEHAVIOUR),
                "baseline": _active_charge_behaviour(),
                "applied": None,
            }
        return {
            "version": 1,
            "owner_pid": os.getpid(),
            "boot_id": _read("/proc/sys/kernel/random/boot_id"),
            "created": int(time.time()),
            "controls": {
                "cpu": cpu,
                "gpu": gpu,
                "scheduler": scheduler,
                "charging": charging,
                "fan": {"applied": False},
            },
        }

    def _install_runtime_restore_tools(self):
        plugin_dir = Path(__file__).resolve().parent
        source_guard = plugin_dir / "runtime-restore-guard.sh"
        source_restore = plugin_dir / "runtime-restore.py"
        if not source_guard.exists() or not source_restore.exists():
            raise RuntimeError("runtime restoration tools are missing")
        shutil.copy2(source_guard, self.runtime_guard_path)
        shutil.copy2(source_restore, self.runtime_restore_path)
        self.runtime_guard_path.chmod(0o755)
        self.runtime_restore_path.chmod(0o755)

    def _ensure_runtime_session_locked(self, capabilities):
        if self.runtime_marker.exists():
            return self._runtime_state()
        self._install_runtime_restore_tools()
        state = self._capture_runtime_state(capabilities)
        _atomic_text(self.runtime_state_path,
                     json.dumps(state, indent=2, sort_keys=True) + "\n")
        _atomic_text(self.runtime_marker, f"{os.getpid()}\n")
        try:
            unit = f"rke-runtime-restore-guard-{os.getpid()}-{time.monotonic_ns()}"
            _run(["systemd-run", f"--unit={unit}", "--collect",
                  str(self.runtime_guard_path), str(self.runtime_marker),
                  str(self.runtime_state_path), str(self.runtime_restore_path),
                  str(self.canonical_fan_config), str(FAN_CONFIG)])
        except Exception:
            self.runtime_marker.unlink(missing_ok=True)
            self.runtime_state_path.unlink(missing_ok=True)
            raise
        decky.logger.info("Captured native runtime baseline and started restoration guard")
        return state

    def _record_profile_intent_locked(self, state, clean, capabilities,
                                      fan_applied):
        controls = state["controls"]
        policies = {item["id"]: item for item in controls["cpu"]}
        for policy in capabilities["cpu"]:
            pid = policy["id"]
            policies[pid]["applied"] = {
                "governor": clean["cpu_governor"],
                "minimum": clean["cpu_min"][pid],
                "maximum": clean["cpu_max"][pid],
            }
        gpu = controls.get("gpu")
        if gpu is not None and capabilities["gpu"]["available"]:
            gpu["applied"] = {
                "governor": clean["gpu_governor"],
                "minimum": clean["gpu_min"],
                "maximum": clean["gpu_max"],
            }
        scheduler = controls.get("scheduler")
        if scheduler is not None:
            scheduler["applied"] = clean["cpu_scheduler"] == "lavd"
        if fan_applied:
            controls["fan"]["applied"] = True
        _atomic_text(self.runtime_state_path,
                     json.dumps(state, indent=2, sort_keys=True) + "\n")

    def _fan_session_active(self):
        if not self.runtime_marker.exists():
            return False
        return bool(self._runtime_state().get("controls", {}).get(
            "fan", {}).get("applied"))

    def _restore_runtime_session(self):
        if not self.runtime_marker.exists():
            return False
        self._install_runtime_restore_tools()
        output = _run([
            str(self.runtime_restore_path), str(self.runtime_marker),
            str(self.runtime_state_path), str(self.canonical_fan_config),
            str(FAN_CONFIG),
        ], timeout=30)
        if self.runtime_marker.exists():
            raise RuntimeError(output or "runtime restoration did not complete")
        decky.logger.info(output or "Restored native runtime baseline")
        return True

    def _restore_legacy_system_fan_curve(self):
        if not self.legacy_fan_guard_marker.exists():
            return False
        data = self._load()
        _write_fan_curve(data["system_fan_curve"])
        self.legacy_fan_guard_marker.unlink(missing_ok=True)
        if _get_setting("cooling.profile", "") == "custom":
            _restart_fancontrol()
        decky.logger.info("Restored protected ROCKNIX Custom fan curve from a legacy session")
        return True

    def _save(self, data):
        self.settings_dir.mkdir(parents=True, exist_ok=True)
        _atomic_text(self.settings_path, json.dumps(data, indent=2, sort_keys=True) + "\n")

    def _state(self):
        data = self._load()
        effective = _get_setting("cooling.profile", "")
        return {"capabilities": _capabilities(), **data,
                "active_preset": self.active_preset, "active_appid": self.active_appid,
                "effective_cooling_profile": effective,
                "fan_curve_active": effective == "custom" and self._fan_session_active()}

    def _apply(self, profile, capabilities=None):
        capabilities = capabilities or _capabilities()
        clean = _validate_profile(profile, capabilities)
        effective_cooling = _get_setting("cooling.profile", "")
        fan_applied = effective_cooling == "custom"
        with self.session_lock, _exclusive_file_lock(self.runtime_lock_path):
            state = self._ensure_runtime_session_locked(capabilities)
            self._record_profile_intent_locked(
                state, clean, capabilities, fan_applied)
            for policy in capabilities["cpu"]:
                path, pid = CPU_ROOT / f"policy{policy['id']}", policy["id"]
                (path / "scaling_governor").write_text(clean["cpu_governor"])
                _write_range(path, clean["cpu_min"][pid], clean["cpu_max"][pid])
            gpu = capabilities["gpu"]
            if gpu["available"]:
                path = _gpu_path()
                (path / "governor").write_text(clean["gpu_governor"])
                _write_gpu_range(path, clean["gpu_min"], clean["gpu_max"])
            if fan_applied:
                _write_fan_curve(clean["fan_curve"])
                fancontrol_pid = _restart_fancontrol()
                decky.logger.info(
                    f"Applied RK-E fan curve through native fancontrol: pid={fancontrol_pid}")
            else:
                decky.logger.info(
                    f"RK-E fan curve inactive: effective ROCKNIX cooling profile is {effective_cooling or 'unknown'}")
            if "lavd" in capabilities["schedulers"]:
                action = "start" if clean["cpu_scheduler"] == "lavd" else "stop"
                _run(["systemctl", action, "scx_lavd.service"])
        return True

    async def get_state(self):
        return await asyncio.to_thread(self._state)

    async def apply_profile(self, profile):
        if self.lock is None:
            self.lock = asyncio.Lock()
        async with self.lock:
            return await asyncio.to_thread(self._apply, profile)

    async def save_preset(self, name, profile):
        def work():
            clean_name = _validate_name(name)
            capabilities = _capabilities()
            clean = _validate_profile(profile, capabilities)
            self._apply(clean, capabilities)
            data = self._load()
            data["presets"][clean_name] = clean
            self._save(data)
            decky.logger.info(f"Saved preset: {clean_name}")
            self.active_preset = clean_name
            return self._state()
        return await asyncio.to_thread(work)

    async def restore_steam_default(self):
        def work():
            data = self._load()
            capabilities = _capabilities()
            restored = _validate_profile(data["steam_default_original"], capabilities)
            data["presets"][DEFAULT_PRESET] = restored
            self._save(data)
            if self.active_preset == DEFAULT_PRESET:
                self._apply(restored, capabilities)
            decky.logger.info("Restored RK-E Default from its original setup snapshot")
            return self._state()
        return await asyncio.to_thread(work)

    async def rename_preset(self, old_name, new_name):
        def work():
            old, new = str(old_name), _validate_name(new_name)
            if old == DEFAULT_PRESET:
                raise ValueError(f"{DEFAULT_PRESET} cannot be renamed")
            data = self._load()
            if old not in data["presets"] or new in data["presets"]:
                raise ValueError("preset does not exist or name is already used")
            data["presets"][new] = data["presets"].pop(old)
            data["game_profiles"] = {appid: new if preset == old else preset
                                      for appid, preset in data["game_profiles"].items()}
            if data["steam_default"] == old:
                data["steam_default"] = new
            if self.active_preset == old:
                self.active_preset = new
            self._save(data)
            return self._state()
        return await asyncio.to_thread(work)

    async def delete_preset(self, name):
        def work():
            target = str(name)
            if target == DEFAULT_PRESET:
                raise ValueError(f"{DEFAULT_PRESET} cannot be deleted")
            data = self._load()
            if target not in data["presets"]:
                raise ValueError("preset does not exist")
            del data["presets"][target]
            data["game_profiles"] = {appid: preset for appid, preset in data["game_profiles"].items()
                                      if preset != target}
            if data["steam_default"] == target:
                data["steam_default"] = DEFAULT_PRESET
            if self.active_preset == target:
                self.active_preset = DEFAULT_PRESET
                self._apply(data["presets"][DEFAULT_PRESET])
            self._save(data)
            return self._state()
        return await asyncio.to_thread(work)

    async def assign_game(self, appid, preset):
        def work():
            game, target = str(appid), str(preset)
            if not game.isdigit() or game == "0":
                raise ValueError("a running Steam game is required")
            data = self._load()
            if target not in data["presets"]:
                raise ValueError("preset does not exist")
            data["game_profiles"][game] = target
            self._save(data)
            if game == self.active_appid:
                self._apply(data["presets"][target])
                self.active_preset = target
            return self._state()
        return await asyncio.to_thread(work)

    async def set_steam_default(self, preset):
        def work():
            target = str(preset)
            data = self._load()
            if target not in data["presets"]:
                raise ValueError("preset does not exist")
            data["steam_default"] = target
            self._save(data)
            if not self.active_appid or self.active_appid not in data["game_profiles"]:
                self._apply(data["presets"][target])
                self.active_preset = target
            return self._state()
        return await asyncio.to_thread(work)

    async def save_system_fan_curve(self, curve):
        def work():
            clean = _normalize_fan_curve(curve)
            if not isinstance(curve, list) or len(clean) != len(curve):
                raise ValueError("system fan curve must contain valid unique points")
            previous_temp, previous_pwm = -1, -1
            for temp, pwm in clean:
                if not 10000 <= temp <= 120000 or not 0 <= pwm <= 255:
                    raise ValueError("fan temperatures must be 10-120°C and PWM must be 0-255")
                if temp <= previous_temp or pwm < previous_pwm:
                    raise ValueError("fan temperature and PWM points must rise")
                previous_temp, previous_pwm = temp, pwm
            data = self._load()
            data["system_fan_curve"] = clean
            self._save(data)
            self._write_canonical_fan_config(clean)
            if not self._fan_session_active():
                _write_fan_curve(clean)
                if _get_setting("cooling.profile", "") == "custom":
                    _restart_fancontrol()
            decky.logger.info(f"Saved ROCKNIX Custom system fan curve: {clean}")
            return self._state()
        return await asyncio.to_thread(work)

    async def unassign_game(self, appid):
        def work():
            data = self._load()
            game = str(appid)
            data["game_profiles"].pop(game, None)
            self._save(data)
            if game == self.active_appid:
                fallback = data["steam_default"]
                self._apply(data["presets"][fallback])
                self.active_preset = fallback
            return self._state()
        return await asyncio.to_thread(work)

    async def activate_game(self, appid):
        def work():
            target = str(appid or "")
            data = self._load()
            preset = data["game_profiles"].get(target, data["steam_default"])
            needs_fan_apply = (
                _get_setting("cooling.profile", "") == "custom" and
                not self._fan_session_active()
            )
            if (target == self.active_appid and preset == self.active_preset and
                    not needs_fan_apply):
                return {"applied": False, "preset": self.active_preset}
            self.active_appid = target
            self.gpu_fdinfo_paths = []
            self.gpu_fdinfo_refresh = 0.0
            self.last_gpu_sample = None
            self._apply(data["presets"][preset])
            self.active_preset = preset
            return {"applied": True, "preset": preset}
        return await asyncio.to_thread(work)

    def _steam_scope_active(self):
        cgroups = (
            Path("/sys/fs/cgroup/systemd/system.slice/steam-bigpicture.scope/cgroup.procs"),
            Path("/sys/fs/cgroup/unified/system.slice/steam-bigpicture.scope/cgroup.procs"),
            Path("/sys/fs/cgroup/pids/system.slice/steam-bigpicture.scope/cgroup.procs"),
        )
        return any(_read(cgroup) for cgroup in cgroups if cgroup.exists())

    def _detect_steam_app(self):
        cgroups = (
            Path("/sys/fs/cgroup/systemd/system.slice/steam-bigpicture.scope/cgroup.procs"),
            Path("/sys/fs/cgroup/unified/system.slice/steam-bigpicture.scope/cgroup.procs"),
            Path("/sys/fs/cgroup/pids/system.slice/steam-bigpicture.scope/cgroup.procs"),
        )
        pids = []
        for cgroup in cgroups:
            if cgroup.exists():
                pids = _read(cgroup).splitlines()
                break
        ignored = (
            "pw-audio-namesp", "network.cr", "steamwebhelper", "pressure-vessel",
            "reaper", "wineserver", "services.exe", "explorer.exe", "rpcss.exe",
            "plugplay.exe", "svchost.exe", "conhost.exe",
        )
        candidates = {}
        for value in pids:
            try:
                pid = int(value)
                process = Path("/proc") / value
                comm = _read(process / "comm").lower()
                if not comm or any(comm.startswith(name) for name in ignored):
                    continue
                environment = (process / "environ").read_bytes().split(b"\0")
            except (OSError, ValueError):
                continue
            appid = ""
            for variable in environment:
                if variable.startswith((b"SteamAppId=", b"SteamGameId=")):
                    candidate = variable.split(b"=", 1)[1].decode(errors="ignore")
                    if candidate.isdigit() and candidate != "0":
                        appid = candidate
                        break
            if appid:
                candidates[appid] = max(pid, candidates.get(appid, 0))
        if not candidates:
            return ""
        # The newest qualifying game process wins if Steam is briefly
        # transitioning between two applications.
        return max(candidates, key=candidates.get)

    async def _game_watch_loop(self):
        pending, confirmations = None, 0
        steam_was_active = False
        while True:
            try:
                steam_active = await asyncio.to_thread(self._steam_scope_active)
                if not steam_active:
                    steam_was_active = False
                    pending, confirmations = None, 0
                    self.active_appid = ""
                    self.active_preset = DEFAULT_PRESET
                    await asyncio.sleep(2)
                    continue
                detected = await asyncio.to_thread(self._detect_steam_app)
                if not steam_was_active:
                    steam_was_active = True
                    pending, confirmations = detected, 2
                elif detected == self.active_appid:
                    pending, confirmations = None, 0
                    await asyncio.sleep(2)
                    continue
                elif detected == pending:
                    confirmations += 1
                else:
                    pending, confirmations = detected, 1
                if confirmations >= 2:
                    result = await self.activate_game(detected)
                    decky.logger.info(
                        f"Backend game watcher: appid={detected or 'Steam'} "
                        f"preset={result['preset']} applied={result['applied']}")
                    pending, confirmations = None, 0
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception as reason:
                decky.logger.error(f"Backend game watcher failed: {reason}")
                await asyncio.sleep(5)

    def _find_gamescope_pid(self):
        if self.gamescope_pid:
            comm = _read(Path("/proc") / str(self.gamescope_pid) / "comm")
            if comm.startswith("gamescope"):
                return self.gamescope_pid
        for process in Path("/proc").glob("[0-9]*"):
            if _read(process / "comm").startswith("gamescope"):
                self.gamescope_pid = int(process.name)
                return self.gamescope_pid
        self.gamescope_pid = None
        return None

    def _refresh_gpu_fdinfo_paths(self):
        processes = []
        if self.active_appid:
            appid = self.active_appid.encode()
            for process in Path("/proc").glob("[0-9]*"):
                try:
                    environment = (process / "environ").read_bytes()
                except OSError:
                    continue
                variables = environment.split(b"\0")
                if (b"SteamAppId=" + appid in variables or
                        b"SteamGameId=" + appid in variables):
                    processes.append(process)
        if not processes:
            pid = self._find_gamescope_pid()
            if pid is not None:
                processes.append(Path("/proc") / str(pid))
        paths = []
        client_ids = set()
        for process in processes:
            for descriptor in (process / "fd").glob("*"):
                try:
                    target = os.readlink(descriptor)
                except OSError:
                    continue
                if not target.startswith("/dev/dri/"):
                    continue
                info = process / "fdinfo" / descriptor.name
                client_id = ""
                try:
                    for line in info.read_text().splitlines():
                        if line.startswith("drm-client-id:"):
                            client_id = line.split(":", 1)[1].strip()
                            break
                except OSError:
                    continue
                if not client_id or client_id in client_ids:
                    continue
                client_ids.add(client_id)
                paths.append(info)
        self.gpu_fdinfo_paths = paths
        self.gpu_fdinfo_refresh = time.monotonic()

    def _gpu_engine_time(self):
        now = time.monotonic()
        if now - self.gpu_fdinfo_refresh >= 10 or not self.gpu_fdinfo_paths:
            self._refresh_gpu_fdinfo_paths()
        total = 0
        for info in self.gpu_fdinfo_paths:
            try:
                for line in info.read_text().splitlines():
                    if line.startswith("drm-engine-gpu:"):
                        total += int(line.split()[1])
                        break
            except (OSError, ValueError, IndexError):
                continue
        return total

    def _telemetry(self):
        cpu = _cpu_capabilities()
        governors = [_read(CPU_ROOT / f"policy{item['id']}" / "scaling_governor") for item in cpu]
        counters = []
        try:
            counters = [int(value) for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
        except (OSError, ValueError, IndexError):
            pass
        cpu_percent = 0.0
        if counters and self.last_cpu_sample:
            deltas = [max(0, now - old) for now, old in zip(counters, self.last_cpu_sample)]
            total, idle = sum(deltas), sum(deltas[index] for index in (3, 4) if index < len(deltas))
            cpu_percent = 100 * (total - idle) / total if total else 0
        if counters:
            self.last_cpu_sample = counters
        # Match MangoHud's msm_drm/KGSL backend on Qualcomm devices: this is
        # total GPU load, rather than the load of gamescope's DRM client.
        kgsl_load = _read_int(KGSL_GPU_ROOT / "gpu_busy_percentage", -1)
        if kgsl_load >= 0:
            gpu_percent = min(100.0, float(kgsl_load))
            self.last_gpu_sample = None
        else:
            gpu_engine_ns = self._gpu_engine_time()
            now_ns = time.monotonic_ns()
            gpu_percent = 0.0
            if self.last_gpu_sample:
                previous_engine, previous_time = self.last_gpu_sample
                elapsed = now_ns - previous_time
                busy = gpu_engine_ns - previous_engine
                if elapsed > 0 and busy >= 0:
                    gpu_percent = min(100.0, busy * 100 / elapsed)
            self.last_gpu_sample = (gpu_engine_ns, now_ns)
        cpu_package_temps, cpu_core_temps, gpu_temps = [], [], []
        primary_gpu_temps = []
        # /sys/devices/virtual/thermal and /sys/class/thermal expose the same
        # zones. Scan the class path once so each sensor has equal weight.
        for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
            kind = _read(zone / "type").lower().replace("_", "-")
            value = _read_int(zone / "temp")
            if not 0 < value < 150000:
                continue
            if kind.startswith("cpuss"):
                cpu_package_temps.append(value)
            elif kind.startswith("cpu"):
                cpu_core_temps.append(value)
            elif kind.startswith(("gpu", "gpuss")):
                gpu_temps.append(value)
                if kind.startswith("gpuss0-") or kind in ("gpuss0", "gpu-thermal"):
                    primary_gpu_temps.append(value)
        mem_total = mem_available = 0
        for line in _read("/proc/meminfo").splitlines():
            if line.startswith("MemTotal:"):
                mem_total = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                mem_available = int(line.split()[1])
        gpu, gpu_path = _gpu_capability(), _gpu_path()
        pwm_path = _fan_pwm_path()
        fan_pwm = _read_int(pwm_path) if pwm_path else 0
        battery = Path("/sys/class/power_supply/battery")
        battery_status = _read(battery / "status")
        charge_behaviour = _read(battery / "charge_behaviour", "auto")
        bypass_charging = (charge_behaviour == "inhibit-charge" or
                           "[inhibit-charge]" in charge_behaviour)
        voltage_uv = _read_int(battery / "voltage_now")
        current_ua = _read_int(battery / "current_now")
        battery_flow_watts = voltage_uv * current_ua / 1_000_000_000_000
        battery_watts = abs(battery_flow_watts)
        battery_filling = (battery_status.lower() == "charging" or
                           (bypass_charging and battery_flow_watts >= 0.2))
        battery_seconds = (_read_int(battery / "time_to_full_avg") if battery_filling
                           else _read_int(battery / "time_to_empty_avg"))
        battery_estimate_ready = battery_seconds > 0
        if bypass_charging and battery_flow_watts <= -0.2:
            discharge_ua = abs(current_ua)
            now = time.monotonic()
            sample_due = (self.battery_discharge_samples < 5 or
                          now - self.battery_discharge_last_sample >= 5)
            if sample_due:
                self.battery_discharge_ema = (discharge_ua if self.battery_discharge_ema is None
                                              else self.battery_discharge_ema * 0.8 + discharge_ua * 0.2)
                self.battery_discharge_samples += 1
                self.battery_discharge_last_sample = now
            charge_uah = _read_int(battery / "charge_counter")
            battery_estimate_ready = self.battery_discharge_samples >= 5 and charge_uah > 0
            battery_seconds = (int(charge_uah * 3600 / self.battery_discharge_ema)
                               if battery_estimate_ready else 0)
        elif not bypass_charging or battery_flow_watts >= 0.2:
            self.battery_discharge_ema = None
            self.battery_discharge_samples = 0
            self.battery_discharge_last_sample = 0.0
        try:
            load = [float(value) for value in _read("/proc/loadavg").split()[:3]]
        except ValueError:
            load = []
        throttled_cpu = throttled_gpu = False
        for cooling in Path("/sys/class/thermal").glob("cooling_device*"):
            kind = _read(cooling / "type").lower()
            active = _read_int(cooling / "cur_state") > 0
            if active and kind.startswith("cpufreq-"):
                throttled_cpu = True
            elif active and (kind.startswith("devfreq-") or "gpu" in kind):
                throttled_gpu = True
        thermal_limit = ("CPU + GPU" if throttled_cpu and throttled_gpu else
                         "CPU" if throttled_cpu else "GPU" if throttled_gpu else "Clear")
        return {
            "battery_percent": _read_int(battery / "capacity"),
            "battery_status": battery_status,
            "bypass_charging": bypass_charging,
            "battery_seconds": max(0, battery_seconds),
            "battery_estimate_ready": battery_estimate_ready,
            "battery_watts": round(battery_watts, 1),
            "battery_flow_watts": round(battery_flow_watts, 2),
            "cpu_temperature": round(
                sum(cpu_package_temps) / len(cpu_package_temps) / 1000, 1
            ) if cpu_package_temps else (
                round(sum(cpu_core_temps) / len(cpu_core_temps) / 1000, 1)
                if cpu_core_temps else 0
            ),
            "cpu_hotspot_temperature": round(
                max(cpu_package_temps + cpu_core_temps) / 1000, 1
            ) if cpu_package_temps or cpu_core_temps else 0,
            # gpuss0 is the primary Adreno temperature used by MangoApp on
            # both observed Qualcomm layouts. Fall back to the hottest GPU
            # zone on devices which expose a different naming scheme.
            "gpu_temperature": round(
                max(primary_gpu_temps or gpu_temps) / 1000, 1
            ) if primary_gpu_temps or gpu_temps else 0,
            "cpu_percent": round(cpu_percent, 1),
            "gpu_percent": round(gpu_percent, 1),
            "cpu_clocks": [{"id": item["id"], "cpus": item["cpus"], "frequency": item["current"],
                            "minimum": item["minimum"], "maximum": item["effective_maximum"]} for item in cpu],
            "cpu_governor": governors[0] if len(set(governors)) == 1 else "Mixed",
            "gpu_frequency": gpu["current"], "gpu_frequency_max": max(gpu["frequencies"], default=0),
            "gpu_governor": _read(gpu_path / "governor") if gpu_path else "",
            "fan_pwm": fan_pwm, "fan_percent": round(fan_pwm * 100 / 255),
            "cooling_profile": _get_setting("cooling.profile", ""),
            "scheduler": "lavd" if _read("/sys/kernel/sched_ext/state") == "enabled" else "kernel",
            "memory_percent": round((mem_total - mem_available) * 100 / mem_total, 1) if mem_total else 0,
            "memory_used_mb": round((mem_total - mem_available) / 1024) if mem_total else 0,
            "memory_total_mb": round(mem_total / 1024) if mem_total else 0,
            "load_average": load,
            "thermal_limit": thermal_limit,
        }

    async def get_telemetry(self):
        return await asyncio.to_thread(self._telemetry)

    async def set_bypass_charging(self, enabled):
        def work():
            if bool(enabled) and not self._load().get("experimental_unlocked", False):
                raise RuntimeError("unlock experimental controls in Utils first")
            behaviour = CHARGE_BEHAVIOUR
            if not behaviour.exists():
                raise RuntimeError("bypass charging is unavailable on this device")
            requested = "inhibit-charge" if bool(enabled) else "auto"
            capabilities = _capabilities()
            with self.session_lock, _exclusive_file_lock(self.runtime_lock_path):
                state = self._ensure_runtime_session_locked(capabilities)
                charging = state["controls"].get("charging")
                if charging is None:
                    raise RuntimeError("bypass charging is unavailable in this runtime session")
                charging["applied"] = requested
                _atomic_text(self.runtime_state_path,
                             json.dumps(state, indent=2, sort_keys=True) + "\n")
                behaviour.write_text(requested)
            self.battery_discharge_ema = None
            self.battery_discharge_samples = 0
            self.battery_discharge_last_sample = 0.0
            active = _read(behaviour)
            accepted = (active == requested or f"[{requested}]" in active)
            if not accepted:
                raise RuntimeError("ROCKNIX did not accept the bypass charging setting")
            decky.logger.info(f"Bypass charging {'enabled' if enabled else 'disabled'}")
            return True
        return await asyncio.to_thread(work)

    async def unlock_experimental(self, code):
        def work():
            if str(code) != "bypasstest":
                raise ValueError("incorrect experimental unlock code")
            data = self._load()
            data["experimental_unlocked"] = True
            self._save(data)
            decky.logger.info("Experimental controls unlocked")
            return self._state()
        return await asyncio.to_thread(work)

    async def lock_experimental(self):
        def work():
            data = self._load()
            data["experimental_unlocked"] = False
            self._save(data)
            decky.logger.info("Experimental controls hidden")
            return self._state()
        return await asyncio.to_thread(work)

    def _log_text(self):
        configured = os.environ.get("DECKY_PLUGIN_LOG_DIR")
        if configured:
            log_dir = Path(configured)
        else:
            log_dir = self.settings_dir.parent.parent / "logs" / "RK-Enhanced"
        try:
            logs = sorted(log_dir.glob("*.log"), key=lambda path: path.stat().st_mtime)
        except OSError:
            logs = []
        if not logs:
            return "No RK-Enhanced log file found."
        try:
            lines = logs[-1].read_text(errors="replace").splitlines()
        except OSError as error:
            return f"Could not read log: {error}"
        offset = self.log_offsets.get(str(logs[-1]), 0)
        return "\n".join(lines[offset:][-200:])

    async def get_log(self):
        return await asyncio.to_thread(self._log_text)

    async def clear_log(self):
        def work():
            configured = os.environ.get("DECKY_PLUGIN_LOG_DIR")
            log_dir = Path(configured) if configured else self.settings_dir.parent.parent / "logs" / "RK-Enhanced"
            try:
                logs = list(log_dir.glob("*.log"))
            except OSError:
                logs = []
            for path in logs:
                try:
                    self.log_offsets[str(path)] = len(path.read_text(errors="replace").splitlines())
                except OSError:
                    pass
            return True
        return await asyncio.to_thread(work)

    async def get_update_info(self):
        def work():
            installed = _read(Path(__file__).resolve().parent / "VERSION")
            if not installed:
                installed = _read(self.settings_dir / INSTALLED_VERSION_FILE)
            if not installed:
                status = _read(self.settings_dir / UPDATE_STATUS_FILE)
                marker = "Installed "
                if marker in status:
                    installed = status.split(marker, 1)[1].split(";", 1)[0].split(",", 1)[0].strip()
            installed = installed or "Unknown"

            cached_at, releases = self.latest_release_cache
            error = ""
            try:
                if not releases or time.monotonic() - cached_at >= 300:
                    payload = json.loads(_run([
                        "curl", "-fsSL",
                        f"https://api.github.com/repos/{UPDATE_REPOSITORY}/releases?per_page=10",
                    ]))
                    releases = [
                        str(release.get("tag_name", ""))
                        for release in payload
                        if not release.get("draft") and any(
                            asset.get("name") == "RK-Enhanced.zip"
                            for asset in release.get("assets", [])
                        ) and release.get("tag_name")
                    ]
                    if not releases:
                        raise RuntimeError("no published RK-Enhanced release was found")
                    self.latest_release_cache = (time.monotonic(), releases)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as reason:
                error = str(reason)
            latest = releases[0] if releases else ""
            previous = ""
            if installed in releases:
                position = releases.index(installed)
                if position + 1 < len(releases):
                    previous = releases[position + 1]
            return {"installed": installed, "latest": latest,
                    "update_available": bool(latest and installed != latest),
                    "previous": previous, "error": error}
        return await asyncio.to_thread(work)

    async def install_release(self, version):
        def work():
            requested = str(version).strip()
            if (not requested or len(requested) > 64 or
                    any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                        for character in requested)):
                raise ValueError("invalid release version")
            source = Path(__file__).resolve().parent / "updater.sh"
            if not source.exists():
                raise RuntimeError("updater.sh is missing from this RK-Enhanced installation")
            target = Path("/storage/homebrew/rk-enhanced-updater.sh")
            shutil.copy2(source, target)
            target.chmod(0o755)
            _atomic_text(self.settings_dir / UPDATE_STATUS_FILE,
                         f"Starting installation of {requested}…\n")
            _run(["systemctl", "reset-failed", "rk-enhanced-update.service"], check=False)
            _run(["systemd-run", "--unit=rk-enhanced-update", "--collect",
                  str(target), requested])
            decky.logger.info(f"Detached release installation started: {requested}")
            return True
        return await asyncio.to_thread(work)

    async def reinstall_latest_release(self):
        info = await self.get_update_info()
        if not info["latest"]:
            raise RuntimeError(info["error"] or "latest release is unavailable")
        return await self.install_release(info["latest"])

    async def _main(self):
        decky.logger.info("RK-Enhanced loaded; native ROCKNIX fancontrol remains in ownership")
        def initialise():
            self._load()
            if self.runtime_marker.exists():
                self._restore_runtime_session()
            if self.legacy_fan_guard_marker.exists():
                self._restore_legacy_system_fan_curve()
        await asyncio.to_thread(initialise)
        self.game_watch_task = asyncio.create_task(self._game_watch_loop())

    async def _unload(self):
        if self.game_watch_task is not None:
            self.game_watch_task.cancel()
            try:
                await self.game_watch_task
            except asyncio.CancelledError:
                pass
            self.game_watch_task = None
        restored = await asyncio.to_thread(self._restore_runtime_session)
        legacy_restored = await asyncio.to_thread(self._restore_legacy_system_fan_curve)
        decky.logger.info(
            "RK-Enhanced unloaded; native runtime baseline restored"
            if restored or legacy_restored
            else "RK-Enhanced unloaded; no runtime session required restoration")
