"""Root Decky backend for RK-Enhanced on ROCKNIX."""

import asyncio
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import math
import os
import re
import secrets
import signal
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path
from tempfile import NamedTemporaryFile

import decky


def _charging_controller_class():
    """Load the sibling module when Decky omits the plugin path from sys.path."""
    module_path = Path(__file__).with_name("charging.py")
    spec = importlib.util.spec_from_file_location("rke_charging", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load charging integration from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ChargingController


def _rgb_controller_class():
    """Load the sibling RGB boundary when Decky omits the plugin path."""
    module_path = Path(__file__).with_name("rgb.py")
    spec = importlib.util.spec_from_file_location("rke_rgb", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load RGB integration from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.RGBController


ChargingController = _charging_controller_class()
RGBController = _rgb_controller_class()

DEFAULT_PRESET = "RK-E Default"
ODIN3_RGB_STARTUP_ACTION = "htr3212-static"
LEGACY_DEFAULT_PRESETS = ("Rocknix Custom", "ROCKNIX Default", "Steam Default")
SETTINGS_FILE = "settings.json"
UPDATE_STATUS_FILE = "update-status.txt"
INSTALL_PROGRESS_FILE = "install-progress.json"
INSTALL_PROGRESS_ACK_FILE = "install-progress-ack.txt"
INSTALLED_VERSION_FILE = "installed-version.txt"
LAST_INSTALLED_VERSION_FILE = "last-installed-version.txt"
INSTALL_HEALTH_REQUEST_FILE = "install-health-request.json"
INSTALL_BACKEND_READY_FILE = "install-backend-ready.json"
INSTALL_FRONTEND_READY_FILE = "install-frontend-ready.json"
INSTALL_HEALTH_PROTOCOL = 1
UPDATE_REPOSITORY = "mrdidit/RK-Enhanced"
FAN_CONFIG = Path("/storage/.config/fancontrol.conf")
CPU_ROOT = Path("/sys/devices/system/cpu/cpufreq")
GPU_ROOT = Path("/sys/class/devfreq")
KGSL_GPU_ROOT = Path("/sys/class/kgsl/kgsl-3d0")
LIFECYCLE_RUN_ROOT = Path("/run/rk-enhanced")
LIFECYCLE_CURRENT = LIFECYCLE_RUN_ROOT / "plugin-lifecycle-current.json"
AUTO_RECOVERY_FOCUS_REQUEST = (
    LIFECYCLE_RUN_ROOT / "automatic-recovery-focus.json")
AUTO_RECOVERY_FOCUS_MAX_AGE_SECONDS = 90.0
LIFECYCLE_LOCK = Path(
    "/run/lock/rk-enhanced-plugin-loader-recovery.lock")
INSTALL_TRANSACTION_LOCK = Path(
    "/run/lock/rk-enhanced-install-transaction.lock")
PLUGIN_LOADER_SERVICE = "plugin_loader.service"
LEGACY_CONTROL_PLUGIN_NAME = "ROCKNIX Control"
INSTALL_PROGRESS_PROTOCOL = 1

INSTALL_PROGRESS_PHASES = {
    "idle", "starting", "preflight", "checking-releases", "downloading",
    "validating", "backing-up", "removing-conflict", "stopping-decky",
    "installing", "starting-decky", "verifying", "rolling-back",
    "completed", "rolled-back", "blocked", "failed", "unavailable",
}
INSTALL_PROGRESS_OUTCOMES = {
    "idle", "running", "succeeded", "failed", "rolled-back", "blocked",
    "unavailable",
}
INSTALL_PROGRESS_STALE_SECONDS = 15 * 60


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


def _process_identity(pid, proc_root=Path("/proc")):
    """Return the PID-reuse-safe identity used by the external lifecycle guard."""
    try:
        pid = int(pid)
        raw = (Path(proc_root) / str(pid) / "stat").read_text()
        close = raw.rfind(")")
        if pid <= 0 or close < 0:
            return None
        fields = raw[close + 2:].split()
        return {
            "pid": pid,
            "start_time_ticks": int(fields[19]),
            "parent_pid": int(fields[1]),
        }
    except (OSError, IndexError, TypeError, ValueError):
        return None


def _process_cpu_seconds(identity, proc_root=Path("/proc")):
    """Read own CPU time for one exact process generation."""
    if not isinstance(identity, dict):
        return None
    try:
        pid = int(identity["pid"])
        expected_start = int(identity["start_time_ticks"])
        raw = (Path(proc_root) / str(pid) / "stat").read_text()
        close = raw.rfind(")")
        if pid <= 0 or close < 0:
            return None
        fields = raw[close + 2:].split()
        if int(fields[19]) != expected_start:
            return None
        # Include completed descendants. While a child is live its counters
        # are included separately by the exact tree snapshot; after wait/reap,
        # Linux transfers those counters into its parent's cutime/cstime. This
        # keeps each root aggregate cumulative across short helper lifetimes.
        ticks = sum(int(fields[index]) for index in (11, 12, 13, 14))
        ticks_per_second = int(os.sysconf("SC_CLK_TCK"))
        if ticks < 0 or ticks_per_second <= 0:
            return None
        return ticks / ticks_per_second
    except (KeyError, OSError, IndexError, TypeError, ValueError):
        return None


def _process_tree_cpu_snapshot(identity, proc_root=Path("/proc")):
    """Return own CPU counters for one exact live process tree.

    Child discovery is restricted to each known process's task children files;
    this never scans the system process table. Exact start times protect every
    counter from PID reuse, while a fixed bound keeps Monitor work predictable.
    """
    if not isinstance(identity, dict):
        return {}
    pending = [identity]
    visited = set()
    snapshot = {}
    while pending and len(visited) < 128:
        current = pending.pop()
        try:
            pid = int(current["pid"])
            start_time = int(current["start_time_ticks"])
        except (KeyError, TypeError, ValueError):
            continue
        key = (pid, start_time)
        if key in visited:
            continue
        visited.add(key)
        cpu_seconds = _process_cpu_seconds(current, proc_root)
        if cpu_seconds is None:
            continue
        snapshot[key] = cpu_seconds

        task_root = Path(proc_root) / str(pid) / "task"
        try:
            tasks = sorted(
                (task for task in task_root.iterdir()
                 if task.name.isdigit()),
                key=lambda task: int(task.name),
            )[:128]
        except OSError:
            tasks = []
        child_pids = set()
        for task in tasks:
            try:
                values = (task / "children").read_text().split()
            except OSError:
                continue
            for value in values:
                try:
                    child_pid = int(value)
                except ValueError:
                    continue
                if child_pid > 0:
                    child_pids.add(child_pid)
        for child_pid in sorted(child_pids, reverse=True):
            child = _process_identity(child_pid, proc_root)
            if child is not None and child.get("parent_pid") == pid:
                pending.append(child)
    return snapshot


def _battery_power(battery):
    """Return whether battery power is measurable and its signed wattage.

    A current of exactly zero is a valid sample. It must remain distinct from
    a missing or malformed power-supply attribute.
    """
    battery = Path(battery)
    try:
        voltage_uv = int((battery / "voltage_now").read_text().strip())
        current_ua = int((battery / "current_now").read_text().strip())
    except (OSError, TypeError, ValueError):
        return False, 0.0, 0
    if voltage_uv <= 0:
        return False, 0.0, current_ua
    return True, voltage_uv * current_ua / 1_000_000_000_000, current_ua


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


def _guard_unit_identity(unit):
    """Resolve only an internally named RK-E guard unit to its live identity."""
    if (not isinstance(unit, str) or
            re.fullmatch(
                r"rke-(?:plugin-lifecycle|runtime-restore)-guard-"
                r"[A-Za-z0-9-]{1,128}", unit) is None):
        return None
    for attempt in range(3):
        try:
            pid = int(_run([
                "systemctl", "show", "--property=MainPID", "--value", unit,
            ], timeout=3) or "0")
        except (OSError, RuntimeError, subprocess.SubprocessError,
                TypeError, ValueError):
            pid = 0
        identity = _process_identity(pid)
        if identity is not None:
            return identity
        if attempt < 2:
            time.sleep(0.05)
    return None


def _parse_device_network_info(output):
    """Select the preferred active IPv4 address from ``ip -o`` output.

    Match ROCKNIX's own information screen by preferring wired ``eth0``, then
    Wi-Fi, while still supporting devices whose interfaces use other names.
    Linux's ``global`` scope includes private LAN addresses, so do not use
    ``IPv4Address.is_global`` (which means publicly routable in Python).
    """
    addresses = []
    for position, line in enumerate(str(output or "").splitlines()):
        fields = line.split()
        if len(fields) < 4 or fields[2] != "inet":
            continue
        interface = fields[1].split("@", 1)[0]
        try:
            address = ipaddress.ip_interface(fields[3]).ip
        except ValueError:
            continue
        if (address.version != 4 or address.is_loopback or
                address.is_link_local or address.is_multicast or
                address.is_unspecified):
            continue
        priority = 0 if interface == "eth0" else 1 if interface.startswith("wlan") else 2
        addresses.append((priority, position, str(address), interface))
    if not addresses:
        return {"ip": "Offline", "interface": ""}
    _, _, address, interface = min(addresses)
    return {"ip": address, "interface": interface}


def _device_network_info():
    try:
        output = _run([
            "ip", "-o", "-4", "address", "show", "up", "scope", "global",
        ], check=False, timeout=3)
    except (OSError, subprocess.SubprocessError):
        output = ""
    return _parse_device_network_info(output)


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
    # ROCKNIX normally exposes set_setting as a profile function rather than
    # an executable. Use that native implementation so its shared config lock
    # protects concurrent changes from the system UI and services. Values are
    # passed positionally, never interpolated into the shell program.
    try:
        native = _run([
            "/bin/bash", "-c",
            '. /etc/profile >/dev/null 2>&1; '
            'declare -F set_setting >/dev/null && printf yes',
        ], check=False)
    except OSError:
        native = ""
    if native == "yes":
        _run([
            "/bin/bash", "-c",
            '. /etc/profile >/dev/null 2>&1; set_setting "$1" "$2"',
            "rke-set-setting", str(name), str(value),
        ])
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


def _atomic_executable_copy(source, target, mode=0o755):
    """Stage an executable without following an existing target symlink."""
    source = Path(source)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target_mode = target.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(target_mode):
            raise RuntimeError(
                f"Refusing unsafe updater staging path: {target}")
    temporary = None
    try:
        with NamedTemporaryFile(
                "wb", dir=target.parent,
                prefix=f".{target.name}.", delete=False) as output:
            temporary = Path(output.name)
            with source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(mode)
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _legacy_conflict_approval_token(directory):
    """Bind a destructive request to one real directory and manifest inode."""
    directory = Path(directory)
    manifest = directory / "plugin.json"
    directory_status = directory.lstat()
    manifest_status = manifest.lstat()
    if (not stat.S_ISDIR(directory_status.st_mode) or
            not stat.S_ISREG(manifest_status.st_mode) or
            any(ord(character) < 32 or ord(character) == 127
                for character in directory.name)):
        raise RuntimeError("The conflict path is unsafe for automatic removal.")
    try:
        metadata = json.loads(manifest.read_text())
    except (OSError, ValueError, json.JSONDecodeError) as reason:
        raise RuntimeError(
            "The conflict manifest could not be validated.") from reason
    if (not isinstance(metadata, dict) or
            not isinstance(metadata.get("name"), str) or
            metadata["name"].strip().casefold() !=
            LEGACY_CONTROL_PLUGIN_NAME.casefold()):
        raise RuntimeError("The approved plugin is no longer ROCKNIX Control.")
    fingerprint = (
        f"{directory}\t"
        f"{directory_status.st_dev}:{directory_status.st_ino}:"
        f"{int(directory_status.st_ctime)}\t"
        f"{manifest_status.st_dev}:{manifest_status.st_ino}:"
        f"{int(manifest_status.st_ctime)}:{manifest_status.st_size}\t"
        f"{_sha256_file(manifest)}\n"
    )
    return hashlib.sha256(fingerprint.encode()).hexdigest()


def _frontend_bundle_id(plugin_dir):
    """Validate and identify the exact built frontend artifact."""
    plugin_dir = Path(plugin_dir)
    index_path = plugin_dir / "dist" / "index.js"
    manifest_path = plugin_dir / "dist" / "frontend-integrity.json"
    try:
        bundle = index_path.read_bytes()
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return ""
    pattern = re.compile(br"rke-frontend-sha256-v1:([0-9a-f]{64})")
    matches = list(pattern.finditer(bundle))
    if len(matches) != 1 or not isinstance(manifest, dict):
        return ""
    match = matches[0]
    digest = match.group(1).decode("ascii")
    normalized = (
        bundle[:match.start(1)] + (b"0" * 64) + bundle[match.end(1):])
    bundle_id = match.group(0).decode("ascii")
    if (hashlib.sha256(normalized).hexdigest() != digest or
            manifest.get("protocol") != 1 or
            manifest.get("algorithm") != "sha256-normalized-v1" or
            manifest.get("bundle_id") != bundle_id or
            manifest.get("index_sha256") != hashlib.sha256(bundle).hexdigest()):
        return ""
    return bundle_id


@contextmanager
def _exclusive_file_lock(path, timeout=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        if timeout is None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + max(0.0, float(timeout))
            while True:
                try:
                    fcntl.flock(
                        handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"timed out acquiring lifecycle lock {path}")
                    time.sleep(min(0.05, max(
                        0.0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _publish_lifecycle_current(payload):
    """Atomically publish a generation under the shared maintenance lock."""
    with _exclusive_file_lock(LIFECYCLE_LOCK, timeout=4):
        _atomic_text(LIFECYCLE_CURRENT, payload)
        LIFECYCLE_CURRENT.chmod(0o600)


def _remove_lifecycle_current(token):
    """Remove CURRENT only if it still names the caller's generation."""
    with _exclusive_file_lock(LIFECYCLE_LOCK, timeout=0.5):
        try:
            current = json.loads(LIFECYCLE_CURRENT.read_text())
        except (OSError, ValueError, json.JSONDecodeError):
            current = {}
        if current.get("token") != token:
            return False
        LIFECYCLE_CURRENT.unlink(missing_ok=True)
        return True


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
        self.install_health_request_path = (
            self.settings_dir / INSTALL_HEALTH_REQUEST_FILE)
        self.install_backend_ready_path = (
            self.settings_dir / INSTALL_BACKEND_READY_FILE)
        self.install_frontend_ready_path = (
            self.settings_dir / INSTALL_FRONTEND_READY_FILE)
        self.install_progress_path = (
            self.settings_dir / INSTALL_PROGRESS_FILE)
        self.install_progress_ack_path = (
            self.settings_dir / INSTALL_PROGRESS_ACK_FILE)
        self.backup_root = self.settings_dir.parent.parent / "plugin-backups"
        self.plugins_root = Path(__file__).resolve().parent.parent
        self.install_health_response = None
        self.install_status_generation = 0
        self.install_status_transaction_id = ""
        self.plugin_conflict_fingerprint = ""
        self.startup_rgb_pending = False
        self.mutation_transaction_local = threading.local()
        self.legacy_fan_guard_marker = self.settings_dir / "fan-curve-session.active"
        self.runtime_marker = self.settings_dir / "runtime-session.active"
        self.runtime_restore_request = (
            self.settings_dir / "runtime-session.active.restore-request")
        self.runtime_state_path = self.settings_dir / "runtime-session.json"
        self.runtime_lock_path = self.settings_dir / "runtime-session.lock"
        self.runtime_restore_path = self.settings_dir / "runtime-restore.py"
        self.runtime_guard_path = self.settings_dir / "runtime-restore-guard.sh"
        self.runtime_guard_unit = ""
        self.runtime_guard_identity = None
        self.plugin_loader_recovery_path = (
            self.settings_dir / "plugin_loader_recovery.py")
        self.canonical_fan_config = self.settings_dir / "rocknix-custom-fancontrol.conf"
        self.active_preset = DEFAULT_PRESET
        self.active_appid = ""
        self.last_cpu_sample = None
        self.last_gpu_sample = None
        self.gamescope_pid = None
        self.gpu_fdinfo_paths = []
        self.gpu_fdinfo_refresh = 0.0
        self.monitor_lock = threading.RLock()
        self.monitor_session = ""
        self.monitor_generation = 0
        self.monitor_revision = 0
        self.monitor_bypass_active = False
        self.monitor_charging_valid = None
        self.last_rke_cpu_sample = None
        self.backend_identity = _process_identity(os.getpid())
        self.battery_discharge_ema = None
        self.battery_discharge_samples = 0
        self.battery_discharge_last_sample = 0.0
        self.log_offsets = {}
        self.latest_release_cache = (0.0, [])
        self.session_lock = threading.RLock()
        self.charging = ChargingController(self.settings_dir)
        self.rgb = RGBController(
            self.settings_dir, run=_run, get_setting=_get_setting,
            set_setting=_set_setting, get_runtime_capability=_rocknix_env)
        self.charging_status_warning = ""
        self.lock = None
        self.rgb_lock = None
        self.game_watch_task = None
        self.lifecycle_heartbeat_task = None
        self.lifecycle_token = ""
        self.lifecycle_lease_path = None
        self.lifecycle_active_path = None
        self.lifecycle_heartbeat_path = None
        self.lifecycle_ready_path = None
        self.lifecycle_guard_unit = ""
        self.lifecycle_guard_identity = None

    def _plugin_version(self):
        plugin_dir = Path(__file__).resolve().parent
        version = _read(plugin_dir / "VERSION")
        if version:
            return version
        try:
            metadata = json.loads((plugin_dir / "plugin.json").read_text())
        except (OSError, ValueError, json.JSONDecodeError):
            return ""
        return str(metadata.get("version", "")).strip()

    def _install_health_request(self):
        try:
            request = json.loads(self.install_health_request_path.read_text())
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(request, dict):
            return None
        nonce = request.get("nonce")
        if (request.get("protocol") != INSTALL_HEALTH_PROTOCOL or
                not isinstance(nonce, str) or not 16 <= len(nonce) <= 128):
            return None
        return request

    def _publish_backend_install_health(self):
        """Answer only the exact, co-located installer health challenge."""
        request = self._install_health_request()
        if request is None:
            self.install_health_response = None
            return None

        plugin_dir = Path(__file__).resolve().parent
        version = self._plugin_version()
        boot_id = _read("/proc/sys/kernel/random/boot_id")
        main_hash = _sha256_file(plugin_dir / "main.py")
        dist_hash = _sha256_file(plugin_dir / "dist" / "index.js")
        frontend_bundle_id = _frontend_bundle_id(plugin_dir)
        require_frontend = request.get("require_frontend")
        expected = {
            "version": version,
            "boot_id": boot_id,
            "main_sha256": main_hash,
            "dist_sha256": dist_hash,
            "frontend_bundle_id": frontend_bundle_id,
            "require_frontend": require_frontend,
        }
        if (not re.fullmatch(
                r"rke-frontend-sha256-v1:[0-9a-f]{64}",
                frontend_bundle_id) or
                not re.fullmatch(r"[0-9a-f]{32}", self.lifecycle_token) or
                not isinstance(require_frontend, bool) or
                any(request.get(key) != value for key, value in expected.items())):
            decky.logger.error(
                "Install health challenge does not match the running release")
            self.install_health_response = None
            return False

        try:
            loader_pid = int(_run([
                "systemctl", "show", "--property=MainPID", "--value",
                PLUGIN_LOADER_SERVICE,
            ], timeout=3) or "0")
        except (OSError, TypeError, ValueError, subprocess.SubprocessError):
            loader_pid = 0
        backend = _process_identity(os.getpid())
        loader = _process_identity(loader_pid)
        if backend is None or loader is None:
            decky.logger.error(
                "Install health could not identify the backend and PluginLoader")
            self.install_health_response = None
            return False

        response = {
            "protocol": INSTALL_HEALTH_PROTOCOL,
            "nonce": request["nonce"],
            **expected,
            "backend": backend,
            "loader": loader,
            "lifecycle_token": self.lifecycle_token,
            "ready_at": int(time.time()),
        }
        _atomic_text(
            self.install_backend_ready_path,
            json.dumps(response, indent=2, sort_keys=True) + "\n",
        )
        self.install_backend_ready_path.chmod(0o600)
        self.install_health_response = response
        return True

    async def report_frontend_ready(self, build_id):
        """Confirm that the exact loaded frontend completed a state RPC."""
        def work():
            request = self._install_health_request()
            if request is None:
                return None
            response = self.install_health_response
            bound_fields = (
                "protocol", "nonce", "version", "boot_id", "main_sha256",
                "dist_sha256", "frontend_bundle_id", "require_frontend",
            )
            if (not isinstance(response, dict) or
                    any(response.get(key) != request.get(key)
                        for key in bound_fields) or
                    not isinstance(build_id, str) or
                    build_id != request.get("frontend_bundle_id") or
                    build_id != response.get("frontend_bundle_id") or
                    build_id != _frontend_bundle_id(
                        Path(__file__).resolve().parent)):
                return False
            payload = {
                **response,
                "frontend_ready_at": int(time.time()),
            }
            _atomic_text(
                self.install_frontend_ready_path,
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
            )
            self.install_frontend_ready_path.chmod(0o600)
            return True
        return await asyncio.to_thread(work)

    def _plugin_conflict_state(self, record_transition=False):
        """Return exact legacy plugin conflicts from immediate plugin children.

        Detection deliberately uses the manifest identity, not a directory-name
        guess. Symlinked plugins are still conflicts (Decky may load them), but
        are never offered to the destructive maintenance helper.
        """
        plugins_root = Path(getattr(
            self, "plugins_root", Path(__file__).resolve().parent.parent))
        conflicts = []
        scan_errors = []
        try:
            children = sorted(plugins_root.iterdir(), key=lambda path: path.name)
        except OSError as reason:
            children = []
            scan_errors.append(f"Could not inspect Decky plugins: {reason}")
        for directory in children:
            try:
                directory_mode = directory.lstat().st_mode
            except FileNotFoundError:
                continue
            except OSError as reason:
                scan_errors.append(
                    f"Could not inspect plugin path {directory.name}: {reason}")
                continue
            if not (stat.S_ISDIR(directory_mode) or
                    (stat.S_ISLNK(directory_mode) and directory.is_dir())):
                continue
            manifest = directory / "plugin.json"
            try:
                manifest_mode = manifest.lstat().st_mode
                metadata = json.loads(manifest.read_text())
            except FileNotFoundError:
                continue
            except OSError as reason:
                scan_errors.append(
                    f"Could not inspect {directory.name}/plugin.json: {reason}")
                continue
            except (ValueError, json.JSONDecodeError):
                continue
            if (not isinstance(metadata, dict) or
                    not isinstance(metadata.get("name"), str) or
                    metadata["name"].strip().casefold() !=
                    LEGACY_CONTROL_PLUGIN_NAME.casefold()):
                continue
            version = str(metadata.get("version", "Unknown")).strip()
            if not version or len(version) > 80 or any(
                    character in version for character in "\r\n\0"):
                version = "Unknown"
            removable = (
                stat.S_ISDIR(directory_mode) and
                stat.S_ISREG(manifest_mode) and
                directory.parent == plugins_root
            )
            conflicts.append({
                "name": LEGACY_CONTROL_PLUGIN_NAME,
                "version": version,
                "directory": str(directory),
                "removable": removable,
                "reason": "" if removable else
                    "Symlinked plugin paths cannot be removed automatically.",
            })
        scan_error = "; ".join(scan_errors)
        blocked = bool(conflicts or scan_error)
        if conflicts:
            message = (
                "ROCKNIX Control is installed and can compete with "
                "RK-Enhanced for CPU, GPU, and fan control. Remove it before "
                "using RK-Enhanced controls."
            )
        elif scan_error:
            message = (
                f"{scan_error} RK-Enhanced controls are disabled until the "
                "plugin directory can be checked."
            )
        else:
            message = ""
        state = {
            "blocked": blocked,
            "conflicts": conflicts,
            "message": message,
            "scan_error": scan_error,
        }
        if record_transition:
            fingerprint = json.dumps(state, sort_keys=True)
            previous = getattr(self, "plugin_conflict_fingerprint", "")
            if fingerprint != previous:
                if blocked:
                    decky.logger.error(
                        "RK-Enhanced mutations blocked: " + message)
                elif previous:
                    decky.logger.info(
                        "ROCKNIX Control conflict cleared; RK-Enhanced controls are available")
                self.plugin_conflict_fingerprint = fingerprint
        return state

    def _require_mutations_allowed(self):
        local = getattr(self, "mutation_transaction_local", None)
        in_transaction = bool(local and getattr(local, "depth", 0))
        if not in_transaction:
            install_status = self._install_status()
            if install_status["active"]:
                raise RuntimeError(
                    "An RK-Enhanced installation is in progress; controls are temporarily disabled.")
            try:
                with _exclusive_file_lock(INSTALL_TRANSACTION_LOCK, timeout=0):
                    pass
            except TimeoutError as reason:
                raise RuntimeError(
                    "An RK-Enhanced installation is starting; controls are temporarily disabled.") from reason
            except OSError as reason:
                raise RuntimeError(
                    "The RK-Enhanced installation lock could not be verified; controls are disabled.") from reason
        conflict = self._plugin_conflict_state(record_transition=True)
        if conflict["blocked"]:
            raise RuntimeError(conflict["message"])
        return True

    @contextmanager
    def _install_mutation_guard(self):
        """Serialize a complete backend mutation against install replacement."""
        local = getattr(self, "mutation_transaction_local", None)
        if local is None:
            local = threading.local()
            self.mutation_transaction_local = local
        depth = int(getattr(local, "depth", 0) or 0)
        if depth:
            local.depth = depth + 1
            try:
                yield
            finally:
                local.depth = depth
            return
        try:
            with _exclusive_file_lock(INSTALL_TRANSACTION_LOCK, timeout=0):
                local.depth = 1
                try:
                    yield
                finally:
                    local.depth = 0
        except TimeoutError as reason:
            raise RuntimeError(
                "An RK-Enhanced installation is in progress; controls are disabled.") from reason

    @contextmanager
    def _hardware_mutation_guard(self):
        """Serialize and conflict-check one complete hardware/preset change."""
        with self._install_mutation_guard():
            conflict = self._plugin_conflict_state(
                record_transition=True)
            if conflict["blocked"]:
                raise RuntimeError(conflict["message"])
            yield

    def _run_hardware_mutation(self, function, *arguments):
        with self._hardware_mutation_guard():
            return function(*arguments)

    def _run_install_mutation(self, function, *arguments):
        with self._install_mutation_guard():
            return function(*arguments)

    def _install_progress_paths(self):
        settings_dir = Path(getattr(
            self, "settings_dir",
            Path(__file__).resolve().parent / "settings"))
        progress = Path(getattr(
            self, "install_progress_path",
            settings_dir / INSTALL_PROGRESS_FILE))
        acknowledgement = Path(getattr(
            self, "install_progress_ack_path",
            settings_dir / INSTALL_PROGRESS_ACK_FILE))
        return progress, acknowledgement

    @staticmethod
    def _idle_install_status(message=""):
        return {
            "protocol": INSTALL_PROGRESS_PROTOCOL,
            "transaction_id": "",
            "generation": 0,
            "active": False,
            "terminal": False,
            "kind": "",
            "source_version": "",
            "target_version": "",
            "decky_version": "",
            "phase": "idle",
            "message": str(message),
            "outcome": "idle",
            "started_at": 0,
            "updated_at": 0,
            "success": False,
            "rolled_back": False,
            "error": "",
            "acknowledged": False,
        }

    @staticmethod
    def _validate_install_status_payload(payload):
        if not isinstance(payload, dict):
            raise ValueError("installer progress is not an object")
        transaction_id = payload.get("transaction_id")
        generation = payload.get("generation")
        phase = payload.get("phase")
        outcome = payload.get("outcome")
        started_at = payload.get("started_at")
        updated_at = payload.get("updated_at")
        writer = payload.get("writer")
        if payload.get("protocol") != INSTALL_PROGRESS_PROTOCOL:
            raise ValueError("unsupported installer progress protocol")
        if (not isinstance(transaction_id, str) or
                not re.fullmatch(
                    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", transaction_id)):
            raise ValueError("invalid installer transaction id")
        if (isinstance(generation, bool) or not isinstance(generation, int) or
                generation < 1):
            raise ValueError("invalid installer generation")
        if phase not in INSTALL_PROGRESS_PHASES:
            raise ValueError("invalid installer phase")
        if outcome not in INSTALL_PROGRESS_OUTCOMES:
            raise ValueError("invalid installer outcome")
        for field in ("active", "terminal", "rolled_back"):
            if not isinstance(payload.get(field), bool):
                raise ValueError(f"invalid installer {field} flag")
        if (payload.get("success") is not None and
                not isinstance(payload.get("success"), bool)):
            raise ValueError("invalid installer success state")
        for field in (
                "kind", "source_version", "target_version", "decky_version",
                "message"):
            value = payload.get(field)
            if (not isinstance(value, str) or len(value) > 2048 or
                    "\0" in value):
                raise ValueError(f"invalid installer {field}")
        error = payload.get("error")
        if (error is not None and
                (not isinstance(error, str) or len(error) > 2048 or
                 "\0" in error)):
            raise ValueError("invalid installer error")
        if not isinstance(writer, dict):
            raise ValueError("invalid installer writer")
        writer_pid = writer.get("pid")
        writer_start = writer.get("start_time_ticks")
        writer_boot = writer.get("boot_id")
        if (isinstance(writer_pid, bool) or not isinstance(writer_pid, int) or
                writer_pid <= 0 or isinstance(writer_start, bool) or
                not isinstance(writer_start, int) or writer_start <= 0 or
                not isinstance(writer_boot, str) or not writer_boot or
                len(writer_boot) > 128 or "\0" in writer_boot):
            raise ValueError("invalid installer writer identity")
        if (isinstance(started_at, bool) or not isinstance(started_at, int) or
                started_at <= 0 or isinstance(updated_at, bool) or
                not isinstance(updated_at, int) or updated_at < started_at):
            raise ValueError("invalid installer timestamps")
        if payload["active"] == payload["terminal"]:
            raise ValueError("installer active/terminal state is inconsistent")
        if payload["active"] and outcome != "running":
            raise ValueError("active installer outcome is not running")
        if payload["active"] and payload["success"] is not None:
            raise ValueError("active installer success state is already final")
        if payload["terminal"] and outcome == "running":
            raise ValueError("terminal installer outcome is still running")
        if (payload["terminal"] and
                payload["success"] != (outcome == "succeeded")):
            raise ValueError("installer success state is inconsistent")
        if payload["rolled_back"] != (outcome == "rolled-back"):
            raise ValueError("installer rollback state is inconsistent")
        normalized = dict(payload)
        normalized["writer"] = dict(writer)
        normalized["error"] = error or ""
        return normalized

    def _interrupt_stale_install_status(self, status, progress_path):
        if not status["active"]:
            return status
        writer = status["writer"]
        current_boot = _read("/proc/sys/kernel/random/boot_id")
        writer_identity = _process_identity(writer["pid"])
        writer_alive = (
            writer_identity is not None and
            writer_identity.get("start_time_ticks") ==
            writer["start_time_ticks"]
        )
        stale = (
            not current_boot or writer["boot_id"] != current_boot or
            not writer_alive or
            time.time() - status["updated_at"] >
            INSTALL_PROGRESS_STALE_SECONDS
        )
        if not stale:
            return status
        # Both the SSH installer and detached updater hold this exact lock for
        # the complete transaction. Acquiring it proves that an old active
        # record has no live owner; this avoids mistaking a slow manual install
        # for a crash merely because its progress timestamp is old.
        try:
            with _exclusive_file_lock(INSTALL_TRANSACTION_LOCK, timeout=0):
                try:
                    current = self._validate_install_status_payload(
                        json.loads(progress_path.read_text()))
                except (OSError, ValueError, json.JSONDecodeError):
                    return status
                if (current["transaction_id"] != status["transaction_id"] or
                        current["generation"] != status["generation"] or
                        not current["active"]):
                    return current
                now = int(time.time())
                current.update({
                    "active": False,
                    "terminal": True,
                    "phase": "failed",
                    "message": "Installation was interrupted before completion.",
                    "outcome": "failed",
                    "updated_at": max(now, current["started_at"]),
                    "success": False,
                    "rolled_back": False,
                    "error": "The installer transaction is no longer running.",
                })
                _atomic_text(
                    progress_path,
                    json.dumps(current, indent=2, sort_keys=True) + "\n")
                progress_path.chmod(0o600)
                decky.logger.error(
                    "Marked stale installer transaction "
                    f"{current['transaction_id']} as interrupted")
                return current
        except (OSError, TimeoutError):
            return status

    def _install_status(self):
        progress_path, acknowledgement_path = self._install_progress_paths()
        try:
            payload = json.loads(progress_path.read_text())
            status = self._validate_install_status_payload(payload)
            status = self._interrupt_stale_install_status(
                status, progress_path)
            status = self._validate_install_status_payload(status)
        except FileNotFoundError:
            settings_dir = Path(getattr(
                self, "settings_dir",
                Path(__file__).resolve().parent / "settings"))
            return self._idle_install_status(
                _read(settings_dir / UPDATE_STATUS_FILE))
        except (OSError, ValueError, json.JSONDecodeError) as reason:
            unavailable = self._idle_install_status(
                "Installer status is unavailable.")
            unavailable.update({
                "terminal": True,
                "phase": "unavailable",
                "outcome": "unavailable",
                "error": str(reason),
            })
            return unavailable

        seen_generation = int(getattr(
            self, "install_status_generation", 0) or 0)
        seen_transaction = str(getattr(
            self, "install_status_transaction_id", "") or "")
        if (status["generation"] < seen_generation or
                (status["generation"] == seen_generation and
                 seen_transaction and
                 status["transaction_id"] != seen_transaction)):
            unavailable = self._idle_install_status(
                "Installer status was rejected as stale.")
            unavailable.update({
                "terminal": True,
                "phase": "unavailable",
                "outcome": "unavailable",
                "error": "Installer generation moved backwards or changed identity.",
            })
            return unavailable
        self.install_status_generation = status["generation"]
        self.install_status_transaction_id = status["transaction_id"]
        status["acknowledged"] = (
            status["terminal"] and
            _read(acknowledgement_path) == status["transaction_id"]
        )
        return status

    async def get_install_status(self):
        return await asyncio.to_thread(self._install_status)

    async def ack_install_status(self, transaction_id):
        def work():
            requested = str(transaction_id)
            status = self._install_status()
            if (not status["terminal"] or not status["transaction_id"] or
                    requested != status["transaction_id"]):
                raise ValueError(
                    "only the current completed installer transaction can be dismissed")
            _, acknowledgement_path = self._install_progress_paths()
            _atomic_text(acknowledgement_path, requested + "\n")
            acknowledgement_path.chmod(0o600)
            return True
        return await asyncio.to_thread(work)

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
        ownership_available = (
            not self._install_status()["active"] and
            not self._plugin_conflict_state()["blocked"]
        )
        if changed and ownership_available:
            self._save(data)
        if ownership_available:
            self._write_canonical_fan_config(data["system_fan_curve"])
        # Creating the system Custom curve changes the same file used by the
        # legacy control plugin. Keep settings migration readable, but never
        # claim that shared fan boundary while a conflict exists.
        if ownership_available and not FAN_CONFIG.exists():
            try:
                with self._hardware_mutation_guard():
                    if not FAN_CONFIG.exists():
                        _write_fan_curve(data["system_fan_curve"])
            except RuntimeError:
                # A transaction or conflict can appear after the read-only
                # checks. State loading must stay available; Custom curve
                # creation is safely deferred to a later generation.
                pass
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
        return {
            "version": 1,
            "owner_pid": os.getpid(),
            "boot_id": _read("/proc/sys/kernel/random/boot_id"),
            "created": int(time.time()),
            "controls": {
                "cpu": cpu,
                "gpu": gpu,
                "scheduler": scheduler,
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

    def _install_plugin_loader_recovery_tool(self):
        source = Path(__file__).resolve().with_name(
            "plugin_loader_recovery.py")
        if not source.is_file():
            raise RuntimeError("PluginLoader recovery helper is missing")
        self.settings_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, self.plugin_loader_recovery_path)
        self.plugin_loader_recovery_path.chmod(0o755)

    def _start_plugin_lifecycle_guard(self):
        """Start an out-of-cgroup guard for this exact backend generation."""
        self.lifecycle_guard_unit = ""
        self.lifecycle_guard_identity = None
        self._install_plugin_loader_recovery_tool()
        owner = _process_identity(os.getpid())
        try:
            loader_pid = int(_run([
                "systemctl", "show", "--property=MainPID", "--value",
                PLUGIN_LOADER_SERVICE,
            ], timeout=3) or "0")
        except (TypeError, ValueError):
            loader_pid = 0
        loader = _process_identity(loader_pid)
        boot_id = _read("/proc/sys/kernel/random/boot_id")
        if owner is None or loader is None or not boot_id:
            raise RuntimeError("could not identify the PluginLoader generation")
        self.backend_identity = owner

        token = secrets.token_hex(16)
        LIFECYCLE_RUN_ROOT.mkdir(parents=True, exist_ok=True)
        lease_path = LIFECYCLE_RUN_ROOT / f"plugin-lifecycle-{token}.json"
        active_path = LIFECYCLE_RUN_ROOT / f"plugin-lifecycle-{token}.active"
        heartbeat_path = (
            LIFECYCLE_RUN_ROOT / f"plugin-lifecycle-{token}.heartbeat")
        ready_path = (
            LIFECYCLE_RUN_ROOT / f"plugin-lifecycle-{token}.ready")
        lease = {
            "version": 1,
            "token": token,
            "boot_id": boot_id,
            "owner": owner,
            "loader": loader,
        }
        payload = json.dumps(lease, indent=2, sort_keys=True) + "\n"
        unit = f"rke-plugin-lifecycle-guard-{owner['pid']}-{token[:8]}"
        try:
            LIFECYCLE_RUN_ROOT.chmod(0o700)
            _atomic_text(lease_path, payload)
            lease_path.chmod(0o600)
            _atomic_text(active_path, token + "\n")
            active_path.chmod(0o600)
            _atomic_text(heartbeat_path, f"{time.monotonic_ns()}\n")
            heartbeat_path.chmod(0o600)
            _run([
                "systemd-run", f"--unit={unit}", "--collect",
                str(self.plugin_loader_recovery_path), "guard",
                str(lease_path),
            ], timeout=3)

            # The helper writes readiness only after validating the immutable
            # lease, exact owner/Loader identities, active marker and initial
            # monotonic heartbeat. Keep the old CURRENT lease published until
            # that independent process is ready to receive the handoff.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if _read(ready_path) == token:
                    break
                time.sleep(0.05)
            else:
                raise RuntimeError(
                    "PluginLoader lifecycle guard did not become ready")

            _publish_lifecycle_current(payload)
        except Exception:
            try:
                active_path.unlink(missing_ok=True)
            except OSError:
                pass
            for artifact in (heartbeat_path, ready_path, lease_path):
                try:
                    artifact.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                _remove_lifecycle_current(token)
            except OSError:
                pass
            self.lifecycle_guard_unit = ""
            self.lifecycle_guard_identity = None
            raise

        self.lifecycle_guard_unit = unit
        self.lifecycle_guard_identity = _guard_unit_identity(unit)
        self.lifecycle_token = token
        self.lifecycle_lease_path = lease_path
        self.lifecycle_active_path = active_path
        self.lifecycle_heartbeat_path = heartbeat_path
        self.lifecycle_ready_path = ready_path
        decky.logger.info(
            f"Started PluginLoader lifecycle guard for backend PID {owner['pid']}")

    async def _lifecycle_heartbeat_loop(self):
        while self.lifecycle_active_path is not None:
            try:
                if not self.lifecycle_active_path.exists():
                    return
                _atomic_text(
                    self.lifecycle_heartbeat_path,
                    f"{time.monotonic_ns()}\n",
                )
            except OSError as reason:
                decky.logger.error(
                    f"PluginLoader lifecycle heartbeat failed: {reason}")
                return
            await asyncio.sleep(5)

    def _mark_lifecycle_guard_clean(self):
        token = self.lifecycle_token
        if not token:
            self.lifecycle_guard_unit = ""
            self.lifecycle_guard_identity = None
            return False
        errors = []
        # Tombstone first. The independent guard treats a missing active file
        # as authoritative clean unload even if later artifact cleanup fails.
        for artifact in (
                self.lifecycle_active_path,
                self.lifecycle_heartbeat_path,
                self.lifecycle_ready_path,
                self.lifecycle_lease_path):
            if artifact is None:
                continue
            try:
                artifact.unlink(missing_ok=True)
            except OSError as reason:
                errors.append(f"{artifact.name}: {reason}")
        try:
            _remove_lifecycle_current(token)
        except OSError as reason:
            errors.append(f"current lease: {reason}")
        finally:
            self.lifecycle_token = ""
            self.lifecycle_lease_path = None
            self.lifecycle_active_path = None
            self.lifecycle_heartbeat_path = None
            self.lifecycle_ready_path = None
            self.lifecycle_guard_unit = ""
            self.lifecycle_guard_identity = None
        if errors:
            decky.logger.warning(
                "PluginLoader lifecycle cleanup was incomplete: " +
                "; ".join(errors))
        return True

    def _request_detached_runtime_restore(self):
        """Hand clean-unload restoration to a unit outside PluginLoader."""
        if not self.runtime_marker.exists():
            self.runtime_restore_request.unlink(missing_ok=True)
            return ""
        # The existing per-session guard observes this request independently
        # of the Decky/FEX owner PID. Write it before attempting the faster
        # detached unit or refreshing its installed tools, so either failure
        # cannot strand applied state in an owner process which Decky happens
        # to keep alive.
        _atomic_text(
            self.runtime_restore_request,
            f"restore requested by {os.getpid()}\n",
        )
        self._install_runtime_restore_tools()
        unit = f"rke-runtime-restore-clean-{os.getpid()}-{time.monotonic_ns()}"
        try:
            _run([
                "systemd-run", f"--unit={unit}", "--collect",
                str(self.runtime_restore_path), str(self.runtime_marker),
                str(self.runtime_state_path), str(self.canonical_fan_config),
                str(FAN_CONFIG),
            ], timeout=1.5)
        except Exception as reason:
            decky.logger.warning(
                "Immediate detached runtime restoration could not start; "
                f"the existing session guard has the request: {reason}")
            return "guard"
        return "detached"

    def _ensure_runtime_session_locked(self, capabilities):
        if self.runtime_marker.exists():
            return self._runtime_state()
        self.runtime_guard_unit = ""
        self.runtime_guard_identity = None
        self.runtime_restore_request.unlink(missing_ok=True)
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
            self.runtime_guard_unit = ""
            self.runtime_guard_identity = None
            self.runtime_marker.unlink(missing_ok=True)
            self.runtime_state_path.unlink(missing_ok=True)
            raise
        self.runtime_guard_unit = unit
        self.runtime_guard_identity = _guard_unit_identity(unit)
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
            self.runtime_guard_unit = ""
            self.runtime_guard_identity = None
            return False
        self._install_runtime_restore_tools()
        output = _run([
            str(self.runtime_restore_path), str(self.runtime_marker),
            str(self.runtime_state_path), str(self.canonical_fan_config),
            str(FAN_CONFIG),
        ], timeout=30)
        if self.runtime_marker.exists():
            raise RuntimeError(output or "runtime restoration did not complete")
        self.runtime_guard_unit = ""
        self.runtime_guard_identity = None
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
        conflict = self._plugin_conflict_state(record_transition=True)
        effective = _get_setting("cooling.profile", "")
        capabilities = _capabilities()
        capabilities["rgb"] = self.rgb.capabilities()
        return {"capabilities": capabilities, **data,
                "active_preset": self.active_preset, "active_appid": self.active_appid,
                "effective_cooling_profile": effective,
                "fan_curve_active": effective == "custom" and self._fan_session_active(),
                "plugin_conflict": conflict,
                "mutations_blocked": conflict["blocked"]}

    def _apply(self, profile, capabilities=None):
        with self._hardware_mutation_guard():
            return self._apply_hardware(profile, capabilities)

    def _apply_hardware(self, profile, capabilities=None):
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

    async def get_plugin_conflict(self):
        return await asyncio.to_thread(
            self._plugin_conflict_state, True)

    async def remove_plugin_conflict(self):
        """Start exact legacy-plugin removal outside PluginLoader's cgroup."""
        def work():
            if self._install_status()["active"]:
                raise RuntimeError(
                    "An RK-Enhanced installation is already in progress.")
            conflict = self._plugin_conflict_state(record_transition=True)
            removable = [
                item for item in conflict["conflicts"]
                if item.get("removable")
            ]
            if not removable:
                if conflict["conflicts"]:
                    raise RuntimeError(
                        "The conflicting plugin path cannot be removed automatically.")
                raise RuntimeError("No ROCKNIX Control plugin was detected.")
            # Remove one exact plugin per Loader generation. If both legacy
            # variants were installed separately, the next generation remains
            # blocked and offers the second exact path rather than silently
            # broadening this destructive request.
            target_directory = removable[0]["directory"]
            approval_token = _legacy_conflict_approval_token(
                target_directory)
            source = Path(__file__).resolve().parent / "updater.sh"
            if not source.exists() or not stat.S_ISREG(source.lstat().st_mode):
                raise RuntimeError(
                    "updater.sh is missing from this RK-Enhanced installation")
            detached = self.settings_dir / "maintenance-updater.sh"
            try:
                detached_mode = detached.lstat().st_mode
            except FileNotFoundError:
                detached_mode = None
            if detached_mode is not None and not stat.S_ISREG(detached_mode):
                raise RuntimeError(
                    "the detached maintenance helper path is not a regular file")
            self.settings_dir.mkdir(parents=True, exist_ok=True)
            _atomic_executable_copy(source, detached, mode=0o700)
            before = self._install_status()
            unit = (
                f"rk-enhanced-conflict-removal-{os.getpid()}-"
                f"{time.monotonic_ns()}")
            _run(["systemctl", "reset-failed", unit + ".service"],
                 check=False)
            _run([
                "systemd-run", f"--unit={unit}", "--collect",
                str(detached), "--remove-rocknix-control", target_directory,
                approval_token,
            ], timeout=5)
            transaction_id = ""
            deadline = time.monotonic() + 0.75
            while time.monotonic() < deadline:
                current = self._install_status()
                if (current["generation"] > before["generation"] and
                        current.get("kind") == "remove-conflict"):
                    transaction_id = current["transaction_id"]
                    break
                time.sleep(0.05)
            decky.logger.info(
                "Detached ROCKNIX Control removal started for exact path: "
                f"{target_directory}")
            return {
                "started": True,
                "transaction_id": transaction_id,
                "message": (
                    "Removing ROCKNIX Control without a backup; Decky will reload."
                ),
                "conflict": removable[0],
            }
        return await asyncio.to_thread(work)

    async def get_device_network_info(self):
        return await asyncio.to_thread(_device_network_info)

    async def get_rgb_state(self):
        return await asyncio.to_thread(self.rgb.get_state)

    async def set_rgb_state(self, request):
        self._require_mutations_allowed()
        def work():
            with self._hardware_mutation_guard():
                return self.rgb.set_state(request)
        if self.rgb_lock is None:
            self.rgb_lock = asyncio.Lock()
        async with self.rgb_lock:
            state = await asyncio.to_thread(work)
        effect = state.get("effect") or (state.get("lighting") or {}).get("effect")
        decky.logger.info(
            f"Applied native RGB state: mode={state.get('mode')} "
            f"effect={effect}")
        return state

    async def set_rgb_calibration(self, request):
        self._require_mutations_allowed()
        def work():
            with self._hardware_mutation_guard():
                return self.rgb.set_calibration(request)
        if self.rgb_lock is None:
            self.rgb_lock = asyncio.Lock()
        async with self.rgb_lock:
            state = await asyncio.to_thread(work)
        calibration = state.get("calibration") or {}
        decky.logger.info(
            "Applied native RGB calibration: "
            f"green={calibration.get('green_percent')} "
            f"blue={calibration.get('blue_percent')}")
        return state

    async def apply_profile(self, profile):
        self._require_mutations_allowed()
        if self.lock is None:
            self.lock = asyncio.Lock()
        async with self.lock:
            return await asyncio.to_thread(self._apply, profile)

    async def save_preset(self, name, profile):
        def work():
            self._require_mutations_allowed()
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
        return await asyncio.to_thread(self._run_hardware_mutation, work)

    async def restore_steam_default(self):
        def work():
            self._require_mutations_allowed()
            data = self._load()
            capabilities = _capabilities()
            restored = _validate_profile(data["steam_default_original"], capabilities)
            data["presets"][DEFAULT_PRESET] = restored
            self._save(data)
            if self.active_preset == DEFAULT_PRESET:
                self._apply(restored, capabilities)
            decky.logger.info("Restored RK-E Default from its original setup snapshot")
            return self._state()
        return await asyncio.to_thread(self._run_hardware_mutation, work)

    async def rename_preset(self, old_name, new_name):
        def work():
            self._require_mutations_allowed()
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
        return await asyncio.to_thread(self._run_hardware_mutation, work)

    async def delete_preset(self, name):
        def work():
            self._require_mutations_allowed()
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
        return await asyncio.to_thread(self._run_hardware_mutation, work)

    async def assign_game(self, appid, preset):
        def work():
            self._require_mutations_allowed()
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
        return await asyncio.to_thread(self._run_hardware_mutation, work)

    async def set_steam_default(self, preset):
        def work():
            self._require_mutations_allowed()
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
        return await asyncio.to_thread(self._run_hardware_mutation, work)

    async def save_system_fan_curve(self, curve):
        def work():
            self._require_mutations_allowed()
            with self._hardware_mutation_guard():
                clean = _normalize_fan_curve(curve)
                if not isinstance(curve, list) or len(clean) != len(curve):
                    raise ValueError(
                        "system fan curve must contain valid unique points")
                previous_temp, previous_pwm = -1, -1
                for temp, pwm in clean:
                    if not 10000 <= temp <= 120000 or not 0 <= pwm <= 255:
                        raise ValueError(
                            "fan temperatures must be 10-120°C and PWM must be 0-255")
                    if temp <= previous_temp or pwm < previous_pwm:
                        raise ValueError(
                            "fan temperature and PWM points must rise")
                    previous_temp, previous_pwm = temp, pwm
                data = self._load()
                data["system_fan_curve"] = clean
                self._save(data)
                self._write_canonical_fan_config(clean)
                if not self._fan_session_active():
                    _write_fan_curve(clean)
                    if _get_setting("cooling.profile", "") == "custom":
                        _restart_fancontrol()
                decky.logger.info(
                    f"Saved ROCKNIX Custom system fan curve: {clean}")
                return self._state()
        return await asyncio.to_thread(self._run_hardware_mutation, work)

    async def unassign_game(self, appid):
        def work():
            self._require_mutations_allowed()
            data = self._load()
            game = str(appid)
            data["game_profiles"].pop(game, None)
            self._save(data)
            if game == self.active_appid:
                fallback = data["steam_default"]
                self._apply(data["presets"][fallback])
                self.active_preset = fallback
            return self._state()
        return await asyncio.to_thread(self._run_hardware_mutation, work)

    async def activate_game(self, appid):
        def work():
            self._require_mutations_allowed()
            target = str(appid or "")
            data = self._load()
            preset = data["game_profiles"].get(target, data["steam_default"])
            needs_fan_apply = (
                _get_setting("cooling.profile", "") == "custom" and
                not self._fan_session_active()
            )
            needs_runtime_apply = not self.runtime_marker.exists()
            if (target == self.active_appid and preset == self.active_preset and
                    not needs_runtime_apply and not needs_fan_apply):
                return {"applied": False, "preset": self.active_preset}
            self.active_appid = target
            self.gpu_fdinfo_paths = []
            self.gpu_fdinfo_refresh = 0.0
            self.last_gpu_sample = None
            self._apply(data["presets"][preset])
            self.active_preset = preset
            return {"applied": True, "preset": preset}
        return await asyncio.to_thread(self._run_hardware_mutation, work)

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

    async def consume_automatic_recovery_focus_request(self):
        """Consume one fresh automatic-recovery request for the same live game."""
        def work():
            candidate = None
            try:
                with _exclusive_file_lock(LIFECYCLE_LOCK, timeout=1):
                    try:
                        mode = AUTO_RECOVERY_FOCUS_REQUEST.lstat().st_mode
                    except FileNotFoundError:
                        return None
                    except OSError:
                        return None
                    if not stat.S_ISREG(mode):
                        return None
                    try:
                        candidate = json.loads(
                            AUTO_RECOVERY_FOCUS_REQUEST.read_text())
                    except (OSError, ValueError, json.JSONDecodeError):
                        candidate = None
                    try:
                        AUTO_RECOVERY_FOCUS_REQUEST.unlink()
                    except OSError:
                        return None
            except TimeoutError:
                return None

            valid = (
                isinstance(candidate, dict) and
                set(candidate) == {
                    "version", "boot_id", "appid",
                    "requested_monotonic", "reason"} and
                not isinstance(candidate["version"], bool) and
                candidate["version"] == 1 and
                isinstance(candidate["boot_id"], str) and
                candidate["boot_id"] == _read(
                    "/proc/sys/kernel/random/boot_id") and
                isinstance(candidate["appid"], str) and
                candidate["appid"].isdigit() and
                candidate["appid"] != "0" and
                len(candidate["appid"]) <= 20 and
                not isinstance(candidate["requested_monotonic"], bool) and
                isinstance(candidate["requested_monotonic"], (int, float)) and
                math.isfinite(float(candidate["requested_monotonic"])) and
                candidate["reason"] in {
                    "stale-heartbeat", "owner-dead", "loader-unavailable",
                    "replacement-not-ready"}
            )
            if not valid:
                return None
            age = time.monotonic() - float(
                candidate["requested_monotonic"])
            if age < 0 or age > AUTO_RECOVERY_FOCUS_MAX_AGE_SECONDS:
                return None
            appid = candidate["appid"]
            if self._detect_steam_app() != appid:
                return None
            decky.logger.info(
                f"Automatic recovery requested foreground restore for app {appid}")
            return appid

        return await asyncio.to_thread(work)

    async def report_automatic_recovery_focus_result(self, appid, result):
        """Record the bounded frontend navigation outcome for live diagnosis."""
        allowed = {
            "confirmed",
            "navigation-dispatched",
            "steam-ui-unavailable",
            "selection-failed",
            "navigation-failed",
        }
        if (
            not isinstance(appid, str) or
            not appid.isdigit() or
            appid == "0" or
            len(appid) > 20 or
            not isinstance(result, str) or
            result not in allowed
        ):
            return False
        decky.logger.info(
            f"Automatic recovery game navigation for app {appid}: {result}")
        return True

    async def _game_watch_loop(self):
        pending, confirmations = None, 0
        steam_was_active = False
        while True:
            try:
                install_status = await asyncio.to_thread(self._install_status)
                if install_status["active"]:
                    pending, confirmations = None, 0
                    await asyncio.sleep(2)
                    continue
                conflict = await asyncio.to_thread(
                    self._plugin_conflict_state, True)
                if conflict["blocked"]:
                    pending, confirmations = None, 0
                    await asyncio.sleep(2)
                    continue
                if getattr(self, "startup_rgb_pending", False):
                    try:
                        self._require_mutations_allowed()
                        def reapply_rgb():
                            with self._hardware_mutation_guard():
                                return self.rgb.reapply_startup()
                        startup_rgb_action = await asyncio.to_thread(
                            reapply_rgb)
                        if startup_rgb_action:
                            decky.logger.info(
                                "Applied deferred RGB startup state after installation")
                    except Exception as reason:
                        decky.logger.warning(
                            f"Unable to apply deferred RGB startup state: {reason}")
                    finally:
                        self.startup_rgb_pending = False
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
                elif (detected == self.active_appid and
                      self.runtime_marker.exists()):
                    pending, confirmations = None, 0
                    await asyncio.sleep(2)
                    continue
                elif detected == self.active_appid:
                    # The independent restoration guard may have completed a
                    # genuine Steam exit between watcher samples. An unchanged
                    # AppID is not proof that the RK-E runtime session still
                    # exists, so reapply immediately and capture a fresh native
                    # pre-session baseline through the common activation below.
                    pending, confirmations = detected, 2
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

    @staticmethod
    def _monitor_identity(monitor_session, monitor_generation):
        if (not isinstance(monitor_session, str) or not monitor_session or
                len(monitor_session) > 128):
            raise ValueError("monitor session must be a non-empty token")
        if (isinstance(monitor_generation, bool) or
                not isinstance(monitor_generation, int) or
                not 0 < monitor_generation <= 9_007_199_254_740_991):
            raise ValueError("monitor generation must be a positive safe integer")
        return monitor_session, monitor_generation

    def _monitor_current_locked(self, monitor_session, monitor_generation):
        return bool(
            self.monitor_session == monitor_session and
            self.monitor_generation == monitor_generation)

    def _monitor_epoch_locked(self):
        return {
            "generation": self.monitor_generation,
            "revision": self.monitor_revision,
        }

    def _rke_cpu_snapshot(self):
        """Return cumulative counters for exact RK-E process-tree roots."""
        backend = getattr(self, "backend_identity", None)
        if backend is None:
            backend = _process_identity(os.getpid())
            self.backend_identity = backend
        snapshot = {}
        for identity in (
                backend,
                getattr(self, "lifecycle_guard_identity", None),
                getattr(self, "runtime_guard_identity", None)):
            if not isinstance(identity, dict):
                continue
            try:
                root_key = (
                    int(identity["pid"]), int(identity["start_time_ticks"]))
            except (KeyError, TypeError, ValueError):
                continue
            tree = _process_tree_cpu_snapshot(identity)
            if root_key in tree:
                snapshot[root_key] = sum(tree.values())
        return snapshot

    def _rke_cpu_tracking_available(self):
        """Return whether every expected RK-E root has an exact identity."""
        if not isinstance(getattr(self, "backend_identity", None), dict):
            return False
        for unit_attribute, identity_attribute in (
                ("lifecycle_guard_unit", "lifecycle_guard_identity"),
                ("runtime_guard_unit", "runtime_guard_identity")):
            if (getattr(self, unit_attribute, "") and
                    not isinstance(getattr(self, identity_attribute, None), dict)):
                return False
        return True

    def _sample_rke_cpu_percent(self, monitor_session, monitor_generation):
        """Return top-style combined RK-E CPU load for the Monitor epoch."""
        cpu_snapshot = self._rke_cpu_snapshot()
        sampled_at = time.monotonic()
        with self.monitor_lock:
            if not self._monitor_current_locked(
                    monitor_session, monitor_generation):
                return None
            previous = self.last_rke_cpu_sample
            current = (
                monitor_session, monitor_generation, cpu_snapshot, sampled_at)
            if (previous is None or previous[0] != monitor_session or
                    previous[1] != monitor_generation):
                self.last_rke_cpu_sample = current
                return None
            elapsed = sampled_at - previous[3]
            shared = cpu_snapshot.keys() & previous[2].keys()
            deltas = [
                cpu_snapshot[key] - previous[2][key] for key in shared]
            if elapsed <= 0 or any(delta < 0 for delta in deltas):
                # A process can be reaped between the exact parent/child proc
                # reads. Never install that transiently regressed aggregate as
                # the next baseline or its rebound would become a false spike.
                self.last_rke_cpu_sample = None
                return None
            self.last_rke_cpu_sample = current
            if not shared:
                return None
            cpu_delta = sum(deltas)
        # Match top-style process accounting: 100% is one fully occupied
        # logical core. Do not divide by the number of device CPUs because the
        # purpose of this row is to reveal expensive plugin work.
        return cpu_delta * 100 / elapsed

    def _reset_bypass_estimate_locked(self):
        self.battery_discharge_ema = None
        self.battery_discharge_samples = 0
        self.battery_discharge_last_sample = 0.0

    def _invalidate_current_monitor_locked(self, force=False):
        advance_revision = bool(
            force or self.monitor_charging_valid is not False or
            self.monitor_bypass_active)
        self.monitor_bypass_active = False
        self.monitor_charging_valid = False
        if advance_revision:
            self.monitor_revision += 1
        self._reset_bypass_estimate_locked()

    @staticmethod
    def _status_bypass_active(status):
        battery = status.get("battery") or {}
        return bool(
            status.get("coherent") and battery.get("available") and
            battery.get("valid") and not battery.get("stale") and
            not battery.get("transitional") and battery.get("mode") == "bypass")

    def _update_bypass_estimate(
            self, monitor_session, monitor_generation, monitor_revision,
            bypass_charging, battery_flow_watts, current_ua, charge_uah,
            battery_seconds, battery_estimate_ready):
        with self.monitor_lock:
            if (not self._monitor_current_locked(
                    monitor_session, monitor_generation) or
                    self.monitor_revision != monitor_revision):
                raise RuntimeError("monitor charging state changed during telemetry")
            if bypass_charging and battery_flow_watts <= -0.2:
                discharge_ua = abs(current_ua)
                now = time.monotonic()
                sample_due = (self.battery_discharge_samples < 5 or
                              now - self.battery_discharge_last_sample >= 5)
                if sample_due:
                    self.battery_discharge_ema = (
                        discharge_ua if self.battery_discharge_ema is None else
                        self.battery_discharge_ema * 0.8 + discharge_ua * 0.2)
                    self.battery_discharge_samples += 1
                    self.battery_discharge_last_sample = now
                battery_estimate_ready = (
                    self.battery_discharge_samples >= 5 and charge_uah > 0 and
                    bool(self.battery_discharge_ema))
                battery_seconds = (
                    int(charge_uah * 3600 / self.battery_discharge_ema)
                    if battery_estimate_ready else 0)
            elif not bypass_charging or battery_flow_watts >= 0.2:
                self._reset_bypass_estimate_locked()
            return battery_seconds, battery_estimate_ready

    def _telemetry(self, monitor_session=None, monitor_generation=None):
        monitor_request = (
            monitor_session is not None or monitor_generation is not None)
        if monitor_request:
            monitor_session, monitor_generation = self._monitor_identity(
                monitor_session, monitor_generation)
            with self.monitor_lock:
                if not self._monitor_current_locked(
                        monitor_session, monitor_generation):
                    raise RuntimeError("monitor activation is stale")
                monitor_revision = self.monitor_revision
                bypass_charging = self.monitor_bypass_active
        else:
            monitor_generation = 0
            monitor_revision = 0
            bypass_charging = False
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
        rke_cpu_available = self._rke_cpu_tracking_available()
        rke_cpu_percent = self._sample_rke_cpu_percent(
            monitor_session, monitor_generation
        ) if monitor_request and rke_cpu_available else None
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
        battery_power_available, battery_flow_watts, current_ua = (
            _battery_power(battery))
        battery_watts = abs(battery_flow_watts)
        battery_filling = (battery_status.lower() == "charging" or
                           (bypass_charging and battery_flow_watts >= 0.2))
        battery_seconds = (_read_int(battery / "time_to_full_avg") if battery_filling
                           else _read_int(battery / "time_to_empty_avg"))
        battery_estimate_ready = battery_seconds > 0
        charge_uah = _read_int(battery / "charge_counter")
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
        if monitor_request:
            battery_seconds, battery_estimate_ready = self._update_bypass_estimate(
                monitor_session, monitor_generation, monitor_revision,
                bypass_charging, battery_flow_watts, current_ua, charge_uah,
                battery_seconds, battery_estimate_ready)
        response = {
            "monitor_generation": monitor_generation,
            "charging_revision": monitor_revision,
            "battery_percent": _read_int(battery / "capacity"),
            "battery_status": battery_status,
            "battery_seconds": max(0, battery_seconds),
            "battery_estimate_ready": battery_estimate_ready,
            "battery_power_available": battery_power_available,
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
            "rke_cpu_percent": (
                round(rke_cpu_percent, 1)
                if rke_cpu_percent is not None else None),
            "rke_cpu_available": rke_cpu_available,
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
        if monitor_request:
            with self.monitor_lock:
                if (not self._monitor_current_locked(
                        monitor_session, monitor_generation) or
                        self.monitor_revision != monitor_revision):
                    raise RuntimeError("monitor charging state changed during telemetry")
        return response

    async def begin_monitor_session(self, monitor_session, monitor_generation):
        monitor_session, monitor_generation = self._monitor_identity(
            monitor_session, monitor_generation)
        with self.monitor_lock:
            if self._monitor_current_locked(monitor_session, monitor_generation):
                return self._monitor_epoch_locked()
            if monitor_generation <= self.monitor_generation:
                raise RuntimeError("monitor activation is stale")
            self.monitor_session = monitor_session
            self.monitor_generation = monitor_generation
            self.last_rke_cpu_sample = None
            self._invalidate_current_monitor_locked(force=True)
            # The new activation has not observed a charging status yet. Its
            # first invalid result must invalidate any telemetry that raced it,
            # while repeated invalid results can then reuse that revision.
            self.monitor_charging_valid = None
            return self._monitor_epoch_locked()

    async def end_monitor_session(self, monitor_session, monitor_generation):
        monitor_session, monitor_generation = self._monitor_identity(
            monitor_session, monitor_generation)
        with self.monitor_lock:
            if self._monitor_current_locked(monitor_session, monitor_generation):
                self._invalidate_current_monitor_locked(force=True)
                self.monitor_session = ""
                self.last_rke_cpu_sample = None
            elif monitor_generation > self.monitor_generation:
                # Tombstone an end which overtakes its begin RPC, so that the
                # delayed activation cannot resurrect a hidden Monitor tab.
                self.monitor_generation = monitor_generation
                self.monitor_session = ""
                self.last_rke_cpu_sample = None
                self._invalidate_current_monitor_locked(force=True)
            return self._monitor_epoch_locked()

    async def invalidate_monitor_charging_status(
            self, monitor_session, monitor_generation):
        monitor_session, monitor_generation = self._monitor_identity(
            monitor_session, monitor_generation)
        with self.monitor_lock:
            if not self._monitor_current_locked(
                    monitor_session, monitor_generation):
                raise RuntimeError("monitor activation is stale")
            self._invalidate_current_monitor_locked()
            return self._monitor_epoch_locked()

    async def get_telemetry(self, monitor_session=None, monitor_generation=None):
        return await asyncio.to_thread(
            self._telemetry, monitor_session, monitor_generation)

    async def get_charging_status(
            self, monitor_session=None, monitor_generation=None):
        monitor_request = (
            monitor_session is not None or monitor_generation is not None)
        if monitor_request:
            monitor_session, monitor_generation = self._monitor_identity(
                monitor_session, monitor_generation)
            with self.monitor_lock:
                if not self._monitor_current_locked(
                        monitor_session, monitor_generation):
                    raise RuntimeError("monitor activation is stale")
        try:
            status = await asyncio.to_thread(self.charging.get_status)
        except Exception:
            if monitor_request:
                with self.monitor_lock:
                    if self._monitor_current_locked(
                            monitor_session, monitor_generation):
                        self._invalidate_current_monitor_locked()
            raise
        if monitor_request:
            with self.monitor_lock:
                if not self._monitor_current_locked(
                        monitor_session, monitor_generation):
                    raise RuntimeError("monitor activation changed during charging refresh")
                bypass_active = self._status_bypass_active(status)
                if not status.get("coherent"):
                    self._invalidate_current_monitor_locked()
                else:
                    self.monitor_charging_valid = True
                    if bypass_active != self.monitor_bypass_active:
                        self.monitor_revision += 1
                        self.monitor_bypass_active = bypass_active
                    if not bypass_active:
                        self._reset_bypass_estimate_locked()
                status["monitor_generation"] = self.monitor_generation
                status["charging_revision"] = self.monitor_revision
        issues = []
        for label in ("battery", "pump"):
            component = status.get(label) or {}
            if not component.get("valid"):
                command = component.get("command") or {}
                issues.append(
                    f"{label}: {component.get('refresh_error') or component.get('error') or 'invalid status'} "
                    f"(started={command.get('started', False)} "
                    f"exit={command.get('exit_status')} timeout={command.get('timed_out', False)})")
        warning = "; ".join(issues)
        if warning != self.charging_status_warning:
            if warning:
                decky.logger.warning(f"Charging helper status unavailable: {warning}")
            elif self.charging_status_warning:
                decky.logger.info("Charging helper status recovered")
            self.charging_status_warning = warning
        return status

    async def set_battery_policy(self, mode, limit=None):
        self._require_mutations_allowed()
        def work():
            with self._hardware_mutation_guard():
                if not self._load().get("experimental_unlocked", False):
                    raise RuntimeError(
                        "unlock experimental controls in Utils first")
                return self.charging.set_battery_policy(mode, limit)
        result = await asyncio.to_thread(work)
        with self.monitor_lock:
            if self.monitor_session:
                self._invalidate_current_monitor_locked(force=True)
        operation = result.get("operation") or {}
        decky.logger.info(
            f"Charging policy request {operation.get('requested', mode)}: "
            f"ok={operation.get('ok', False)} timeout={operation.get('timed_out', False)}")
        return result

    async def set_pump_profile(self, profile, experimental_risk_confirmed=False):
        self._require_mutations_allowed()
        def work():
            with self._hardware_mutation_guard():
                if not self._load().get("experimental_unlocked", False):
                    raise RuntimeError(
                        "unlock experimental controls in Utils first")
                return self.charging.set_pump_profile(
                    profile, experimental_risk_confirmed)
        result = await asyncio.to_thread(work)
        operation = result.get("operation") or {}
        decky.logger.info(
            f"Pump profile request {operation.get('requested', profile)}: "
            f"ok={operation.get('ok', False)} timeout={operation.get('timed_out', False)}")
        return result

    async def unlock_experimental(self, code):
        def work():
            if str(code) != "bypasstest":
                raise ValueError("incorrect experimental unlock code")
            data = self._load()
            data["experimental_unlocked"] = True
            self._save(data)
            decky.logger.info("Experimental controls unlocked")
            return self._state()
        return await asyncio.to_thread(self._run_hardware_mutation, work)

    async def lock_experimental(self):
        def work():
            data = self._load()
            data["experimental_unlocked"] = False
            self._save(data)
            decky.logger.info("Experimental controls hidden")
            return self._state()
        return await asyncio.to_thread(self._run_hardware_mutation, work)

    def _backup_root_path(self):
        return Path(getattr(
            self, "backup_root",
            self.settings_dir.parent.parent / "plugin-backups"))

    @staticmethod
    def _backup_tree_size(path):
        """Count logical file bytes without following any symlink."""
        total = 0
        pending = [Path(path)]
        while pending:
            directory = pending.pop()
            for child in directory.iterdir():
                mode = child.lstat().st_mode
                if stat.S_ISREG(mode):
                    total += child.lstat().st_size
                elif stat.S_ISDIR(mode):
                    pending.append(child)
                # Symlinks and special files are intentionally not followed or
                # counted. rmtree removes their directory entries only.
        return total

    @staticmethod
    def _backup_creation_order(path, status):
        marker = Path(path) / ".rke-backup-created.json"
        try:
            marker_status = marker.lstat()
            marker_data = json.loads(marker.read_text())
            transaction_id = marker_data.get("transaction_id")
            created_at = marker_data.get("created_at")
            valid_marker = (
                stat.S_ISREG(marker_status.st_mode) and
                isinstance(marker_data, dict) and
                set(marker_data) == {
                    "protocol", "created_at", "transaction_id"} and
                marker_data.get("protocol") == 1 and
                not isinstance(created_at, bool) and
                isinstance(created_at, int) and created_at > 0 and
                created_at <= int(time.time()) + 300 and
                isinstance(transaction_id, str) and
                re.fullmatch(
                    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", transaction_id)
            )
            if valid_marker:
                return created_at, 2, "marker"
        except (OSError, ValueError, json.JSONDecodeError, AttributeError):
            pass
        match = re.search(
            r"-(\d{8}-\d{6})\.[A-Za-z0-9]+$", Path(path).name)
        if match:
            try:
                created_at = int(time.mktime(time.strptime(
                    match.group(1), "%Y%m%d-%H%M%S")))
                return created_at, 1, "name"
            except (OverflowError, ValueError):
                pass
        return int(status.st_mtime), 0, "mtime"

    def _backup_cleanup_info(self):
        root = self._backup_root_path()
        try:
            root_mode = root.lstat().st_mode
        except FileNotFoundError:
            return {
                "eligible_count": 0,
                "eligible_bytes": 0,
                "kept": None,
                "removable": [],
                "errors": [],
            }
        except OSError as reason:
            return {
                "eligible_count": 0,
                "eligible_bytes": 0,
                "kept": None,
                "removable": [],
                "errors": [str(reason)],
            }
        if not stat.S_ISDIR(root_mode):
            return {
                "eligible_count": 0,
                "eligible_bytes": 0,
                "kept": None,
                "removable": [],
                "errors": ["The RK-Enhanced backup root is not a real directory."],
            }

        candidates = []
        errors = []
        try:
            children = list(root.iterdir())
        except OSError as reason:
            children = []
            errors.append(str(reason))
        for child in children:
            if not re.fullmatch(
                    r"RK-Enhanced-before-[A-Za-z0-9._-]+", child.name):
                continue
            try:
                status = child.lstat()
                if (not stat.S_ISDIR(status.st_mode) or
                        child.parent != root):
                    continue
                size = self._backup_tree_size(child)
                created_at, order_source, created_source = (
                    self._backup_creation_order(child, status))
            except OSError as reason:
                errors.append(f"{child.name}: {reason}")
                continue
            candidates.append({
                "name": child.name,
                "path": str(child),
                "bytes": size,
                "modified_at": int(status.st_mtime),
                "created_at": created_at,
                "created_source": created_source,
                # Prefer the strongest creation evidence before comparing its
                # timestamp. This prevents a skewed legacy mtime/name from
                # making a marker-backed rollback snapshot disposable.
                "_order": (order_source, created_at),
            })
        candidates.sort(
            key=lambda item: (item["_order"], item["name"]),
            reverse=True)
        for item in candidates:
            item.pop("_order", None)
        kept = candidates[0] if candidates else None
        removable = candidates[1:]
        return {
            "eligible_count": len(removable),
            "eligible_bytes": sum(item["bytes"] for item in removable),
            "kept": kept,
            "removable": removable,
            "errors": errors,
        }

    async def get_backup_cleanup_info(self):
        return await asyncio.to_thread(self._backup_cleanup_info)

    async def clean_old_backups(self):
        def work():
            root = self._backup_root_path()
            try:
                with _exclusive_file_lock(INSTALL_TRANSACTION_LOCK, timeout=0):
                    before = self._backup_cleanup_info()
                    if before["errors"]:
                        raise RuntimeError(
                            "Backup cleanup was withheld: " +
                            "; ".join(before["errors"]))
                    removed_count = 0
                    removed_bytes = 0
                    for summary in before["removable"]:
                        path = Path(summary["path"])
                        try:
                            status = path.lstat()
                        except FileNotFoundError:
                            continue
                        if (path.parent != root or
                                not re.fullmatch(
                                    r"RK-Enhanced-before-[A-Za-z0-9._-]+",
                                    path.name) or
                                not stat.S_ISDIR(status.st_mode)):
                            raise RuntimeError(
                                f"Backup changed during cleanup: {path.name}")
                        shutil.rmtree(path)
                        removed_count += 1
                        removed_bytes += int(summary["bytes"])
                    after = self._backup_cleanup_info()
            except TimeoutError as reason:
                raise RuntimeError(
                    "An RK-Enhanced installation is already in progress; "
                    "backup cleanup was not started.") from reason
            decky.logger.info(
                "Cleaned old RK-Enhanced backups: "
                f"removed={removed_count} bytes={removed_bytes} "
                f"kept={(after['kept'] or {}).get('name', 'none')}")
            return {
                **after,
                "removed_count": removed_count,
                "removed_bytes": removed_bytes,
            }
        return await asyncio.to_thread(work)

    def _log_text(self):
        configured = os.environ.get("DECKY_PLUGIN_LOG_DIR")
        if configured:
            log_dir = Path(configured)
        else:
            log_dir = self.settings_dir.parent.parent / "logs" / "RK-Enhanced"
        try:
            logs = sorted(
                log_dir.glob("*.log"), key=lambda path: path.stat().st_mtime)
        except OSError:
            logs = []
        if not logs:
            return "No RK-Enhanced log file found."
        entries = []
        errors = []
        timestamp_pattern = re.compile(
            r"^\[?(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})"
            r"(?:,\d+)?\]?")
        for log_path in logs:
            try:
                lines = log_path.read_text(errors="replace").splitlines()
                modified = log_path.stat().st_mtime
            except OSError as reason:
                errors.append(f"Could not read {log_path.name}: {reason}")
                continue
            offset = self.log_offsets.get(str(log_path), 0)
            if offset > len(lines):
                offset = 0
            visible = lines[offset:]
            for index, line in enumerate(visible):
                match = timestamp_pattern.match(line)
                timestamp = modified + index / max(1, len(visible))
                display = line
                if match:
                    try:
                        timestamp = time.mktime(time.strptime(
                            match.group(1).replace("T", " "),
                            "%Y-%m-%d %H:%M:%S"))
                    except ValueError:
                        pass
                    if not line.startswith("["):
                        display = (
                            "[" + match.group(1).replace("T", " ") + "]" +
                            line[match.end():])
                entries.append((timestamp, str(log_path), index, display))
        entries.sort(key=lambda item: item[:3])
        merged = [entry[3] for entry in entries[-400:]]
        if errors:
            merged.extend(errors)
        return "\n".join(merged) if merged else "Log is empty."

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
            last_installed = _read(
                self.settings_dir / LAST_INSTALLED_VERSION_FILE)
            if (len(last_installed) > 64 or any(
                    character not in
                    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                    for character in last_installed)):
                last_installed = ""
            last_installed_available = bool(
                last_installed and last_installed != installed and
                last_installed in releases)
            if not last_installed_available:
                last_installed = ""
            return {"installed": installed, "latest": latest,
                    "update_available": bool(latest and installed != latest),
                    # `previous` remains the previous published GitHub release,
                    # regardless of this device's actual upgrade path.
                    "previous": previous,
                    "previous_published": previous,
                    "last_installed": last_installed,
                    "last_installed_available": last_installed_available,
                    "error": error}
        return await asyncio.to_thread(work)

    async def install_release(self, version, remove_conflict=False):
        def work():
            requested = str(version).strip()
            if self._install_status()["active"]:
                raise RuntimeError(
                    "An RK-Enhanced installation is already in progress.")
            if not isinstance(remove_conflict, bool):
                raise ValueError("remove_conflict must be a boolean")
            if (not requested or len(requested) > 64 or
                    any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                        for character in requested)):
                raise ValueError("invalid release version")
            conflict = self._plugin_conflict_state(record_transition=True)
            if conflict["scan_error"]:
                raise RuntimeError(conflict["message"])
            if conflict["conflicts"] and not remove_conflict:
                raise RuntimeError(
                    "ROCKNIX Control must be removed before installing a release.")
            if (remove_conflict and any(
                    not item.get("removable")
                    for item in conflict["conflicts"])):
                raise RuntimeError(
                    "A conflicting plugin path cannot be removed automatically.")
            source = Path(__file__).resolve().parent / "updater.sh"
            if not source.exists():
                raise RuntimeError("updater.sh is missing from this RK-Enhanced installation")
            target = Path("/storage/homebrew/rk-enhanced-updater.sh")
            _atomic_executable_copy(source, target)
            _atomic_text(self.settings_dir / UPDATE_STATUS_FILE,
                         f"Starting installation of {requested}…\n")
            _run(["systemctl", "reset-failed", "rk-enhanced-update.service"], check=False)
            command = [
                "systemd-run", "--unit=rk-enhanced-update", "--collect",
                str(target), requested, "require-frontend",
            ]
            if remove_conflict:
                command.append("remove-conflicting-rocknix-control")
            _run(command)
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
        # Establish independent lifecycle protection before settings migration
        # or runtime restoration. Those operations can legitimately take
        # longer than a replacement handoff window, but the new backend can
        # identify itself immediately.
        try:
            await asyncio.to_thread(self._start_plugin_lifecycle_guard)
            self.lifecycle_heartbeat_task = asyncio.create_task(
                self._lifecycle_heartbeat_loop())
        except Exception as reason:
            # Recovery protection must be visible in the log, but an optional
            # guard setup failure must not make Decky reject the whole plugin.
            decky.logger.error(
                f"Unable to start PluginLoader lifecycle guard: {reason}")

        def initialise():
            conflict = self._plugin_conflict_state(record_transition=True)
            self._load()
            # A prior RK-E owner may have died after applying a runtime
            # session. Restore that exact owned baseline once before yielding;
            # this is crash cleanup, not a new preset application. All new
            # hardware ownership (including startup RGB reapply) remains
            # blocked below while the legacy plugin is installed.
            if self.runtime_marker.exists():
                self._restore_runtime_session()
            if self.legacy_fan_guard_marker.exists():
                if conflict["blocked"]:
                    # This older marker restoration unconditionally rewrites
                    # fancontrol.conf and may restart native fancontrol. Defer
                    # it until the conflicting controller has been removed;
                    # unlike the ownership-aware runtime session above, it
                    # cannot prove that its old values are still RK-E-owned.
                    decky.logger.warning(
                        "Deferred legacy fan-curve restoration until the "
                        "ROCKNIX Control conflict is removed")
                else:
                    self._restore_legacy_system_fan_curve()
            if conflict["blocked"]:
                decky.logger.warning(
                    "Skipped startup RGB and preset ownership because "
                    "ROCKNIX Control conflicts with RK-Enhanced")
                return
            if self._install_status()["active"]:
                self.startup_rgb_pending = True
                decky.logger.info(
                    "Deferred RGB startup state until the active installation completes")
                return
            try:
                self._require_mutations_allowed()
                with self._hardware_mutation_guard():
                    startup_rgb_action = self.rgb.reapply_startup()
                if startup_rgb_action == "calibration":
                    decky.logger.info(
                        "Reapplied the saved native RGB calibration")
                elif startup_rgb_action == ODIN3_RGB_STARTUP_ACTION:
                    decky.logger.info(
                        "Restored the saved Odin 3 RGB state for this boot")
                elif startup_rgb_action:
                    decky.logger.info("Reapplied the saved native RGB animation")
            except Exception as reason:
                # RGB support is optional and must never prevent the plugin
                # from loading on unsupported or partially configured devices.
                decky.logger.warning(
                    f"Unable to apply saved RGB startup state: {reason}")
        try:
            await asyncio.to_thread(initialise)
        except Exception:
            # Do not keep advertising a healthy backend if plugin
            # initialisation itself failed. Leaving the active lease in place
            # lets the independent guard perform one cooldown-bounded recovery.
            if self.lifecycle_heartbeat_task is not None:
                self.lifecycle_heartbeat_task.cancel()
                try:
                    await self.lifecycle_heartbeat_task
                except asyncio.CancelledError:
                    pass
                self.lifecycle_heartbeat_task = None
            raise
        self.game_watch_task = asyncio.create_task(self._game_watch_loop())
        decky.logger.info("RK-Enhanced backend ready")
        # A stale request can survive an interrupted install or a reboot. It
        # must withhold readiness from that installer, never prevent an
        # otherwise valid plugin generation from starting normally.
        try:
            await asyncio.to_thread(self._publish_backend_install_health)
        except Exception:
            decky.logger.exception(
                "Install health response was withheld; plugin startup continues")

    async def _unload(self):
        # Mark this generation clean before doing any other unload work. The
        # independent guard must never turn an intentional Decky stop into an
        # automatic restart, even if a later cleanup step is slow.
        self._mark_lifecycle_guard_clean()
        if self.lifecycle_heartbeat_task is not None:
            self.lifecycle_heartbeat_task.cancel()
            try:
                await self.lifecycle_heartbeat_task
            except asyncio.CancelledError:
                pass
            self.lifecycle_heartbeat_task = None
        if self.game_watch_task is not None:
            self.game_watch_task.cancel()
            try:
                await self.game_watch_task
            except asyncio.CancelledError:
                pass
            self.game_watch_task = None
        restore_handoff = ""
        try:
            restore_handoff = await asyncio.to_thread(
                self._request_detached_runtime_restore)
        except Exception as reason:
            # A runtime guard already exists for every active session. If the
            # faster clean-unload handoff fails, owner death still triggers
            # that guard without keeping this Decky worker alive.
            decky.logger.warning(
                f"Detached runtime restoration handoff failed: {reason}")
        decky.logger.info(
            "RK-Enhanced unloaded; runtime restoration handed to a detached unit"
            if restore_handoff == "detached"
            else "RK-Enhanced unloaded; runtime restoration requested from the session guard"
            if restore_handoff == "guard"
            else "RK-Enhanced unloaded; no runtime session required restoration")
