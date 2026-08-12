"""Root Decky backend for RK-Enhanced on ROCKNIX."""

import asyncio
import json
import os
import signal
import shutil
import subprocess
import time
from pathlib import Path
from tempfile import NamedTemporaryFile

import decky

DEFAULT_PRESET = "Steam Default"
LEGACY_DEFAULT_PRESETS = ("Rocknix Custom", "ROCKNIX Default")
SETTINGS_FILE = "settings.json"
FAN_CONFIG = Path("/storage/.config/fancontrol.conf")
CPU_ROOT = Path("/sys/devices/system/cpu/cpufreq")
GPU_ROOT = Path("/sys/class/devfreq")
COOLING_PROFILES = ["quiet", "moderate", "aggressive", "custom"]


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


def _run(command, check=True):
    process = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, timeout=15, check=False)
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


def _cpu_capabilities():
    result = []
    policies = list(CPU_ROOT.glob("policy*"))
    policies.sort(key=lambda item: int(item.name[6:]))
    for policy in policies:
        frequencies = _read_ints(policy / "scaling_available_frequencies")
        if not frequencies:
            frequencies = sorted({value for value in (
                _read_int(policy / "cpuinfo_min_freq"),
                _read_int(policy / "cpuinfo_max_freq"),
            ) if value})
        if not frequencies:
            continue
        cpus = _read(policy / "affected_cpus", policy.name[6:]).split()
        result.append({
            "id": policy.name[6:], "cpus": cpus, "frequencies": frequencies,
            "governors": sorted(set(_read(policy / "scaling_available_governors").split())),
            "current": _read_int(policy / "scaling_cur_freq"),
            "minimum": _read_int(policy / "scaling_min_freq"),
            "maximum": _read_int(policy / "scaling_max_freq"),
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


def _capabilities():
    cpu = _cpu_capabilities()
    common = sorted(set.intersection(*(set(item["governors"]) for item in cpu))) if cpu else []
    return {
        "cpu": cpu, "cpu_governors": common, "gpu": _gpu_capability(),
        "cooling_profiles": COOLING_PROFILES,
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
        "cooling_profile": _get_setting("cooling.profile", "moderate"),
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
        low = int(clean.get("cpu_min", {}).get(pid, -1))
        high = int(clean.get("cpu_max", {}).get(pid, -1))
        if low not in frequencies or high not in frequencies or low > high:
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
    cooling = str(clean.get("cooling_profile", ""))
    if cooling not in capabilities["cooling_profiles"]:
        raise ValueError("unsupported cooling profile")
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
        self.active_preset = DEFAULT_PRESET
        self.active_appid = ""
        self.last_cpu_sample = None
        self.last_gpu_sample = None
        self.gamescope_pid = None
        self.gpu_fdinfo_paths = []
        self.gpu_fdinfo_refresh = 0.0
        self.log_offsets = {}
        self.lock = None

    def _load(self):
        try:
            data = json.loads(self.settings_path.read_text())
            if not isinstance(data, dict):
                raise ValueError
        except (OSError, ValueError, json.JSONDecodeError):
            data = {"presets": {}, "game_profiles": {}}
        data.setdefault("presets", {})
        data.setdefault("game_profiles", {})
        changed = False
        legacy = next((name for name in LEGACY_DEFAULT_PRESETS if name in data["presets"]), None)
        if "system_fan_curve" not in data:
            source = data["presets"].get(legacy, {}) if legacy else {}
            data["system_fan_curve"] = _normalize_fan_curve(source.get("fan_curve", _fan_curve()))
            changed = True
        if legacy and DEFAULT_PRESET not in data["presets"]:
            data["presets"][DEFAULT_PRESET] = data["presets"][legacy]
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
        if not isinstance(data.get("steam_default_original"), dict):
            data["steam_default_original"] = json.loads(json.dumps(data["presets"][DEFAULT_PRESET]))
            changed = True
        if data.get("steam_default") not in data["presets"]:
            data["steam_default"] = DEFAULT_PRESET
            changed = True
        for preset in data["presets"].values():
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
        return data

    def _save(self, data):
        self.settings_dir.mkdir(parents=True, exist_ok=True)
        _atomic_text(self.settings_path, json.dumps(data, indent=2, sort_keys=True) + "\n")

    def _state(self):
        data = self._load()
        return {"capabilities": _capabilities(), **data,
                "active_preset": self.active_preset, "active_appid": self.active_appid}

    def _apply(self, profile, capabilities=None):
        capabilities = capabilities or _capabilities()
        clean = _validate_profile(profile, capabilities)
        for policy in capabilities["cpu"]:
            path, pid = CPU_ROOT / f"policy{policy['id']}", policy["id"]
            (path / "scaling_governor").write_text(clean["cpu_governor"])
            _write_range(path, clean["cpu_min"][pid], clean["cpu_max"][pid])
        gpu = capabilities["gpu"]
        if gpu["available"]:
            path = _gpu_path()
            (path / "governor").write_text(clean["gpu_governor"])
            _write_gpu_range(path, clean["gpu_min"], clean["gpu_max"])
        if clean["cooling_profile"] == "custom":
            # Native ROCKNIX fancontrol remains the controller. The active
            # preset supplies the temporary Custom curve it should execute.
            _write_fan_curve(clean["fan_curve"])
        _set_setting("cooling.profile", clean["cooling_profile"])
        decky.logger.info(
            f"Restarting native fancontrol: profile={clean['cooling_profile']} "
            f"curve={'/storage/.config/fancontrol.conf' if clean['cooling_profile'] == 'custom' else 'stock'}"
        )
        fancontrol_pid = _restart_fancontrol()
        decky.logger.info(f"Native fancontrol restarted successfully: pid={fancontrol_pid}")
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
            decky.logger.info("Restored Steam Default from the original ROCKNIX snapshot")
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
            _write_fan_curve(clean)
            _set_setting("cooling.profile", "custom")
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
            if target == self.active_appid and preset == self.active_preset:
                return {"applied": False, "preset": self.active_preset}
            self.active_appid = target
            self._apply(data["presets"][preset])
            self.active_preset = preset
            return {"applied": True, "preset": preset}
        return await asyncio.to_thread(work)

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
        pid = self._find_gamescope_pid()
        paths = []
        client_ids = set()
        if pid is not None:
            process = Path("/proc") / str(pid)
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
        for root in (Path("/sys/devices/virtual/thermal"), Path("/sys/class/thermal")):
            for zone in root.glob("thermal_zone*"):
                kind, value = _read(zone / "type").lower(), _read_int(zone / "temp")
                if value and kind.startswith("cpuss"):
                    cpu_package_temps.append(value)
                elif value and kind.startswith("cpu"):
                    cpu_core_temps.append(value)
                elif value and kind.startswith(("gpu", "gpuss")):
                    gpu_temps.append(value)
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
        battery_seconds = (_read_int(battery / "time_to_full_avg")
                           if battery_status.lower() == "charging"
                           else _read_int(battery / "time_to_empty_avg"))
        voltage_uv = _read_int(battery / "voltage_now")
        current_ua = _read_int(battery / "current_now")
        battery_watts = abs(voltage_uv * current_ua) / 1_000_000_000_000
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
            "battery_seconds": max(0, battery_seconds),
            "battery_watts": round(battery_watts, 1),
            "cpu_temperature": round(
                sum(cpu_package_temps) / len(cpu_package_temps) / 1000, 1
            ) if cpu_package_temps else (
                round(sum(cpu_core_temps) / len(cpu_core_temps) / 1000, 1)
                if cpu_core_temps else 0
            ),
            "gpu_temperature": round(max(gpu_temps) / 1000, 1) if gpu_temps else 0,
            "cpu_percent": round(cpu_percent, 1),
            "gpu_percent": round(gpu_percent, 1),
            "cpu_clocks": [{"id": item["id"], "cpus": item["cpus"], "frequency": item["current"],
                            "minimum": item["minimum"], "maximum": item["maximum"]} for item in cpu],
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

    async def _main(self):
        decky.logger.info("RK-Enhanced loaded; native ROCKNIX fancontrol remains in ownership")
        await asyncio.to_thread(self._load)

    async def _unload(self):
        def restore_system_curve():
            data = self._load()
            _write_fan_curve(data["system_fan_curve"])
            _set_setting("cooling.profile", "custom")
            _restart_fancontrol()
        await asyncio.to_thread(restore_system_curve)
        decky.logger.info("RK-Enhanced unloaded; restored the system ROCKNIX Custom curve")
