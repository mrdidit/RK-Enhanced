import fcntl
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import plugin_loader_recovery as recovery


def stat_text(pid, parent, started, name="process"):
    # Fields after comm begin at field 3.  starttime is field 22.
    tail = ["S", str(parent), *(["0"] * 17), str(started), "0", "0"]
    return f"{pid} ({name}) {' '.join(tail)}\n"


class FakeClock:
    def __init__(self):
        self.value = 0.0
        self.on_sleep = None

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds
        if self.on_sleep:
            self.on_sleep(self.value)


class FakeSystemd:
    def __init__(self, main_pid, control_group="/system.slice/plugin_loader.service"):
        self.pid = main_pid
        self.group = control_group
        self.is_running = bool(main_pid)
        self.steam_is_running = True
        self.calls = []
        self.main_pid_calls = 0
        self.control_group_calls = 0
        self.active_calls = 0
        self.on_stop = None
        self.on_start = None

    def main_pid(self):
        self.main_pid_calls += 1
        return self.pid

    def control_group(self):
        self.control_group_calls += 1
        return self.group

    def active(self):
        self.active_calls += 1
        return self.is_running

    def steam_active(self):
        return self.steam_is_running

    def stop_no_block(self):
        self.calls.append("stop")
        self.is_running = False
        if self.on_stop:
            self.on_stop()

    def reset_failed(self):
        self.calls.append("reset-failed")

    def start_no_block(self):
        self.calls.append("start")
        if self.on_start:
            self.on_start()


class RecoveryFixture:
    def __init__(self, test):
        self.test = test
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.proc = self.root / "proc"
        self.cgroup = self.root / "cgroup"
        self.lock = self.root / "run" / "recovery.lock"
        self.marker = self.root / "run" / "recovery.active"
        self.proc.mkdir()
        self.group = self.cgroup / "system.slice" / recovery.SERVICE
        self.group.mkdir(parents=True)
        self.group_file = self.group / "cgroup.procs"
        self.group_file.write_text("")
        self.clock = FakeClock()
        self.systemd = FakeSystemd(100)
        self.signals = []
        self.table = recovery.ProcessTable(
            self.proc, send_signal=self._signal)
        self.controller = recovery.RecoveryController(
            systemd=self.systemd,
            processes=self.table,
            cgroup_root=self.cgroup,
            lock_path=self.lock,
            marker_path=self.marker,
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
            current_pid=lambda: 9999,
        )

    def close(self):
        self.temporary.cleanup()

    def process(self, pid, parent, started, *, loader=False, fex=False):
        directory = self.proc / str(pid)
        directory.mkdir(exist_ok=True)
        (directory / "stat").write_text(
            stat_text(pid, parent, started, "PluginLoader" if loader else "worker"))
        if loader:
            target = (
                "/usr/bin/FEX" if fex
                else str(recovery.PLUGIN_LOADER_BINARY))
            (directory / "exe").symlink_to(target)
            arguments = (
                [target, str(recovery.PLUGIN_LOADER_BINARY)]
                if fex else [str(recovery.PLUGIN_LOADER_BINARY)])
        else:
            (directory / "exe").symlink_to("/usr/bin/python3")
            arguments = ["/usr/bin/python3", "main.py"]
        (directory / "cmdline").write_bytes(
            b"\0".join(value.encode() for value in arguments) + b"\0")
        task = directory / "task" / str(pid)
        task.mkdir(parents=True, exist_ok=True)
        (task / "children").write_text("")
        parent_children = (
            self.proc / str(parent) / "task" / str(parent) / "children")
        if parent_children.exists():
            children = parent_children.read_text().split()
            if str(pid) not in children:
                parent_children.write_text(" ".join([*children, str(pid)]) + "\n")

    def remove(self, pid):
        directory = self.proc / str(pid)
        for children_path in self.proc.glob("*/task/*/children"):
            children = children_path.read_text().split()
            if str(pid) in children:
                children_path.write_text(
                    " ".join(value for value in children if value != str(pid)) +
                    ("\n" if len(children) > 1 else ""))
        shutil.rmtree(directory)

    def group_pids(self, *pids):
        self.group_file.write_text(
            "".join(f"{pid}\n" for pid in pids))

    def replace_group_pid(self, pid):
        self.group_pids(pid)

    def _signal(self, pid, requested):
        self.signals.append((pid, requested))

    def start_as(self, pid=200, started=2000, *, loader=True):
        self.process(pid, 1, started, loader=loader)
        self.replace_group_pid(pid)
        self.systemd.pid = pid
        self.systemd.is_running = True


class PluginLoaderRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = RecoveryFixture(self)

    def tearDown(self):
        self.fixture.close()

    def populate_old_generation(self):
        self.fixture.process(100, 1, 1000, loader=True)
        self.fixture.process(101, 100, 1001)
        self.fixture.group_pids(100, 101)

    def test_graceful_stop_restarts_with_a_new_verified_main_pid(self):
        self.populate_old_generation()

        def stop():
            self.fixture.remove(101)
            self.fixture.remove(100)
            self.fixture.group_pids()
            self.fixture.systemd.pid = 0

        self.fixture.systemd.on_stop = stop
        self.fixture.systemd.on_start = lambda: self.fixture.start_as()

        result = self.fixture.controller.restart()

        self.assertEqual(result["old_main_pid"], 100)
        self.assertEqual(result["new_main_pid"], 200)
        self.assertEqual(result["captured_pids"], [100, 101])
        self.assertEqual(result["term_pids"], [])
        self.assertEqual(result["kill_pids"], [])
        self.assertEqual(
            self.fixture.systemd.calls, ["stop", "reset-failed", "start"])
        self.assertEqual(self.fixture.signals, [])
        self.assertFalse(self.fixture.marker.exists())

    def test_escalates_children_first_and_only_kills_revalidated_snapshot(self):
        self.populate_old_generation()
        self.fixture.process(300, 1, 3000)  # Unrelated FEX/Python-style process.
        self.fixture.systemd.on_stop = lambda: setattr(
            self.fixture.systemd, "pid", 0)
        self.fixture.systemd.on_start = lambda: self.fixture.start_as()

        def send(pid, requested):
            self.fixture.signals.append((pid, requested))
            if pid == 101 and requested == signal.SIGTERM:
                self.fixture.remove(101)
            if pid == 100 and requested == signal.SIGKILL:
                self.fixture.remove(100)

        self.fixture.table.send_signal = send

        result = self.fixture.controller.restart()

        self.assertEqual(result["term_pids"], [101, 100])
        self.assertEqual(result["kill_pids"], [100])
        self.assertEqual(self.fixture.signals, [
            (101, signal.SIGTERM),
            (100, signal.SIGTERM),
            (100, signal.SIGKILL),
        ])
        self.assertTrue((self.fixture.proc / "300").exists())

    def test_pid_reuse_is_not_signalled(self):
        self.populate_old_generation()

        def stop():
            self.fixture.systemd.pid = 0
            # PID 101 was captured, exited, and was reused by another process.
            (self.fixture.proc / "101" / "stat").write_text(
                stat_text(101, 1, 9001, "unrelated"))

        self.fixture.systemd.on_stop = stop
        self.fixture.systemd.on_start = lambda: self.fixture.start_as()

        def send(pid, requested):
            self.fixture.signals.append((pid, requested))
            if pid == 100 and requested == signal.SIGKILL:
                self.fixture.remove(100)

        self.fixture.table.send_signal = send

        self.fixture.controller.restart()

        self.assertNotIn(101, [pid for pid, _signal in self.fixture.signals])
        self.assertTrue((self.fixture.proc / "101").exists())

    def test_fex_main_is_accepted_only_with_exact_binary_argument(self):
        self.fixture.process(100, 1, 1000, loader=True, fex=True)
        self.fixture.group_pids(100)

        snapshot = self.fixture.controller.snapshot()

        self.assertEqual(snapshot.main.pid, 100)
        self.assertEqual([item.pid for item in snapshot.processes], [100])

    def test_descendant_outside_cgroup_is_captured_by_exact_process_tree(self):
        self.fixture.process(100, 1, 1000, loader=True, fex=True)
        self.fixture.process(101, 100, 1001)
        self.fixture.process(300, 1, 3000)
        self.fixture.group_pids(100)

        snapshot = self.fixture.controller.snapshot()

        self.assertEqual([item.pid for item in snapshot.processes], [100, 101])

    def test_refuses_to_run_from_inside_pluginloader_cgroup(self):
        self.populate_old_generation()
        self.fixture.controller.current_pid = lambda: 101

        with self.assertRaisesRegex(
                recovery.RecoveryError, "must run outside"):
            self.fixture.controller.restart()

        self.assertEqual(self.fixture.systemd.calls, [])
        self.assertFalse(self.fixture.marker.exists())

    def test_rejects_root_or_wrong_service_cgroup(self):
        self.populate_old_generation()
        for unsafe in ("/", "/system.slice/another.service", "relative/path"):
            with self.subTest(control_group=unsafe):
                self.fixture.systemd.group = unsafe
                with self.assertRaisesRegex(
                        recovery.RecoveryError, "unsafe or missing"):
                    self.fixture.controller.snapshot()

    def test_rejects_main_pid_that_is_not_exact_pluginloader_binary(self):
        self.fixture.process(100, 1, 1000)
        self.fixture.group_pids(100)

        with self.assertRaisesRegex(
                recovery.RecoveryError, "does not match the fixed binary"):
            self.fixture.controller.snapshot()

    def test_same_numeric_main_pid_is_not_accepted_as_the_restart(self):
        self.populate_old_generation()

        def stop():
            self.fixture.remove(101)
            self.fixture.remove(100)
            self.fixture.group_pids()
            self.fixture.systemd.pid = 0

        self.fixture.systemd.on_stop = stop
        self.fixture.systemd.on_start = lambda: self.fixture.start_as(
            pid=100, started=8000)

        with self.assertRaisesRegex(
                recovery.RecoveryError, "new verified MainPID"):
            self.fixture.controller.restart()

        self.assertFalse(self.fixture.marker.exists())

    def test_nonblocking_maintenance_lock_prevents_parallel_recovery(self):
        self.populate_old_generation()
        self.fixture.lock.parent.mkdir(parents=True)
        descriptor = os.open(
            self.fixture.lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(
                    recovery.RecoveryError, "maintenance action"):
                self.fixture.controller.restart()
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

        self.assertEqual(self.fixture.systemd.calls, [])
        self.assertFalse(self.fixture.marker.exists())

    def test_source_contains_no_global_process_name_kill(self):
        source = Path(recovery.__file__).read_text()

        self.assertNotIn("pkill", source)
        self.assertNotIn("killall", source)
        self.assertNotIn("systemctl kill", source)
        self.assertEqual(recovery.SERVICE, "plugin_loader.service")
        self.assertEqual(
            recovery.PLUGIN_LOADER_BINARY,
            Path("/storage/homebrew/services/PluginLoader"))

    def test_systemd_boundary_uses_only_fixed_unit_and_nonblocking_actions(self):
        calls = []

        def run(command, **options):
            calls.append((command, options))
            output = "123\n" if "--property=MainPID" in command else ""
            return subprocess.CompletedProcess(command, 0, output, "")

        systemd = recovery.Systemd(run=run)
        self.assertEqual(systemd.main_pid(), 123)
        systemd.stop_no_block()
        systemd.reset_failed()
        systemd.start_no_block()

        self.assertEqual(calls[0][0], [
            "systemctl", "show", "--property=MainPID", "--value",
            recovery.SERVICE,
        ])
        self.assertEqual(calls[1][0], [
            "systemctl", "stop", "--no-block", recovery.SERVICE,
        ])
        self.assertEqual(calls[2][0], [
            "systemctl", "reset-failed", recovery.SERVICE,
        ])
        self.assertEqual(calls[3][0], [
            "systemctl", "start", "--no-block", recovery.SERVICE,
        ])
        self.assertTrue(all("shell" not in options for _command, options in calls))


class SteamGameDetectorTests(unittest.TestCase):
    def test_detects_newest_real_game_and_ignores_helper_processes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc = root / "proc"
            cgroup = root / "cgroup"
            scope = (cgroup / "systemd" / "system.slice" /
                     "steam-bigpicture.scope")
            proc.mkdir()
            scope.mkdir(parents=True)
            (scope / "cgroup.procs").write_text("10\n20\n30\n40\n")

            def process(pid, comm, environment):
                directory = proc / str(pid)
                directory.mkdir()
                (directory / "comm").write_text(comm + "\n")
                (directory / "environ").write_bytes(
                    b"\0".join(value.encode() for value in environment) + b"\0")

            process(10, "steamwebhelper", ["SteamAppId=999999"])
            process(20, "game-one", ["SteamAppId=111111"])
            process(30, "game-two", ["SteamGameId=222222"])
            process(40, "helper", ["SteamAppId=0"])

            detector = recovery.SteamGameDetector(proc, cgroup)

            self.assertEqual(detector.active_appid(), "222222")

    def test_missing_or_malformed_scope_has_no_focus_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc = root / "proc"
            cgroup = root / "cgroup"
            proc.mkdir()
            cgroup.mkdir()
            detector = recovery.SteamGameDetector(proc, cgroup)

            self.assertEqual(detector.active_appid(), "")

            scope = (cgroup / "pids" / "system.slice" /
                     "steam-bigpicture.scope")
            scope.mkdir(parents=True)
            (scope / "cgroup.procs").write_text("not-a-pid\n")
            self.assertEqual(detector.active_appid(), "")


class RecordingRecovery:
    def __init__(self, base):
        self.base = base
        self.restart_calls = 0

    def _cgroup_pids(self, control_group):
        return self.base._cgroup_pids(control_group)

    def _wait_gone(self, identities, seconds):
        return self.base._wait_gone(identities, seconds)

    def _signal(self, identities, requested):
        return self.base._signal(identities, requested)

    def restart(self):
        self.restart_calls += 1
        return {
            "service": recovery.SERVICE,
            "new_main_pid": 200,
            "active": True,
        }


class LifecycleGuardTests(unittest.TestCase):
    TOKEN = "a" * 32
    NEXT_TOKEN = "b" * 32
    BOOT_ID = "11111111-2222-3333-4444-555555555555"

    def setUp(self):
        self.fixture = RecoveryFixture(self)
        self.fixture.clock.value = 1000.0
        self.runtime = self.fixture.root / "run" / "rk-enhanced"
        self.runtime.mkdir(parents=True)
        self.boot_id = self.fixture.root / "boot_id"
        self.boot_id.write_text(self.BOOT_ID + "\n")
        self.recording = RecordingRecovery(self.fixture.controller)
        self.guard = recovery.LifecycleGuard(
            recovery=self.recording,
            systemd=self.fixture.systemd,
            processes=self.fixture.table,
            runtime_root=self.runtime,
            boot_id_path=self.boot_id,
            maintenance_marker=self.fixture.marker,
            lock_path=self.fixture.lock,
            monotonic=self.fixture.clock.monotonic,
            monotonic_ns=lambda: int(
                self.fixture.clock.monotonic() * 1_000_000_000),
            sleep=self.fixture.clock.sleep,
            poll_seconds=1.0,
            heartbeat_stale_seconds=5.0,
            heartbeat_recheck_seconds=6.0,
            replacement_wait_seconds=20.0,
            activation_wait_seconds=10.0,
            owner_grace_seconds=1.0,
            term_seconds=1.0,
            kill_seconds=1.0,
            cooldown_seconds=120.0,
            active_steam_app=lambda: "",
        )

    def tearDown(self):
        self.fixture.close()

    @staticmethod
    def identity_value(identity):
        return {
            "pid": identity.pid,
            "start_time_ticks": identity.start_time_ticks,
            "parent_pid": identity.parent_pid,
        }

    def populate_generation(self, token=TOKEN, loader=100, owner=101,
                            loader_started=1000, owner_started=1001,
                            current=True, ready=False):
        self.fixture.process(loader, 1, loader_started, loader=True, fex=True)
        self.fixture.process(owner, loader, owner_started)
        self.fixture.group_pids(loader, owner)
        self.fixture.systemd.pid = loader
        self.fixture.systemd.is_running = True
        payload = {
            "version": 1,
            "token": token,
            "boot_id": self.BOOT_ID,
            "owner": self.identity_value(self.fixture.table.identity(owner)),
            "loader": self.identity_value(self.fixture.table.identity(loader)),
        }
        path = self.runtime / f"plugin-lifecycle-{token}.json"
        path.write_text(json.dumps(payload) + "\n")
        active = self.runtime / f"plugin-lifecycle-{token}.active"
        active.write_text("active\n")
        heartbeat = self.runtime / f"plugin-lifecycle-{token}.heartbeat"
        heartbeat.write_text(
            str(int(self.fixture.clock.value * 1_000_000_000)) + "\n")
        if ready:
            (self.runtime / f"plugin-lifecycle-{token}.ready").write_text(
                token + "\n")
        if current:
            (self.runtime / "plugin-lifecycle-current.json").write_text(
                json.dumps(payload) + "\n")
        return path, payload, active, heartbeat

    def replace_generation(self, token=NEXT_TOKEN, loader=200, owner=201):
        for pid in (100, 101):
            if (self.fixture.proc / str(pid)).exists():
                self.fixture.remove(pid)
        return self.populate_generation(
            token, loader, owner, loader_started=2000,
            owner_started=2001, current=True, ready=True)

    def write_heartbeat(self, path, observed=None):
        if observed is None:
            observed = self.fixture.clock.value
        path.write_text(str(int(observed * 1_000_000_000)) + "\n")

    def test_clean_active_marker_removal_exits_without_recovery(self):
        lease, _payload, active, _heartbeat = self.populate_generation()
        active.unlink()

        result = self.guard.run(lease)

        self.assertEqual(result["action"], "clean-unload")
        self.assertEqual(self.recording.restart_calls, 0)
        self.assertEqual(self.fixture.signals, [])

    def test_healthy_owner_loader_and_heartbeat_wait_for_clean_unload(self):
        lease, _payload, active, _heartbeat = self.populate_generation()
        ready = self.runtime / f"plugin-lifecycle-{self.TOKEN}.ready"
        saw_ready = False

        def after_sleep(now):
            nonlocal saw_ready
            saw_ready = saw_ready or ready.read_text().strip() == self.TOKEN
            if now >= 1002 and active.exists():
                active.unlink()

        self.fixture.clock.on_sleep = after_sleep

        result = self.guard.run(lease)

        self.assertGreaterEqual(self.fixture.clock.value, 1002)
        self.assertTrue(saw_ready)
        self.assertFalse(ready.exists())
        self.assertEqual(result["action"], "clean-unload")
        self.assertEqual(self.recording.restart_calls, 0)

    def test_healthy_fast_path_avoids_full_proc_and_repeated_systemd_queries(self):
        lease, _payload, active, _heartbeat = self.populate_generation()
        self.guard.loader_verify_seconds = 30.0

        def after_sleep(now):
            if now >= 1004:
                active.unlink(missing_ok=True)

        self.fixture.clock.on_sleep = after_sleep
        with mock.patch.object(
                self.fixture.table, "all_identities",
                side_effect=AssertionError("healthy guard scanned all of /proc")):
            result = self.guard.run(lease)

        self.assertEqual(result["action"], "clean-unload")
        self.assertEqual(self.fixture.systemd.active_calls, 1)
        self.assertEqual(self.fixture.systemd.main_pid_calls, 1)
        self.assertEqual(self.fixture.systemd.control_group_calls, 1)

    def test_healthy_fast_path_periodically_reverifies_loader_with_systemd(self):
        lease, _payload, active, _heartbeat = self.populate_generation()
        self.guard.loader_verify_seconds = 2.0

        def after_sleep(now):
            if now >= 1003:
                active.unlink(missing_ok=True)

        self.fixture.clock.on_sleep = after_sleep
        result = self.guard.run(lease)

        self.assertEqual(result["action"], "clean-unload")
        self.assertEqual(self.fixture.systemd.active_calls, 2)
        self.assertEqual(self.fixture.systemd.main_pid_calls, 2)
        self.assertEqual(self.fixture.systemd.control_group_calls, 2)

    def test_exact_loader_identity_change_forces_immediate_full_verification(self):
        lease_path, _payload, _active, _heartbeat = self.populate_generation()
        lease = self.guard._load_lease(lease_path)
        self.assertTrue(self.guard._establish_readiness(lease))
        (self.fixture.proc / "100" / "stat").write_text(
            stat_text(100, 1, 9000, "PluginLoader"))

        current = self.guard._current_loader_for_lease(lease)

        self.assertEqual(current.start_time_ticks, 9000)
        self.assertEqual(self.fixture.systemd.active_calls, 2)
        self.assertEqual(self.fixture.systemd.main_pid_calls, 2)
        self.assertEqual(self.fixture.systemd.control_group_calls, 2)

    def test_incomplete_bounded_owner_tree_uses_conservative_full_scan(self):
        lease_path, _payload, _active, _heartbeat = self.populate_generation()
        lease = self.guard._load_lease(lease_path)
        expected = self.fixture.table.identity(101)
        with mock.patch.object(
                self.fixture.table, "bounded_process_tree",
                return_value=((expected,), False)), mock.patch.object(
                    self.fixture.table, "all_identities",
                    wraps=self.fixture.table.all_identities) as full_scan:
            captured = self.guard._capture_owner_tree(lease)

        self.assertEqual([item.pid for item in captured], [101])
        full_scan.assert_called_once_with()

    def test_fallback_rejects_owner_pid_reused_during_tree_capture(self):
        lease_path, _payload, _active, _heartbeat = self.populate_generation()
        lease = self.guard._load_lease(lease_path)
        reused = recovery.ProcessIdentity(101, 9001, 1)
        unrelated_child = recovery.ProcessIdentity(102, 9002, 101)
        with mock.patch.object(
                self.fixture.table, "bounded_process_tree",
                return_value=((lease.owner,), False)), mock.patch.object(
                    self.fixture.table, "all_identities",
                    return_value={101: reused, 102: unrelated_child}):
            captured = self.guard._capture_owner_tree(lease)

        self.assertEqual(captured, ())

    def test_different_unready_pointer_forces_immediate_loader_verification(self):
        lease, _payload, _active, _heartbeat = self.populate_generation()
        self.guard.loader_verify_seconds = 100.0
        transitioned = False

        def after_sleep(now):
            nonlocal transitioned
            if now >= 1001 and not transitioned:
                transitioned = True
                # Keep the old Loader and owner alive.  Only systemd and the
                # unready CURRENT pointer expose this interrupted handoff.
                self.populate_generation(
                    self.NEXT_TOKEN, 200, 201,
                    loader_started=2000, owner_started=2001,
                    current=True, ready=False)

        def send(pid, requested):
            self.fixture.signals.append((pid, requested))
            if requested == signal.SIGTERM and pid == 101:
                self.fixture.remove(pid)

        self.fixture.clock.on_sleep = after_sleep
        self.fixture.table.send_signal = send
        result = self.guard.run(lease)

        self.assertTrue(transitioned)
        self.assertEqual(result["action"], "restarted")
        self.assertEqual(result["reason"], "replacement-not-ready")
        self.assertGreaterEqual(self.fixture.systemd.active_calls, 2)

    def test_maintenance_release_forces_immediate_loader_verification(self):
        lease, _payload, _active, _heartbeat = self.populate_generation()
        self.guard.loader_verify_seconds = 100.0
        self.fixture.marker.parent.mkdir(parents=True, exist_ok=True)
        self.fixture.marker.write_text("maintenance\n")
        descriptor = os.open(
            self.fixture.lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        transitioned = False

        def after_sleep(now):
            nonlocal transitioned
            if now >= 1001 and not transitioned:
                transitioned = True
                # Keep the old generation alive while systemd moves to a new
                # Loader, then end maintenance without publishing a lease.
                self.fixture.process(200, 1, 2000, loader=True, fex=True)
                self.fixture.group_pids(200)
                self.fixture.systemd.pid = 200
                fcntl.flock(descriptor, fcntl.LOCK_UN)

        def send(pid, requested):
            self.fixture.signals.append((pid, requested))
            if requested == signal.SIGTERM and pid == 101:
                self.fixture.remove(pid)

        self.fixture.clock.on_sleep = after_sleep
        self.fixture.table.send_signal = send
        try:
            result = self.guard.run(lease)
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(descriptor)

        self.assertTrue(transitioned)
        self.assertEqual(result["action"], "restarted")
        self.assertEqual(result["reason"], "replacement-not-ready")
        self.assertGreaterEqual(self.fixture.systemd.active_calls, 2)

    def test_new_loader_generation_cleans_only_stale_old_owner_tree(self):
        lease, _payload, _active, _heartbeat = self.populate_generation()
        self.fixture.process(102, 101, 1002)
        self.fixture.process(300, 1, 3000)
        transitioned = False

        def after_sleep(now):
            nonlocal transitioned
            if now >= 1001 and not transitioned:
                transitioned = True
                # The old backend survived, but systemd has a verified new
                # Loader for which no ready replacement lease is published.
                self.fixture.remove(100)
                self.fixture.process(200, 1, 2000, loader=True, fex=True)
                self.fixture.group_pids(200)
                self.fixture.systemd.pid = 200

        def send(pid, requested):
            self.fixture.signals.append((pid, requested))
            if requested == signal.SIGTERM and (self.fixture.proc / str(pid)).exists():
                self.fixture.remove(pid)

        self.fixture.clock.on_sleep = after_sleep
        self.fixture.table.send_signal = send

        result = self.guard.run(lease)

        self.assertEqual(result["action"], "restarted")
        self.assertEqual(result["reason"], "replacement-not-ready")
        self.assertEqual(
            [pid for pid, requested in self.fixture.signals
             if requested == signal.SIGTERM], [102, 101])
        self.assertTrue((self.fixture.proc / "200").exists())
        self.assertTrue((self.fixture.proc / "300").exists())
        self.assertEqual(self.recording.restart_calls, 1)
        self.assertGreaterEqual(self.fixture.clock.value, 1022)

    def test_owner_death_waits_for_and_accepts_valid_current_replacement(self):
        lease, _payload, _active, _heartbeat = self.populate_generation()
        owner_removed = False
        installed = False

        def after_sleep(now):
            nonlocal owner_removed, installed
            if now >= 1001 and not owner_removed:
                owner_removed = True
                self.fixture.remove(101)
            if now >= 1005 and not installed:
                installed = True
                self.replace_generation()

        self.fixture.clock.on_sleep = after_sleep

        result = self.guard.run(lease)

        self.assertEqual(result["action"], "replacement-lease")
        self.assertEqual(result["token"], self.NEXT_TOKEN)
        self.assertEqual(self.recording.restart_calls, 0)
        self.assertGreaterEqual(self.fixture.clock.value, 1005)

    def test_valid_replacement_still_cleans_a_surviving_old_owner(self):
        lease, _payload, _active, _heartbeat = self.populate_generation()
        self.fixture.process(102, 101, 1002)
        transitioned = False

        def after_sleep(now):
            nonlocal transitioned
            if now >= 1001 and not transitioned:
                transitioned = True
                self.fixture.remove(100)
                self.populate_generation(
                    self.NEXT_TOKEN, 200, 201,
                    loader_started=2000, owner_started=2001, current=True,
                    ready=True)

        def send(pid, requested):
            self.fixture.signals.append((pid, requested))
            if requested == signal.SIGTERM and (self.fixture.proc / str(pid)).exists():
                self.fixture.remove(pid)

        self.fixture.clock.on_sleep = after_sleep
        self.fixture.table.send_signal = send

        result = self.guard.run(lease)

        self.assertEqual(result["action"], "loader-replaced")
        self.assertEqual(result["token"], self.NEXT_TOKEN)
        self.assertEqual(result["term_pids"], [102, 101])
        self.assertTrue((self.fixture.proc / "200").exists())
        self.assertTrue((self.fixture.proc / "201").exists())
        self.assertEqual(self.recording.restart_calls, 0)

    def test_replacement_cleans_captured_child_after_owner_dies(self):
        lease, _payload, _active, _heartbeat = self.populate_generation()
        self.fixture.process(102, 101, 1002)
        replaced = False

        def after_sleep(now):
            nonlocal replaced
            if now >= 1001 and not replaced:
                replaced = True
                self.fixture.remove(101)
                (self.fixture.proc / "102" / "stat").write_text(
                    stat_text(102, 1, 1002, "orphaned-worker"))
                self.fixture.remove(100)
                self.populate_generation(
                    self.NEXT_TOKEN, 200, 201,
                    loader_started=2000, owner_started=2001, current=True,
                    ready=True)

        def send(pid, requested):
            self.fixture.signals.append((pid, requested))
            if requested == signal.SIGTERM and (self.fixture.proc / str(pid)).exists():
                self.fixture.remove(pid)

        self.fixture.clock.on_sleep = after_sleep
        self.fixture.table.send_signal = send

        result = self.guard.run(lease)

        self.assertTrue(replaced)
        self.assertEqual(result["action"], "loader-replaced")
        self.assertEqual(result["token"], self.NEXT_TOKEN)
        self.assertEqual(result["term_pids"], [102])
        self.assertFalse((self.fixture.proc / "102").exists())
        self.assertTrue((self.fixture.proc / "200").exists())
        self.assertTrue((self.fixture.proc / "201").exists())
        self.assertEqual(self.recording.restart_calls, 0)

    def test_stale_heartbeat_requires_recheck_then_recovers_once(self):
        lease, _payload, _active, heartbeat = self.populate_generation()
        made_stale = False

        def after_sleep(now):
            nonlocal made_stale
            if now >= 1001 and not made_stale:
                made_stale = True
                self.write_heartbeat(heartbeat, 900)

        self.fixture.clock.on_sleep = after_sleep

        result = self.guard.run(lease)

        self.assertEqual(result["action"], "restarted")
        self.assertEqual(result["reason"], "stale-heartbeat")
        self.assertEqual(self.recording.restart_calls, 1)
        self.assertGreaterEqual(self.fixture.clock.value, 1007)
        cooldown = json.loads(self.guard.cooldown_path.read_text())
        self.assertEqual(cooldown["boot_id"], self.BOOT_ID)
        self.assertEqual(
            cooldown["attempted_monotonic"], self.fixture.clock.value)

    def test_automatic_in_game_restart_records_one_shot_focus_request(self):
        lease, _payload, _active, heartbeat = self.populate_generation()
        self.guard.active_steam_app = lambda: "3214610"
        made_stale = False

        def after_sleep(now):
            nonlocal made_stale
            if now >= 1001 and not made_stale:
                made_stale = True
                self.write_heartbeat(heartbeat, 900)

        self.fixture.clock.on_sleep = after_sleep

        result = self.guard.run(lease)

        self.assertEqual(result["action"], "restarted")
        self.assertEqual(result["focus_appid"], "3214610")
        request = json.loads(self.guard.focus_request_path.read_text())
        self.assertEqual(request, {
            "version": 1,
            "boot_id": self.BOOT_ID,
            "appid": "3214610",
            "requested_monotonic": self.fixture.clock.value,
            "reason": "stale-heartbeat",
        })
        self.assertEqual(
            self.guard.focus_request_path.stat().st_mode & 0o777, 0o600)

    def test_failed_automatic_restart_removes_focus_request(self):
        self.populate_generation()
        self.guard.active_steam_app = lambda: "3214610"
        self.recording.restart = mock.Mock(
            side_effect=recovery.RecoveryError("restart failed"))

        with self.assertRaisesRegex(recovery.RecoveryError, "restart failed"):
            self.guard._automatic_restart("stale-heartbeat")

        self.assertFalse(self.guard.focus_request_path.exists())

    def test_automatic_restart_without_game_clears_old_focus_request(self):
        self.populate_generation()
        self.guard.focus_request_path.write_text("stale\n")

        result = self.guard._automatic_restart("owner-dead")

        self.assertEqual(result["focus_appid"], "")
        self.assertFalse(self.guard.focus_request_path.exists())

    def test_heartbeat_recovery_during_recheck_cancels_restart(self):
        lease, _payload, active, heartbeat = self.populate_generation()
        made_stale = False
        refreshed = False

        def after_sleep(now):
            nonlocal made_stale, refreshed
            if now >= 1001 and not made_stale:
                made_stale = True
                self.write_heartbeat(heartbeat, 900)
            elif now >= 1007 and not refreshed:
                refreshed = True
                self.write_heartbeat(heartbeat, now)
            if refreshed and now >= 1008:
                active.unlink(missing_ok=True)

        self.fixture.clock.on_sleep = after_sleep

        result = self.guard.run(lease)

        self.assertTrue(refreshed)
        self.assertEqual(result["action"], "clean-unload")
        self.assertEqual(self.recording.restart_calls, 0)

    def test_owner_death_without_replacement_recovers_once_after_wait(self):
        lease, _payload, _active, _heartbeat = self.populate_generation()
        removed = False

        def after_sleep(now):
            nonlocal removed
            if now >= 1001 and not removed:
                removed = True
                self.fixture.remove(101)

        self.fixture.clock.on_sleep = after_sleep

        result = self.guard.run(lease)

        self.assertEqual(result["action"], "restarted")
        self.assertEqual(result["reason"], "owner-dead")
        self.assertEqual(self.recording.restart_calls, 1)
        self.assertGreaterEqual(self.fixture.clock.value, 1021)

    def test_maintenance_marker_inhibits_until_clean_unload(self):
        lease, _payload, active, _heartbeat = self.populate_generation()
        self.fixture.marker.parent.mkdir(parents=True, exist_ok=True)
        self.fixture.marker.write_text("maintenance\n")
        descriptor = os.open(
            self.fixture.lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        released = False

        def after_sleep(now):
            nonlocal released
            if now >= 1002 and not released:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                released = True
            if now >= 1003:
                active.unlink(missing_ok=True)

        self.fixture.clock.on_sleep = after_sleep
        try:
            result = self.guard.run(lease)
        finally:
            if not released:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

        self.assertEqual(result["action"], "clean-unload")
        self.assertEqual(self.recording.restart_calls, 0)
        self.assertFalse(self.fixture.marker.exists())

    def test_stale_regular_maintenance_marker_is_removed(self):
        lease_path, _payload, _active, _heartbeat = self.populate_generation()
        self.fixture.marker.parent.mkdir(parents=True, exist_ok=True)
        self.fixture.marker.write_text("abandoned-maintenance\n")

        self.assertFalse(self.guard._maintenance_active())

        self.assertFalse(self.fixture.marker.exists())
        self.assertTrue(self.guard._active(self.guard._load_lease(lease_path)))

    def test_unsafe_maintenance_marker_is_conservatively_preserved(self):
        self.populate_generation()
        self.fixture.marker.parent.mkdir(parents=True, exist_ok=True)
        victim = self.fixture.root / "marker-victim"
        victim.write_text("keep\n")
        self.fixture.marker.symlink_to(victim)

        self.assertTrue(self.guard._maintenance_active())

        self.assertTrue(self.fixture.marker.is_symlink())
        self.assertEqual(victim.read_text(), "keep\n")

    def test_steam_inactive_never_auto_recovers(self):
        lease, _payload, active, heartbeat = self.populate_generation()
        self.fixture.systemd.steam_is_running = False
        made_stale = False

        def after_sleep(now):
            nonlocal made_stale
            if now >= 1001 and not made_stale:
                made_stale = True
                self.write_heartbeat(heartbeat, 900)
            if now >= 1010:
                active.unlink(missing_ok=True)

        self.fixture.clock.on_sleep = after_sleep

        result = self.guard.run(lease)

        self.assertEqual(result["action"], "clean-unload")
        self.assertEqual(self.recording.restart_calls, 0)

    def test_recent_same_boot_cooldown_prevents_restart_and_exits(self):
        lease, _payload, _active, heartbeat = self.populate_generation()
        self.guard.cooldown_path.write_text(json.dumps({
            "version": 1,
            "boot_id": self.BOOT_ID,
            "attempted_monotonic": 950,
        }) + "\n")
        made_stale = False

        def after_sleep(now):
            nonlocal made_stale
            if now >= 1001 and not made_stale:
                made_stale = True
                self.write_heartbeat(heartbeat, 900)

        self.fixture.clock.on_sleep = after_sleep

        result = self.guard.run(lease)

        self.assertEqual(result["action"], "cooldown")
        self.assertEqual(self.recording.restart_calls, 0)

    def test_reused_owner_pid_is_not_signalled_after_loader_change(self):
        lease, _payload, _active, _heartbeat = self.populate_generation()
        transitioned = False

        def after_sleep(now):
            nonlocal transitioned
            if now >= 1001 and not transitioned:
                transitioned = True
                self.fixture.remove(100)
                (self.fixture.proc / "101" / "stat").write_text(
                    stat_text(101, 1, 9001, "unrelated"))
                self.populate_generation(
                    self.NEXT_TOKEN, 200, 201,
                    loader_started=2000, owner_started=2001, current=True,
                    ready=True)

        self.fixture.clock.on_sleep = after_sleep

        result = self.guard.run(lease)

        self.assertEqual(result["action"], "replacement-lease")
        self.assertEqual(self.fixture.signals, [])
        self.assertTrue((self.fixture.proc / "101").exists())

    def test_replacement_without_ready_marker_is_not_accepted(self):
        lease, _payload, _active, _heartbeat = self.populate_generation()
        transitioned = False

        def after_sleep(now):
            nonlocal transitioned
            if now >= 1001 and not transitioned:
                transitioned = True
                self.fixture.remove(100)
                self.populate_generation(
                    self.NEXT_TOKEN, 200, 201,
                    loader_started=2000, owner_started=2001, current=True,
                    ready=False)

        def send(pid, requested):
            self.fixture.signals.append((pid, requested))
            if requested == signal.SIGTERM and (self.fixture.proc / str(pid)).exists():
                self.fixture.remove(pid)

        self.fixture.clock.on_sleep = after_sleep
        self.fixture.table.send_signal = send

        result = self.guard.run(lease)

        self.assertEqual(result["action"], "restarted")
        self.assertEqual(result["reason"], "replacement-not-ready")
        self.assertEqual(self.recording.restart_calls, 1)

    def test_old_current_is_ignored_until_own_lease_is_published(self):
        self.populate_generation()
        lease, payload, active, _heartbeat = self.populate_generation(
            self.NEXT_TOKEN, 200, 201,
            loader_started=2000, owner_started=2001, current=False)
        current = self.runtime / "plugin-lifecycle-current.json"
        ready = self.runtime / f"plugin-lifecycle-{self.NEXT_TOKEN}.ready"
        activated = False
        saw_ready_with_old_current = False

        def after_sleep(now):
            nonlocal activated, saw_ready_with_old_current
            if now >= 1001 and not activated:
                self.assertTrue(ready.exists())
                old = json.loads(current.read_text())
                saw_ready_with_old_current = old["token"] == self.TOKEN
                current.write_text(json.dumps(payload) + "\n")
                activated = True
            if now >= 1003:
                active.unlink(missing_ok=True)

        self.fixture.clock.on_sleep = after_sleep

        result = self.guard.run(lease)

        self.assertTrue(saw_ready_with_old_current)
        self.assertTrue(activated)
        self.assertEqual(result["action"], "clean-unload")
        self.assertFalse(ready.exists())
        self.assertEqual(self.recording.restart_calls, 0)

    def test_guard_exits_if_own_current_is_never_published(self):
        lease, _payload, _active, _heartbeat = self.populate_generation(
            current=False)
        ready = self.runtime / f"plugin-lifecycle-{self.TOKEN}.ready"

        with self.assertRaisesRegex(
                recovery.RecoveryError, "not published as current"):
            self.guard.run(lease)

        self.assertGreaterEqual(self.fixture.clock.value, 1010)
        self.assertFalse(ready.exists())
        self.assertEqual(self.recording.restart_calls, 0)

    def test_invalid_initial_heartbeat_fails_before_ready(self):
        lease, _payload, _active, heartbeat = self.populate_generation()
        heartbeat.write_text("malformed\n")
        ready = self.runtime / f"plugin-lifecycle-{self.TOKEN}.ready"

        with self.assertRaisesRegex(
                recovery.RecoveryError, "could not validate"):
            self.guard.run(lease)

        self.assertFalse(ready.exists())
        self.assertEqual(self.recording.restart_calls, 0)

    def test_heartbeat_is_monotonic_content_not_wall_clock_or_mtime(self):
        lease_path, _payload, _active, heartbeat = self.populate_generation()
        lease = self.guard._load_lease(lease_path)
        self.write_heartbeat(heartbeat)

        for mtime, wall_time in ((1, -10_000), (2_000_000_000, 10**20)):
            with self.subTest(mtime=mtime, wall_time=wall_time):
                os.utime(heartbeat, (mtime, mtime))
                with mock.patch.object(
                        recovery.time, "time", return_value=wall_time):
                    self.assertTrue(self.guard._heartbeat_fresh(lease))

        heartbeat.write_text("not-a-number\n")
        self.assertFalse(self.guard._heartbeat_fresh(lease))
        self.write_heartbeat(heartbeat, self.fixture.clock.value + 1)
        self.assertFalse(self.guard._heartbeat_fresh(lease))

    def test_cooldown_uses_monotonic_time_only(self):
        self.populate_generation()
        self.guard.cooldown_path.write_text(json.dumps({
            "version": 1,
            "boot_id": self.BOOT_ID,
            "attempted_monotonic": 950,
        }) + "\n")

        for wall_time in (-10_000, 10**20):
            with mock.patch.object(
                    recovery.time, "time", return_value=wall_time):
                self.assertTrue(self.guard._cooldown_active())
        self.fixture.clock.value = 1200
        with mock.patch.object(recovery.time, "time", return_value=-10_000):
            self.assertFalse(self.guard._cooldown_active())

    def test_future_same_boot_cooldown_is_conservatively_inhibited(self):
        self.populate_generation()
        self.guard.cooldown_path.write_text(json.dumps({
            "version": 1,
            "boot_id": self.BOOT_ID,
            "attempted_monotonic": 1010,
        }) + "\n")

        # A same-boot value ahead of the reader cannot bypass the cooldown.
        self.assertTrue(self.guard._cooldown_active())
        # It naturally expires after the recorded point plus the fixed window.
        self.fixture.clock.value = 1131
        self.assertFalse(self.guard._cooldown_active())

    def test_production_guard_waits_cover_heartbeat_and_slow_startup(self):
        self.assertGreater(recovery.HEARTBEAT_RECHECK_SECONDS, 5.0)
        self.assertGreaterEqual(recovery.REPLACEMENT_WAIT_SECONDS, 20.0)
        self.assertGreaterEqual(recovery.ACTIVATION_WAIT_SECONDS, 10.0)

    def test_lease_path_and_token_cannot_be_caller_selected(self):
        lease, payload, _active, _heartbeat = self.populate_generation()
        outside = self.fixture.root / lease.name
        outside.write_text(json.dumps(payload))
        wrong_name = self.runtime / f"plugin-lifecycle-{'c' * 32}.json"
        wrong_name.write_text(json.dumps(payload))

        with self.assertRaisesRegex(recovery.RecoveryError, "outside"):
            self.guard.run(outside)
        with self.assertRaisesRegex(recovery.RecoveryError, "does not match"):
            self.guard.run(wrong_name)

        self.assertEqual(self.recording.restart_calls, 0)

    def test_cli_accepts_only_guard_plus_lease_shape(self):
        lease, _payload, _active, _heartbeat = self.populate_generation()
        expected = {"action": "clean-unload", "restarted": False}

        with mock.patch.object(recovery.os, "geteuid", return_value=0), \
                mock.patch.object(
                    recovery.LifecycleGuard, "run", return_value=expected) as run, \
                mock.patch("builtins.print"):
            result = recovery.cli(["guard", str(lease)])

        self.assertEqual(result, 0)
        run.assert_called_once_with(lease)

    def test_retire_current_removes_only_markers_and_pointer_idempotently(self):
        lease, payload, active, heartbeat = self.populate_generation(ready=True)
        current = self.runtime / "plugin-lifecycle-current.json"
        ready = self.runtime / f"plugin-lifecycle-{self.TOKEN}.ready"

        first = self.guard.retire_current()
        second = self.guard.retire_current()

        self.assertTrue(first["retired"])
        self.assertEqual(first["token"], self.TOKEN)
        self.assertTrue(first["active_removed"])
        self.assertTrue(first["heartbeat_removed"])
        self.assertTrue(first["ready_removed"])
        self.assertTrue(first["current_removed"])
        self.assertFalse(active.exists())
        self.assertFalse(heartbeat.exists())
        self.assertFalse(ready.exists())
        self.assertFalse(current.exists())
        self.assertTrue(lease.exists())
        self.assertEqual(json.loads(lease.read_text()), payload)
        self.assertEqual(second, {
            "action": "retired-current",
            "retired": False,
            "token": "",
        })

    def test_retire_malformed_current_pointer_unlinks_nothing(self):
        lease, _payload, active, heartbeat = self.populate_generation(ready=True)
        ready = self.runtime / f"plugin-lifecycle-{self.TOKEN}.ready"
        current = self.runtime / "plugin-lifecycle-current.json"
        current.write_text("{not-json\n")

        with self.assertRaisesRegex(recovery.RecoveryError, "malformed"):
            self.guard.retire_current()

        self.assertTrue(current.exists())
        self.assertTrue(lease.exists())
        self.assertTrue(active.exists())
        self.assertTrue(heartbeat.exists())
        self.assertTrue(ready.exists())

    def test_retire_traversal_token_cannot_unlink_any_path(self):
        lease, payload, active, heartbeat = self.populate_generation(ready=True)
        ready = self.runtime / f"plugin-lifecycle-{self.TOKEN}.ready"
        current = self.runtime / "plugin-lifecycle-current.json"
        victim = self.fixture.root / "victim"
        victim.write_text("keep\n")
        payload["token"] = "../../victim"
        current.write_text(json.dumps(payload) + "\n")

        with self.assertRaisesRegex(recovery.RecoveryError, "token is invalid"):
            self.guard.retire_current()

        self.assertEqual(victim.read_text(), "keep\n")
        self.assertTrue(current.exists())
        self.assertTrue(lease.exists())
        self.assertTrue(active.exists())
        self.assertTrue(heartbeat.exists())
        self.assertTrue(ready.exists())

    def test_retire_requires_current_and_generation_lease_to_match(self):
        lease, payload, active, heartbeat = self.populate_generation(ready=True)
        ready = self.runtime / f"plugin-lifecycle-{self.TOKEN}.ready"
        changed = json.loads(json.dumps(payload))
        changed["owner"]["start_time_ticks"] += 1
        lease.write_text(json.dumps(changed) + "\n")

        with self.assertRaisesRegex(recovery.RecoveryError, "does not match"):
            self.guard.retire_current()

        self.assertTrue(
            (self.runtime / "plugin-lifecycle-current.json").exists())
        self.assertTrue(active.exists())
        self.assertTrue(heartbeat.exists())
        self.assertTrue(ready.exists())

    def test_retire_current_is_serialized_with_concurrent_publication(self):
        old_lease, _old_payload, old_active, old_heartbeat = (
            self.populate_generation(ready=True))
        old_ready = self.runtime / f"plugin-lifecycle-{self.TOKEN}.ready"
        self.fixture.lock.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.fixture.lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

            with self.assertRaisesRegex(
                    recovery.RecoveryError, "publication is in progress"):
                self.guard.retire_current()

            self.assertTrue(old_lease.exists())
            self.assertTrue(old_active.exists())
            self.assertTrue(old_heartbeat.exists())
            self.assertTrue(old_ready.exists())

            # Model the concurrent publisher completing while it still owns
            # the shared lock.  Retirement may only observe this generation
            # after the publisher releases the lock.
            new_lease, _new_payload, new_active, new_heartbeat = (
                self.populate_generation(
                    self.NEXT_TOKEN, 200, 201,
                    loader_started=2000, owner_started=2001,
                    current=True, ready=True))
            new_ready = (
                self.runtime /
                f"plugin-lifecycle-{self.NEXT_TOKEN}.ready")
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

        result = self.guard.retire_current()

        self.assertEqual(result["token"], self.NEXT_TOKEN)
        self.assertTrue(result["current_removed"])
        self.assertTrue(old_lease.exists())
        self.assertTrue(old_active.exists())
        self.assertTrue(old_heartbeat.exists())
        self.assertTrue(old_ready.exists())
        self.assertTrue(new_lease.exists())
        self.assertFalse(new_active.exists())
        self.assertFalse(new_heartbeat.exists())
        self.assertFalse(new_ready.exists())

    def test_cli_retire_current_has_no_path_argument(self):
        expected = {
            "action": "retired-current", "retired": False, "token": "",
        }
        with mock.patch.object(recovery.os, "geteuid", return_value=0), \
                mock.patch.object(
                    recovery.LifecycleGuard, "retire_current",
                    return_value=expected) as retire, \
                mock.patch("builtins.print"):
            result = recovery.cli(["retire-current"])
            rejected = recovery.cli(["retire-current", "/tmp/not-allowed"])

        self.assertEqual(result, 0)
        self.assertEqual(rejected, 1)
        retire.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
