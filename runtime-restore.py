#!/usr/bin/python3
"""Restore native runtime controls after an RK-Enhanced session."""

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


def read(path, default=""):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def read_int(path, default=0):
    try:
        return int(read(path))
    except (TypeError, ValueError):
        return default


def active_charge_behaviour(path):
    value = read(path)
    for option in value.split():
        if option.startswith("[") and option.endswith("]"):
            return option[1:-1]
    return value


def clean_environment():
    environment = os.environ.copy()
    for variable in ("LD_LIBRARY_PATH", "LD_PRELOAD"):
        original = environment.pop(f"{variable}_ORIG", None)
        if original:
            environment[variable] = original
        else:
            environment.pop(variable, None)
    return environment


def service_active(unit):
    process = subprocess.run(
        ["systemctl", "is-active", "--quiet", unit], check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=10, env=clean_environment())
    return process.returncode == 0


def set_service_active(unit, active):
    subprocess.run(
        ["systemctl", "start" if active else "stop", unit], check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        timeout=15, env=clean_environment())


def write_range(path, low, high, low_name, high_name):
    current_low = read_int(path / low_name)
    current_high = read_int(path / high_name)
    if high < current_low:
        (path / low_name).write_text(str(low))
        (path / high_name).write_text(str(high))
    elif low > current_high:
        (path / high_name).write_text(str(high))
        (path / low_name).write_text(str(low))
    else:
        (path / low_name).write_text(str(low))
        (path / high_name).write_text(str(high))


def restore_range(control, low_name, high_name, changes):
    path = Path(control["path"])
    baseline = control.get("baseline", {})
    applied = control.get("applied", {})
    if not path.exists() or not applied:
        return
    current_low = read_int(path / low_name)
    current_high = read_int(path / high_name)
    restore_low = current_low == applied.get("minimum")
    restore_high = current_high == applied.get("maximum")
    target_low = int(baseline["minimum"]) if restore_low else current_low
    target_high = int(baseline["maximum"]) if restore_high else current_high
    if (restore_low or restore_high) and target_low <= target_high:
        write_range(path, target_low, target_high, low_name, high_name)
        if restore_low:
            changes.append(f"{path.name} minimum")
        if restore_high:
            changes.append(f"{path.name} maximum")


def restore_governor(control, filename, changes):
    path = Path(control["path"])
    baseline = control.get("baseline", {})
    applied = control.get("applied", {})
    target = path / filename
    if (target.exists() and applied.get("governor") is not None and
            read(target) == str(applied["governor"])):
        target.write_text(str(baseline["governor"]))
        changes.append(f"{path.name} governor")


def fancontrol_main_pid(exclude=None):
    cgroups = (
        "/sys/fs/cgroup/systemd/system.slice/fancontrol.service/cgroup.procs",
        "/sys/fs/cgroup/pids/system.slice/fancontrol.service/cgroup.procs",
        "/sys/fs/cgroup/unified/system.slice/fancontrol.service/cgroup.procs",
        "/sys/fs/cgroup/system.slice/fancontrol.service/cgroup.procs",
    )
    for cgroup in cgroups:
        for value in read(cgroup).splitlines():
            try:
                pid = int(value)
            except ValueError:
                continue
            if pid != exclude and read(Path("/proc") / value / "comm") == "fancontrol":
                return pid
    return None


def reload_fancontrol():
    previous = fancontrol_main_pid()
    if previous is None:
        subprocess.run(
            ["systemctl", "start", "fancontrol.service"], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            timeout=15, env=clean_environment())
        return
    os.kill(previous, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if fancontrol_main_pid(exclude=previous) is not None:
            return
        time.sleep(0.1)
    raise RuntimeError("native fancontrol did not restart within 5 seconds")


def main():
    if len(sys.argv) != 5:
        raise SystemExit(
            "usage: runtime-restore.py MARKER STATE CANONICAL_FAN TARGET_FAN")
    marker, state_path, canonical_fan, target_fan = map(Path, sys.argv[1:])
    lock_path = state_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    changes, errors = [], []
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if not marker.exists():
            return 0
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError, json.JSONDecodeError) as reason:
            print(f"Could not read runtime session: {reason}", file=sys.stderr)
            return 1

        controls = state.get("controls", {})
        fan = controls.get("fan", {})
        if fan.get("applied"):
            try:
                if not canonical_fan.is_file():
                    raise RuntimeError("protected ROCKNIX Custom curve is missing")
                shutil.copy2(canonical_fan, target_fan)
                changes.append("ROCKNIX Custom fan curve")
                get_setting = shutil.which("get_setting")
                cooling = ""
                if get_setting:
                    result = subprocess.run(
                        [get_setting, "cooling.profile"], check=False, text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        timeout=10, env=clean_environment())
                    cooling = result.stdout.strip()
                if cooling == "custom":
                    reload_fancontrol()
            except Exception as reason:
                errors.append(f"fan: {reason}")

        current_boot = read("/proc/sys/kernel/random/boot_id")
        same_boot = not state.get("boot_id") or state.get("boot_id") == current_boot
        if same_boot:
            for control in controls.get("cpu", []):
                try:
                    restore_range(
                        control, "scaling_min_freq", "scaling_max_freq", changes)
                    restore_governor(control, "scaling_governor", changes)
                except Exception as reason:
                    errors.append(f"CPU {control.get('id', '?')}: {reason}")
            gpu = controls.get("gpu")
            if gpu:
                try:
                    restore_range(gpu, "min_freq", "max_freq", changes)
                    restore_governor(gpu, "governor", changes)
                except Exception as reason:
                    errors.append(f"GPU: {reason}")
            scheduler = controls.get("scheduler")
            if scheduler and scheduler.get("applied") is not None:
                try:
                    current = service_active(scheduler["unit"])
                    if current == bool(scheduler["applied"]):
                        baseline = bool(scheduler["baseline"])
                        if current != baseline:
                            set_service_active(scheduler["unit"], baseline)
                            changes.append("CPU scheduler")
                except Exception as reason:
                    errors.append(f"scheduler: {reason}")
            charging = controls.get("charging")
            if charging and charging.get("applied") is not None:
                try:
                    path = Path(charging["path"])
                    if (path.exists() and active_charge_behaviour(path) ==
                            charging["applied"]):
                        path.write_text(str(charging["baseline"]))
                        changes.append("charging behaviour")
                except Exception as reason:
                    errors.append(f"charging: {reason}")
        else:
            changes.append("runtime controls skipped after reboot")

        if errors:
            print("Runtime restoration incomplete: " + "; ".join(errors),
                  file=sys.stderr)
            return 1
        marker.unlink(missing_ok=True)
        state_path.unlink(missing_ok=True)
        report = "Restored: " + (", ".join(changes) if changes else "no owned values changed")
        (state_path.parent / "runtime-restore-last.txt").write_text(report + "\n")
        print(report)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
