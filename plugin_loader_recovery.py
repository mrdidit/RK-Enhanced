#!/usr/bin/python3
"""Bounded, identity-safe recovery for ROCKNIX's Decky PluginLoader.

This helper is intended to run in a transient systemd service outside
``plugin_loader.service``.  It deliberately has no configurable service or
binary arguments: a caller cannot turn the root helper into a generic process
killer.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import argparse
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable, Iterable


SERVICE = "plugin_loader.service"
PLUGIN_LOADER_BINARY = Path("/storage/homebrew/services/PluginLoader")
CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC_ROOT = Path("/proc")
LOCK_PATH = Path("/run/lock/rk-enhanced-plugin-loader-recovery.lock")
MARKER_PATH = Path("/run/rk-enhanced-plugin-loader-recovery.active")
LIFECYCLE_ROOT = Path("/run/rk-enhanced")
CURRENT_LEASE_PATH = LIFECYCLE_ROOT / "plugin-lifecycle-current.json"
AUTO_RECOVERY_PATH = LIFECYCLE_ROOT / "plugin-loader-auto-recovery.json"
AUTO_FOCUS_REQUEST_PATH = (
    LIFECYCLE_ROOT / "automatic-recovery-focus.json")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
STEAM_SCOPE = "steam-bigpicture.scope"

GRACEFUL_STOP_SECONDS = 10.0
TERM_SECONDS = 3.0
KILL_SECONDS = 3.0
START_SECONDS = 15.0
POLL_SECONDS = 0.1
GUARD_POLL_SECONDS = 2.0
HEARTBEAT_STALE_SECONDS = 15.0
HEARTBEAT_RECHECK_SECONDS = 6.0
REPLACEMENT_WAIT_SECONDS = 20.0
ACTIVATION_WAIT_SECONDS = 10.0
OLD_OWNER_GRACE_SECONDS = 10.0
AUTO_RECOVERY_COOLDOWN_SECONDS = 120.0

LEASE_VERSION = 1
TOKEN_PATTERN = re.compile(r"[0-9a-f]{32}")

STEAM_SCOPE_CGROUPS = (
    Path("systemd/system.slice/steam-bigpicture.scope/cgroup.procs"),
    Path("unified/system.slice/steam-bigpicture.scope/cgroup.procs"),
    Path("pids/system.slice/steam-bigpicture.scope/cgroup.procs"),
)
STEAM_GAME_IGNORED_PROCESSES = (
    "pw-audio-namesp", "network.cr", "steamwebhelper", "pressure-vessel",
    "reaper", "wineserver", "services.exe", "explorer.exe", "rpcss.exe",
    "plugplay.exe", "svchost.exe", "conhost.exe",
)

TRUSTED_ENVIRONMENT = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C",
    "LC_ALL": "C",
}


class RecoveryError(RuntimeError):
    """A recovery safety check or bounded operation failed."""


@dataclass(frozen=True)
class ProcessIdentity:
    """The reusable PID plus the non-reusable identity of that process."""

    pid: int
    start_time_ticks: int
    parent_pid: int


@dataclass(frozen=True)
class LoaderSnapshot:
    main: ProcessIdentity | None
    control_group: str
    processes: tuple[ProcessIdentity, ...]


class SteamGameDetector:
    """Read the active Steam AppID without invoking Steam or gamescope."""

    def __init__(self, proc_root: Path = PROC_ROOT,
                 cgroup_root: Path = CGROUP_ROOT):
        self.proc_root = Path(proc_root)
        self.cgroup_root = Path(cgroup_root)

    def _scope_pids(self):
        for relative in STEAM_SCOPE_CGROUPS:
            path = self.cgroup_root / relative
            try:
                values = path.read_text().splitlines()
            except FileNotFoundError:
                continue
            except OSError:
                return ()
            result = []
            for value in values:
                try:
                    pid = int(value.strip())
                except ValueError:
                    continue
                if pid > 0:
                    result.append(pid)
            return tuple(result)
        return ()

    def active_appid(self) -> str:
        candidates: dict[str, int] = {}
        for pid in self._scope_pids():
            process = self.proc_root / str(pid)
            try:
                comm = (process / "comm").read_text().strip().lower()
                if (not comm or any(
                        comm.startswith(name)
                        for name in STEAM_GAME_IGNORED_PROCESSES)):
                    continue
                environment = (process / "environ").read_bytes().split(b"\0")
            except OSError:
                continue
            steam_appid = ""
            steam_gameid = ""
            for variable in environment:
                if variable.startswith(b"SteamAppId="):
                    appid = variable.split(b"=", 1)[1].decode(errors="ignore")
                    if (appid.isdigit() and appid != "0" and
                            len(appid) <= 10 and int(appid) <= 0xFFFFFFFF):
                        steam_appid = appid
                elif variable.startswith(b"SteamGameId="):
                    appid = variable.split(b"=", 1)[1].decode(errors="ignore")
                    if (appid.isdigit() and appid != "0" and
                            len(appid) <= 10 and int(appid) <= 0xFFFFFFFF):
                        steam_gameid = appid
                else:
                    continue
            appid = steam_appid or steam_gameid
            if appid:
                candidates[appid] = max(pid, candidates.get(appid, 0))
        if not candidates:
            return ""
        return max(candidates, key=candidates.get)


@dataclass(frozen=True)
class LifecycleLease:
    version: int
    token: str
    boot_id: str
    owner: ProcessIdentity
    loader: ProcessIdentity
    path: Path
    runtime_root: Path

    @property
    def active_path(self):
        return self.runtime_root / f"plugin-lifecycle-{self.token}.active"

    @property
    def heartbeat_path(self):
        return self.runtime_root / f"plugin-lifecycle-{self.token}.heartbeat"

    @property
    def ready_path(self):
        return self.runtime_root / f"plugin-lifecycle-{self.token}.ready"


class Systemd:
    """Small fixed-unit systemd boundary; no shell is ever involved."""

    def __init__(self, run: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self._run = run

    def _command(self, arguments: list[str], *, check: bool = True):
        try:
            result = self._run(
                ["systemctl", *arguments],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=5,
                env=dict(TRUSTED_ENVIRONMENT),
            )
        except (OSError, subprocess.SubprocessError) as reason:
            raise RecoveryError(
                f"systemctl {arguments[0]} could not run: {reason}") from reason
        if check and result.returncode:
            reason = result.stderr.strip() or result.stdout.strip()
            raise RecoveryError(reason or f"systemctl {arguments[0]} failed")
        return result

    def main_pid(self) -> int:
        result = self._command(
            ["show", "--property=MainPID", "--value", SERVICE])
        try:
            value = int(result.stdout.strip() or "0")
        except ValueError as reason:
            raise RecoveryError("PluginLoader returned an invalid MainPID") from reason
        return value if value > 0 else 0

    def control_group(self) -> str:
        result = self._command(
            ["show", "--property=ControlGroup", "--value", SERVICE])
        return result.stdout.strip()

    def active(self) -> bool:
        return self._command(
            ["is-active", "--quiet", SERVICE], check=False).returncode == 0

    def steam_active(self) -> bool:
        return self._command(
            ["is-active", "--quiet", STEAM_SCOPE], check=False).returncode == 0

    def stop_no_block(self):
        self._command(["stop", "--no-block", SERVICE])

    def reset_failed(self):
        self._command(["reset-failed", SERVICE], check=False)

    def start_no_block(self):
        self._command(["start", "--no-block", SERVICE])


class ProcessTable:
    """Read and signal processes while defending against PID reuse."""

    FEX_NAMES = frozenset(("FEX", "FEXInterpreter", "FEXLoader"))

    def __init__(
            self, proc_root: Path = PROC_ROOT,
            send_signal: Callable[[int, int], None] = os.kill):
        self.proc_root = Path(proc_root)
        self.send_signal = send_signal

    def identity(self, pid: int) -> ProcessIdentity | None:
        if pid <= 0:
            return None
        try:
            raw = (self.proc_root / str(pid) / "stat").read_text()
            close = raw.rfind(")")
            if close < 0:
                return None
            # The tail begins with field 3 (state).  PPID is field 4 and the
            # process start time is field 22.
            fields = raw[close + 2:].split()
            return ProcessIdentity(
                pid=pid,
                start_time_ticks=int(fields[19]),
                parent_pid=int(fields[1]),
            )
        except (OSError, IndexError, ValueError):
            return None

    def same_process(self, identity: ProcessIdentity) -> bool:
        current = self.identity(identity.pid)
        return bool(
            current and current.start_time_ticks == identity.start_time_ticks)

    def all_identities(self) -> dict[int, ProcessIdentity]:
        result = {}
        try:
            entries = tuple(self.proc_root.iterdir())
        except OSError:
            return result
        for entry in entries:
            if not entry.name.isdigit():
                continue
            identity = self.identity(int(entry.name))
            if identity is not None:
                result[identity.pid] = identity
        return result

    def matches_loader_binary(self, pid: int) -> bool:
        expected = str(PLUGIN_LOADER_BINARY)
        process = self.proc_root / str(pid)
        try:
            executable = os.readlink(process / "exe")
        except OSError:
            executable = ""
        if executable == expected:
            return True
        try:
            arguments = tuple(
                value.decode(errors="surrogateescape")
                for value in (process / "cmdline").read_bytes().split(b"\0")
                if value
            )
        except OSError:
            return False
        if arguments and arguments[0] == expected:
            return True
        # On ARM ROCKNIX, binfmt/FEX can remain the visible executable while
        # the exact Decky binary is its first argument.  Accept only those
        # known interpreters and only the fixed absolute target.
        return bool(
            Path(executable).name in self.FEX_NAMES and
            len(arguments) > 1 and arguments[1] == expected)

    def loader_roots(self) -> set[int]:
        return {
            pid for pid in self.all_identities()
            if self.matches_loader_binary(pid)
        }

    @staticmethod
    def descendants(
            identities: dict[int, ProcessIdentity], roots: Iterable[int]
    ) -> set[int]:
        selected = {pid for pid in roots if pid in identities}
        changed = True
        while changed:
            changed = False
            for pid, identity in identities.items():
                if pid not in selected and identity.parent_pid in selected:
                    selected.add(pid)
                    changed = True
        return selected

    def signal_if_same(self, identity: ProcessIdentity, requested: int) -> bool:
        if not self.same_process(identity):
            return False
        try:
            self.send_signal(identity.pid, requested)
        except ProcessLookupError:
            return False
        except PermissionError as reason:
            raise RecoveryError(
                f"permission denied signalling PID {identity.pid}") from reason
        return True


class RecoveryController:
    def __init__(
            self, *, systemd: Systemd | None = None,
            processes: ProcessTable | None = None,
            cgroup_root: Path = CGROUP_ROOT,
            lock_path: Path = LOCK_PATH,
            marker_path: Path = MARKER_PATH,
            monotonic: Callable[[], float] = time.monotonic,
            sleep: Callable[[float], None] = time.sleep,
            current_pid: Callable[[], int] = os.getpid):
        self.systemd = systemd or Systemd()
        self.processes = processes or ProcessTable()
        self.cgroup_root = Path(cgroup_root)
        self.lock_path = Path(lock_path)
        self.marker_path = Path(marker_path)
        self.monotonic = monotonic
        self.sleep = sleep
        self.current_pid = current_pid

    @staticmethod
    def _safe_control_group(value: str) -> str:
        value = str(value or "").strip()
        path = PurePosixPath(value)
        if (not value.startswith("/") or value == "/" or
                ".." in path.parts or path.name != SERVICE):
            raise RecoveryError("PluginLoader has an unsafe or missing control group")
        return value

    def _cgroup_directories(self, control_group: str) -> tuple[Path, ...]:
        relative = control_group.lstrip("/")
        candidates = (
            self.cgroup_root / relative,
            self.cgroup_root / "systemd" / relative,
            self.cgroup_root / "pids" / relative,
            self.cgroup_root / "unified" / relative,
        )
        unique = []
        for candidate in candidates:
            if candidate not in unique and candidate.is_dir():
                unique.append(candidate)
        return tuple(unique)

    def _cgroup_pids(self, control_group: str) -> set[int]:
        result = set()
        for directory in self._cgroup_directories(control_group):
            try:
                files = tuple(directory.rglob("cgroup.procs"))
            except OSError:
                continue
            for path in files:
                try:
                    values = path.read_text().splitlines()
                except OSError:
                    continue
                for value in values:
                    try:
                        pid = int(value.strip())
                    except ValueError:
                        continue
                    if pid > 0:
                        result.add(pid)
        return result

    def snapshot(self) -> LoaderSnapshot:
        control_group = self._safe_control_group(
            self.systemd.control_group())
        main_pid = self.systemd.main_pid()
        identities = self.processes.all_identities()
        cgroup_pids = self._cgroup_pids(control_group)
        main = identities.get(main_pid) if main_pid else None
        if main_pid:
            if main is None or main_pid not in cgroup_pids:
                raise RecoveryError("PluginLoader MainPID is not in its control group")
            if not self.processes.matches_loader_binary(main_pid):
                raise RecoveryError("PluginLoader MainPID does not match the fixed binary")

        # Include the recursively snapshotted unit cgroup and descendants of
        # every exact PluginLoader binary.  This catches a still-parented FEX
        # worker even if it has already moved outside the service cgroup.
        roots = self.processes.loader_roots()
        if main_pid:
            roots.add(main_pid)
        selected = cgroup_pids | self.processes.descendants(identities, roots)
        captured = tuple(
            sorted(
                (identities[pid] for pid in selected if pid in identities),
                key=lambda identity: identity.pid,
            )
        )
        if self.current_pid() in {identity.pid for identity in captured}:
            raise RecoveryError(
                "recovery helper must run outside plugin_loader.service")
        return LoaderSnapshot(main, control_group, captured)

    def _current(self, identities: Iterable[ProcessIdentity]):
        return tuple(
            identity for identity in identities
            if self.processes.same_process(identity)
        )

    def _wait_gone(
            self, identities: Iterable[ProcessIdentity], seconds: float
    ) -> tuple[ProcessIdentity, ...]:
        identities = tuple(identities)
        deadline = self.monotonic() + seconds
        while True:
            remaining = self._current(identities)
            if not remaining or self.monotonic() >= deadline:
                return remaining
            self.sleep(min(POLL_SECONDS, max(0.0, deadline - self.monotonic())))

    @staticmethod
    def _signal_order(identities: Iterable[ProcessIdentity]):
        identities = tuple(identities)
        by_pid = {identity.pid: identity for identity in identities}

        def depth(identity):
            result, parent, seen = 0, identity.parent_pid, set()
            while parent in by_pid and parent not in seen:
                seen.add(parent)
                result += 1
                parent = by_pid[parent].parent_pid
            return result

        # Children first, then the loader roots.  PID is only a stable tie
        # breaker; identity is always revalidated immediately before a signal.
        return tuple(sorted(
            identities, key=lambda item: (depth(item), item.pid), reverse=True))

    def _signal(self, identities: Iterable[ProcessIdentity], requested: int):
        signalled = []
        for identity in self._signal_order(identities):
            if self.processes.signal_if_same(identity, requested):
                signalled.append(identity.pid)
        return signalled

    def _assert_no_replacement(self, old_main: ProcessIdentity | None):
        pid = self.systemd.main_pid()
        if not pid:
            return
        current = self.processes.identity(pid)
        if (old_main and current and current.pid == old_main.pid and
                current.start_time_ticks == old_main.start_time_ticks):
            return
        raise RecoveryError(
            "a new PluginLoader generation appeared during cleanup")

    def _verify_started(
            self, old_main: ProcessIdentity | None) -> ProcessIdentity:
        deadline = self.monotonic() + START_SECONDS
        while self.monotonic() < deadline:
            pid = self.systemd.main_pid()
            current = self.processes.identity(pid) if pid else None
            new_pid = bool(current and (old_main is None or pid != old_main.pid))
            if (new_pid and self.systemd.active() and
                    self.processes.matches_loader_binary(pid)):
                control_group = self._safe_control_group(
                    self.systemd.control_group())
                if pid in self._cgroup_pids(control_group):
                    return current
            self.sleep(min(POLL_SECONDS, max(
                0.0, deadline - self.monotonic())))
        raise RecoveryError("PluginLoader did not start with a new verified MainPID")

    def _write_marker(self, payload):
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
                "w", dir=self.marker_path.parent, delete=False) as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.chmod(0o600)
        temporary.replace(self.marker_path)

    @contextmanager
    def _maintenance(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as reason:
                raise RecoveryError(
                    "another PluginLoader maintenance action is running") from reason
            self._write_marker({
                "pid": self.current_pid(),
                "service": SERVICE,
                "binary": str(PLUGIN_LOADER_BINARY),
                "started_at": int(time.time()),
            })
            try:
                yield
            finally:
                self.marker_path.unlink(missing_ok=True)
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def restart(self):
        with self._maintenance():
            snapshot = self.snapshot()
            self._write_marker({
                "pid": self.current_pid(),
                "service": SERVICE,
                "binary": str(PLUGIN_LOADER_BINARY),
                "old_main_pid": snapshot.main.pid if snapshot.main else 0,
                "captured_pids": [item.pid for item in snapshot.processes],
                "started_at": int(time.time()),
            })

            self.systemd.stop_no_block()
            remaining = self._wait_gone(
                snapshot.processes, GRACEFUL_STOP_SECONDS)
            term_pids = []
            kill_pids = []
            if remaining:
                self._assert_no_replacement(snapshot.main)
                term_pids = self._signal(remaining, signal.SIGTERM)
                remaining = self._wait_gone(remaining, TERM_SECONDS)
            if remaining:
                self._assert_no_replacement(snapshot.main)
                kill_pids = self._signal(remaining, signal.SIGKILL)
                remaining = self._wait_gone(remaining, KILL_SECONDS)
            if remaining:
                raise RecoveryError(
                    "captured PluginLoader processes survived SIGKILL: " +
                    ", ".join(str(item.pid) for item in remaining))

            self.systemd.reset_failed()
            self.systemd.start_no_block()
            started = self._verify_started(snapshot.main)
            return {
                "service": SERVICE,
                "old_main_pid": snapshot.main.pid if snapshot.main else 0,
                "captured_pids": [item.pid for item in snapshot.processes],
                "term_pids": term_pids,
                "kill_pids": kill_pids,
                "new_main_pid": started.pid,
                "active": True,
            }


class LifecycleGuard:
    """Watch one immutable RKE backend generation from outside Decky.

    The plugin creates the immutable lease, active marker and first heartbeat
    before starting this guard.  The guard validates that state and publishes
    its derived ready marker; only then does the plugin publish the shared
    current lease under the common lock.  Clean unload removes the active
    marker.  The guard never accepts a caller-selected PID, service, binary or
    auxiliary path; every path is derived from the validated lease token.
    """

    def __init__(
            self, *, recovery: RecoveryController | None = None,
            systemd: Systemd | None = None,
            processes: ProcessTable | None = None,
            runtime_root: Path = LIFECYCLE_ROOT,
            boot_id_path: Path = BOOT_ID_PATH,
            maintenance_marker: Path = MARKER_PATH,
            lock_path: Path = LOCK_PATH,
            monotonic: Callable[[], float] = time.monotonic,
            monotonic_ns: Callable[[], int] = time.monotonic_ns,
            sleep: Callable[[float], None] = time.sleep,
            poll_seconds: float = GUARD_POLL_SECONDS,
            heartbeat_stale_seconds: float = HEARTBEAT_STALE_SECONDS,
            heartbeat_recheck_seconds: float = HEARTBEAT_RECHECK_SECONDS,
            replacement_wait_seconds: float = REPLACEMENT_WAIT_SECONDS,
            activation_wait_seconds: float = ACTIVATION_WAIT_SECONDS,
            owner_grace_seconds: float = OLD_OWNER_GRACE_SECONDS,
            term_seconds: float = TERM_SECONDS,
            kill_seconds: float = KILL_SECONDS,
            cooldown_seconds: float = AUTO_RECOVERY_COOLDOWN_SECONDS,
            active_steam_app: Callable[[], str] | None = None):
        self.systemd = systemd or Systemd()
        self.processes = processes or ProcessTable()
        self.runtime_root = Path(runtime_root)
        self.current_lease_path = (
            self.runtime_root / "plugin-lifecycle-current.json")
        self.cooldown_path = (
            self.runtime_root / "plugin-loader-auto-recovery.json")
        self.focus_request_path = (
            self.runtime_root / AUTO_FOCUS_REQUEST_PATH.name)
        self.boot_id_path = Path(boot_id_path)
        self.maintenance_marker = Path(maintenance_marker)
        self.lock_path = Path(lock_path)
        self.monotonic = monotonic
        self.monotonic_ns = monotonic_ns
        self.sleep = sleep
        self.poll_seconds = poll_seconds
        self.heartbeat_stale_seconds = heartbeat_stale_seconds
        self.heartbeat_recheck_seconds = heartbeat_recheck_seconds
        self.replacement_wait_seconds = replacement_wait_seconds
        self.activation_wait_seconds = activation_wait_seconds
        self.owner_grace_seconds = owner_grace_seconds
        self.term_seconds = term_seconds
        self.kill_seconds = kill_seconds
        self.cooldown_seconds = cooldown_seconds
        self.recovery = recovery or RecoveryController(
            systemd=self.systemd, processes=self.processes,
            monotonic=self.monotonic, sleep=self.sleep)
        self.active_steam_app = active_steam_app or SteamGameDetector(
            proc_root=self.processes.proc_root).active_appid

    @staticmethod
    def _identity(value, name):
        if not isinstance(value, dict) or set(value) != {
                "pid", "start_time_ticks", "parent_pid"}:
            raise RecoveryError(f"lifecycle lease has an invalid {name} identity")
        fields = {}
        for key in ("pid", "start_time_ticks", "parent_pid"):
            candidate = value[key]
            if isinstance(candidate, bool) or not isinstance(candidate, int):
                raise RecoveryError(
                    f"lifecycle lease has an invalid {name} identity")
            fields[key] = candidate
        if fields["pid"] <= 0 or fields["start_time_ticks"] <= 0 or fields["parent_pid"] < 0:
            raise RecoveryError(f"lifecycle lease has an invalid {name} identity")
        return ProcessIdentity(**fields)

    @staticmethod
    def _regular_file(path: Path) -> bool:
        try:
            return stat.S_ISREG(path.lstat().st_mode) and not path.is_symlink()
        except OSError:
            return False

    def _boot_id(self):
        try:
            value = self.boot_id_path.read_text().strip()
        except OSError as reason:
            raise RecoveryError("current boot ID is unavailable") from reason
        if not value:
            raise RecoveryError("current boot ID is unavailable")
        return value

    def _load_lease(self, path: Path, *, current=False) -> LifecycleLease:
        path = Path(path)
        expected_current = self.current_lease_path
        if current:
            if path != expected_current:
                raise RecoveryError("invalid current lifecycle lease path")
        else:
            if path.parent != self.runtime_root:
                raise RecoveryError("lifecycle lease is outside the fixed runtime directory")
            match = re.fullmatch(
                r"plugin-lifecycle-([0-9a-f]{32})\.json", path.name)
            if match is None:
                raise RecoveryError("lifecycle lease has an invalid fixed path")
        if not self._regular_file(path):
            raise RecoveryError("lifecycle lease is missing or is not a regular file")
        try:
            candidate = json.loads(path.read_text())
        except (OSError, ValueError, json.JSONDecodeError) as reason:
            raise RecoveryError("lifecycle lease is malformed") from reason
        if not isinstance(candidate, dict) or set(candidate) != {
                "version", "token", "boot_id", "owner", "loader"}:
            raise RecoveryError("lifecycle lease has an invalid schema")
        version = candidate["version"]
        token = candidate["token"]
        boot_id = candidate["boot_id"]
        if isinstance(version, bool) or version != LEASE_VERSION:
            raise RecoveryError("unsupported lifecycle lease version")
        if not isinstance(token, str) or TOKEN_PATTERN.fullmatch(token) is None:
            raise RecoveryError("lifecycle lease token is invalid")
        if not current and token != match.group(1):
            raise RecoveryError("lifecycle lease token does not match its fixed path")
        if not isinstance(boot_id, str) or boot_id != self._boot_id():
            raise RecoveryError("lifecycle lease belongs to another boot")
        owner = self._identity(candidate["owner"], "owner")
        loader = self._identity(candidate["loader"], "loader")
        if owner.pid == loader.pid:
            raise RecoveryError("lifecycle owner and PluginLoader cannot be the same process")
        return LifecycleLease(
            version, token, boot_id, owner, loader, path, self.runtime_root)

    def _active(self, lease: LifecycleLease) -> bool:
        return self._regular_file(lease.active_path)

    def _maintenance_active(self) -> bool:
        """Return true only for a marker backed by the held shared lock.

        An interrupted maintenance process can leave its regular marker in
        ``/run``.  Once the shared lock is free, remove only that unchanged
        regular file so a stale marker cannot disable automatic recovery for
        the rest of the boot.  Symlinks, special files and inspection failures
        remain conservative and inhibit recovery.
        """
        try:
            before = self.maintenance_marker.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if not stat.S_ISREG(before.st_mode):
            return True

        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                self.lock_path,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
            )
        except OSError:
            return True
        acquired = False
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                return True
            except OSError:
                return True

            try:
                current = self.maintenance_marker.lstat()
            except FileNotFoundError:
                return False
            except OSError:
                return True
            if (not stat.S_ISREG(current.st_mode) or
                    current.st_dev != before.st_dev or
                    current.st_ino != before.st_ino):
                return True
            try:
                self.maintenance_marker.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                return True
            return False
        finally:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _same_lease(left: LifecycleLease, right: LifecycleLease) -> bool:
        return bool(
            left.version == right.version and left.token == right.token and
            left.boot_id == right.boot_id and left.owner == right.owner and
            left.loader == right.loader)

    @staticmethod
    def _unlink_fixed_marker(path: Path):
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return False
        except OSError as reason:
            raise RecoveryError(
                f"could not inspect lifecycle marker {path.name}") from reason
        # Unlinking a symlink removes the link itself, never its target.  Other
        # special files and directories are not valid lifecycle markers.
        if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
            raise RecoveryError(
                f"lifecycle marker {path.name} is not a removable file")
        try:
            path.unlink()
        except OSError as reason:
            raise RecoveryError(
                f"could not remove lifecycle marker {path.name}") from reason
        return True

    def retire_current(self):
        """Tombstone exactly the current generation before intentional stop.

        This action is deliberately one-shot and pathless.  The immutable
        generation lease must agree with the shared pointer before either
        derived marker can be removed.  A missing pointer is an idempotent
        success; malformed or cross-boot data fails without unlinking anything.
        """
        with self._publication_lock():
            return self._retire_current_locked()

    @contextmanager
    def _publication_lock(self):
        """Serialize CURRENT publication, retirement and maintenance.

        The plugin and installer use this same fixed lock while publishing or
        removing ``plugin-lifecycle-current.json``.  Retirement therefore
        cannot validate one generation and then tombstone it after another
        generation has become current.
        """
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as reason:
                raise RecoveryError(
                    "PluginLoader lifecycle publication is in progress") from reason
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _retire_current_locked(self):
        try:
            self.current_lease_path.lstat()
        except FileNotFoundError:
            return {
                "action": "retired-current",
                "retired": False,
                "token": "",
            }
        except OSError as reason:
            raise RecoveryError(
                "could not inspect current lifecycle pointer") from reason
        current = self._load_lease(self.current_lease_path, current=True)
        generation_path = (
            self.runtime_root /
            f"plugin-lifecycle-{current.token}.json")
        generation = self._load_lease(generation_path)
        if not self._same_lease(current, generation):
            raise RecoveryError(
                "current lifecycle pointer does not match its generation lease")

        active_removed = self._unlink_fixed_marker(generation.active_path)
        heartbeat_removed = self._unlink_fixed_marker(
            generation.heartbeat_path)
        ready_removed = self._unlink_fixed_marker(generation.ready_path)

        # A newer generation can replace the shared pointer while an updater
        # is beginning.  Never unlink that newer pointer.
        try:
            latest = self._load_lease(self.current_lease_path, current=True)
        except RecoveryError:
            latest = None
        current_removed = False
        if latest is not None and self._same_lease(latest, current):
            try:
                self.current_lease_path.unlink()
                current_removed = True
            except FileNotFoundError:
                pass
            except OSError as reason:
                raise RecoveryError(
                    "could not remove current lifecycle pointer") from reason
        return {
            "action": "retired-current",
            "retired": True,
            "token": current.token,
            "active_removed": active_removed,
            "heartbeat_removed": heartbeat_removed,
            "ready_removed": ready_removed,
            "current_removed": current_removed,
        }

    def _heartbeat_value(self, lease: LifecycleLease):
        if not self._regular_file(lease.heartbeat_path):
            return None
        try:
            raw = lease.heartbeat_path.read_text().strip()
        except OSError:
            return None
        if re.fullmatch(r"[1-9]\d*", raw or "") is None:
            return None
        try:
            value = int(raw)
        except ValueError:
            return None
        now = self.monotonic_ns()
        if value > now:
            return None
        return value

    def _heartbeat_fresh(self, lease: LifecycleLease) -> bool:
        observed = self._heartbeat_value(lease)
        if observed is None:
            return False
        age_ns = self.monotonic_ns() - observed
        return age_ns <= int(self.heartbeat_stale_seconds * 1_000_000_000)

    def _ready(self, lease: LifecycleLease) -> bool:
        if not self._regular_file(lease.ready_path):
            return False
        try:
            return lease.ready_path.read_text().strip() == lease.token
        except OSError:
            return False

    def _establish_readiness(self, lease: LifecycleLease) -> bool:
        if (not self._active(lease) or
                not self.processes.same_process(lease.owner) or
                not self._same_identity(
                    self._verified_current_loader(), lease.loader) or
                not self._heartbeat_fresh(lease)):
            return False
        with tempfile.NamedTemporaryFile(
                "w", dir=self.runtime_root, delete=False) as handle:
            handle.write(lease.token + "\n")
            temporary = Path(handle.name)
        temporary.chmod(0o600)
        temporary.replace(lease.ready_path)
        return True

    def _remove_readiness(self, lease: LifecycleLease):
        try:
            if lease.ready_path.read_text().strip() == lease.token:
                lease.ready_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _await_own_activation(self, lease: LifecycleLease):
        deadline = self.monotonic() + self.activation_wait_seconds
        while self.monotonic() < deadline:
            if not self._active(lease):
                return False
            try:
                current = self._load_lease(
                    self.current_lease_path, current=True)
            except RecoveryError:
                current = None
            if current is not None and self._same_lease(current, lease):
                return True
            self.sleep(min(
                self.poll_seconds,
                max(0.0, deadline - self.monotonic()),
            ))
        if not self._active(lease):
            return False
        raise RecoveryError(
            "lifecycle lease was not published as current before timeout")

    @staticmethod
    def _same_identity(left: ProcessIdentity | None,
                       right: ProcessIdentity | None) -> bool:
        return bool(
            left and right and left.pid == right.pid and
            left.start_time_ticks == right.start_time_ticks)

    def _verified_current_loader(self) -> ProcessIdentity | None:
        if not self.systemd.active():
            return None
        pid = self.systemd.main_pid()
        identity = self.processes.identity(pid) if pid else None
        if identity is None or not self.processes.matches_loader_binary(pid):
            return None
        try:
            control_group = RecoveryController._safe_control_group(
                self.systemd.control_group())
        except RecoveryError:
            return None
        if pid not in self.recovery._cgroup_pids(control_group):
            return None
        return identity

    def _capture_owner_tree(self, lease: LifecycleLease):
        if not self.processes.same_process(lease.owner):
            return ()
        identities = self.processes.all_identities()
        selected = self.processes.descendants(identities, (lease.owner.pid,))
        return tuple(
            sorted(
                (identities[pid] for pid in selected if pid in identities),
                key=lambda identity: identity.pid,
            )
        )

    def _replacement_lease(self, original: LifecycleLease):
        try:
            candidate = self._load_lease(self.current_lease_path, current=True)
        except RecoveryError:
            return None
        if (candidate.token == original.token or not self._active(candidate) or
                not self._ready(candidate)):
            return None
        if not self.processes.same_process(candidate.owner):
            return None
        current_loader = self._verified_current_loader()
        if not self._same_identity(candidate.loader, current_loader):
            return None
        if not self._heartbeat_fresh(candidate):
            return None
        return candidate

    def _wait_for_replacement(self, original: LifecycleLease):
        deadline = self.monotonic() + self.replacement_wait_seconds
        while self.monotonic() < deadline:
            if not self._active(original):
                return "clean"
            replacement = self._replacement_lease(original)
            if replacement is not None:
                return replacement
            self.sleep(min(
                self.poll_seconds,
                max(0.0, deadline - self.monotonic()),
            ))
        return self._replacement_lease(original)

    def _cleanup_old_owner(self, lease, captured):
        remaining = self.recovery._wait_gone(
            captured, self.owner_grace_seconds)
        if not self._active(lease):
            return {"clean": True, "term_pids": [], "kill_pids": []}
        term_pids = []
        kill_pids = []
        if remaining:
            term_pids = self.recovery._signal(remaining, signal.SIGTERM)
            remaining = self.recovery._wait_gone(
                remaining, self.term_seconds)
        if remaining:
            kill_pids = self.recovery._signal(remaining, signal.SIGKILL)
            remaining = self.recovery._wait_gone(
                remaining, self.kill_seconds)
        if remaining:
            raise RecoveryError(
                "stale RKE owner processes survived SIGKILL: " +
                ", ".join(str(item.pid) for item in remaining))
        return {
            "clean": False,
            "term_pids": term_pids,
            "kill_pids": kill_pids,
        }

    def _cooldown_active(self):
        if not self.cooldown_path.exists():
            return False
        if not self._regular_file(self.cooldown_path):
            return True
        try:
            candidate = json.loads(self.cooldown_path.read_text())
            valid = (
                isinstance(candidate, dict) and
                set(candidate) == {
                    "version", "boot_id", "attempted_monotonic"} and
                candidate["version"] == 1 and
                not isinstance(candidate["attempted_monotonic"], bool) and
                isinstance(candidate["attempted_monotonic"], (int, float)) and
                candidate["boot_id"] == self._boot_id()
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return True
        if not valid:
            return True
        age = self.monotonic() - float(candidate["attempted_monotonic"])
        return age < self.cooldown_seconds

    def _record_cooldown(self):
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "boot_id": self._boot_id(),
            "attempted_monotonic": self.monotonic(),
        }
        with tempfile.NamedTemporaryFile(
                "w", dir=self.runtime_root, delete=False) as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.chmod(0o600)
        temporary.replace(self.cooldown_path)

    def _clear_focus_request(self):
        try:
            mode = self.focus_request_path.lstat().st_mode
        except FileNotFoundError:
            return
        except OSError:
            return
        if stat.S_ISREG(mode) or stat.S_ISLNK(mode):
            try:
                self.focus_request_path.unlink()
            except OSError:
                pass

    def _record_focus_request(self, reason):
        """Record a short-lived request only for an automatic in-game reload."""
        self._clear_focus_request()
        try:
            appid = str(self.active_steam_app() or "")
        except Exception:
            return ""
        if (not appid.isdigit() or appid == "0" or len(appid) > 20):
            return ""
        payload = {
            "version": 1,
            "boot_id": self._boot_id(),
            "appid": appid,
            "requested_monotonic": self.monotonic(),
            "reason": str(reason),
        }
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
                "w", dir=self.runtime_root, delete=False) as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.chmod(0o600)
        temporary.replace(self.focus_request_path)
        return appid

    def _automatic_restart(self, reason):
        if self._cooldown_active():
            return {"action": "cooldown", "reason": reason, "restarted": False}
        self._record_cooldown()
        focus_appid = self._record_focus_request(reason)
        try:
            result = self.recovery.restart()
        except Exception:
            self._clear_focus_request()
            raise
        return {
            "action": "restarted",
            "reason": reason,
            "restarted": True,
            "focus_appid": focus_appid,
            "recovery": result,
        }

    def _replacement_result(self, lease, replacement, captured_owner):
        had_survivors = any(
            self.processes.same_process(identity)
            for identity in captured_owner)
        if captured_owner:
            refreshed = self._capture_owner_tree(lease)
            known = {item.pid: item for item in captured_owner}
            known.update({item.pid: item for item in refreshed})
            cleanup = self._cleanup_old_owner(
                lease, tuple(sorted(
                    known.values(), key=lambda identity: identity.pid)))
            if cleanup["clean"]:
                return {"action": "clean-unload", "restarted": False}
        else:
            cleanup = {"term_pids": [], "kill_pids": []}
        if had_survivors:
            return {
                "action": "loader-replaced",
                "token": replacement.token,
                "new_main_pid": replacement.loader.pid,
                "restarted": False,
                **{key: cleanup[key] for key in ("term_pids", "kill_pids")},
            }
        return {
            "action": "replacement-lease",
            "token": replacement.token,
            "restarted": False,
        }

    def run(self, lease_path: Path):
        lease = self._load_lease(Path(lease_path))
        if not self._active(lease):
            return {"action": "clean-unload", "restarted": False}
        ready = self._establish_readiness(lease)
        if not ready:
            raise RecoveryError(
                "lifecycle guard could not validate owner, Loader and heartbeat")
        try:
            if not self._await_own_activation(lease):
                return {"action": "clean-unload", "restarted": False}
            return self._run_lease(lease)
        finally:
            if ready:
                self._remove_readiness(lease)

    def _run_lease(self, lease: LifecycleLease):
        captured_owner = self._capture_owner_tree(lease)
        stale_checked_at = None

        while True:
            if not self._active(lease):
                return {"action": "clean-unload", "restarted": False}
            if self._maintenance_active():
                stale_checked_at = None
                self.sleep(self.poll_seconds)
                continue

            replacement = self._replacement_lease(lease)
            if replacement is not None:
                return self._replacement_result(
                    lease, replacement, captured_owner)

            owner_alive = self.processes.same_process(lease.owner)
            if owner_alive:
                refreshed = self._capture_owner_tree(lease)
                known = {item.pid: item for item in captured_owner}
                known.update({item.pid: item for item in refreshed})
                captured_owner = tuple(sorted(
                    known.values(), key=lambda identity: identity.pid))
            current_loader = self._verified_current_loader()
            same_loader = self._same_identity(current_loader, lease.loader)

            if current_loader is not None and not same_loader:
                cleanup = self._cleanup_old_owner(lease, captured_owner)
                if cleanup["clean"]:
                    return {"action": "clean-unload", "restarted": False}
                replacement = self._wait_for_replacement(lease)
                if replacement == "clean":
                    return {"action": "clean-unload", "restarted": False}
                if isinstance(replacement, LifecycleLease):
                    return {
                        "action": "loader-replaced",
                        "token": replacement.token,
                        "new_main_pid": current_loader.pid,
                        "restarted": False,
                        **{key: cleanup[key] for key in (
                            "term_pids", "kill_pids")},
                    }
                if not self.systemd.steam_active():
                    self.sleep(self.poll_seconds)
                    continue
                return self._automatic_restart("replacement-not-ready")

            if owner_alive and same_loader:
                if self._heartbeat_fresh(lease):
                    stale_checked_at = None
                    self.sleep(self.poll_seconds)
                    continue
                if stale_checked_at is None:
                    stale_checked_at = self.monotonic()
                    self.sleep(self.heartbeat_recheck_seconds)
                    continue
                if (self.monotonic() - stale_checked_at <
                        self.heartbeat_recheck_seconds):
                    self.sleep(self.heartbeat_recheck_seconds - (
                        self.monotonic() - stale_checked_at))
                    continue
                if self._heartbeat_fresh(lease):
                    stale_checked_at = None
                    continue
                if not self.systemd.steam_active():
                    self.sleep(self.poll_seconds)
                    continue
                return self._automatic_restart("stale-heartbeat")

            stale_checked_at = None
            replacement = self._wait_for_replacement(lease)
            if replacement == "clean":
                return {"action": "clean-unload", "restarted": False}
            if isinstance(replacement, LifecycleLease):
                return self._replacement_result(
                    lease, replacement, captured_owner)
            if not self.systemd.steam_active():
                self.sleep(self.poll_seconds)
                continue
            reason = "owner-dead" if not owner_alive else "loader-unavailable"
            return self._automatic_restart(reason)


def cli(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Safely restart ROCKNIX Decky PluginLoader")
    parser.add_argument(
        "action", choices=("restart", "guard", "retire-current"))
    parser.add_argument("lease", nargs="?")
    arguments = parser.parse_args(argv)
    if os.geteuid() != 0:
        print("PluginLoader recovery must run as root", file=sys.stderr)
        return 1
    try:
        if arguments.action == "restart":
            if arguments.lease is not None:
                raise RecoveryError("restart does not accept a lease path")
            result = RecoveryController().restart()
        elif arguments.action == "guard":
            if arguments.lease is None:
                raise RecoveryError("guard requires its fixed lifecycle lease path")
            result = LifecycleGuard().run(Path(arguments.lease))
        else:
            if arguments.lease is not None:
                raise RecoveryError("retire-current does not accept a path")
            result = LifecycleGuard().retire_current()
    except (RecoveryError, OSError, subprocess.SubprocessError) as reason:
        print(f"PluginLoader recovery failed: {reason}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
