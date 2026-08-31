import asyncio
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class RuntimeRestoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "rke_runtime_restore", ROOT / "runtime-restore.py")
        cls.restore_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.restore_module)

    def run_restore(self, root, control=None, gpu=None, legacy_charging=None,
                    fan_applied=False, boot_id=None):
        marker = root / "runtime-session.active"
        state = root / "runtime-session.json"
        canonical = root / "canonical-fancontrol.conf"
        target = root / "fancontrol.conf"
        canonical.write_text("SPEEDS=(255 0)\nTEMPS=(85000 0)\n")
        marker.write_text("123\n")
        state.write_text(json.dumps({
            "version": 1,
            "boot_id": boot_id or Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "controls": {
                "cpu": [control] if control else [],
                "gpu": gpu,
                "scheduler": None,
                "charging": legacy_charging,
                "fan": {"applied": fan_applied},
            },
        }))
        result = subprocess.run(
            [sys.executable, str(ROOT / "runtime-restore.py"), str(marker),
             str(state), str(canonical), str(target)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False)
        return result, marker, state

    def test_restores_owned_cpu_values_including_native_boost_ceiling(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy = root / "policy6"
            policy.mkdir()
            (policy / "scaling_governor").write_text("performance")
            (policy / "scaling_min_freq").write_text("1000")
            (policy / "scaling_max_freq").write_text("4089")
            control = {
                "id": "6", "path": str(policy),
                "baseline": {"governor": "schedutil", "minimum": 300, "maximum": 4320},
                "applied": {"governor": "performance", "minimum": 1000, "maximum": 4089},
            }

            result, marker, state = self.run_restore(root, control)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((policy / "scaling_governor").read_text(), "schedutil")
            self.assertEqual((policy / "scaling_min_freq").read_text(), "300")
            self.assertEqual((policy / "scaling_max_freq").read_text(), "4320")
            self.assertFalse(marker.exists())
            self.assertFalse(state.exists())

    def test_preserves_values_changed_after_rke_applied(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy = root / "policy6"
            policy.mkdir()
            (policy / "scaling_governor").write_text("ondemand")
            (policy / "scaling_min_freq").write_text("1000")
            (policy / "scaling_max_freq").write_text("3500")
            control = {
                "id": "6", "path": str(policy),
                "baseline": {"governor": "schedutil", "minimum": 300, "maximum": 4320},
                "applied": {"governor": "performance", "minimum": 1000, "maximum": 4089},
            }

            result, _, _ = self.run_restore(root, control)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((policy / "scaling_governor").read_text(), "ondemand")
            self.assertEqual((policy / "scaling_min_freq").read_text(), "300")
            self.assertEqual((policy / "scaling_max_freq").read_text(), "3500")

    def test_restores_gpu_and_fan_but_ignores_legacy_charging_ownership(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gpu_path = root / "gpu"
            gpu_path.mkdir()
            (gpu_path / "governor").write_text("performance")
            (gpu_path / "min_freq").write_text("200")
            (gpu_path / "max_freq").write_text("900")
            charging_path = root / "charge_behaviour"
            charging_path.write_text("inhibit-charge")
            gpu = {
                "path": str(gpu_path),
                "baseline": {"governor": "msm-adreno-tz", "minimum": 100, "maximum": 800},
                "applied": {"governor": "performance", "minimum": 200, "maximum": 900},
            }
            legacy_charging = {
                "path": str(charging_path), "baseline": "auto",
                "applied": "inhibit-charge",
            }

            result, _, _ = self.run_restore(
                root, gpu=gpu, legacy_charging=legacy_charging,
                fan_applied=True)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((gpu_path / "governor").read_text(), "msm-adreno-tz")
            self.assertEqual((gpu_path / "min_freq").read_text(), "100")
            self.assertEqual((gpu_path / "max_freq").read_text(), "800")
            self.assertEqual(charging_path.read_text(), "inhibit-charge")
            self.assertEqual(
                (root / "fancontrol.conf").read_text(),
                "SPEEDS=(255 0)\nTEMPS=(85000 0)\n")

    def test_fan_restore_reloads_custom_profile_without_get_setting_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canonical = root / "canonical-fancontrol.conf"
            target = root / "fancontrol.conf"
            config = root / "system.cfg"
            canonical.write_text("SPEEDS=(255 0)\nTEMPS=(85000 0)\n")
            target.write_text("SPEEDS=(51 0)\nTEMPS=(55000 0)\n")
            config.write_text("audio.volume=80\ncooling.profile=custom\n")

            with mock.patch.object(
                    self.restore_module, "SYSTEM_CONFIG", config), \
                    mock.patch.object(
                        self.restore_module.shutil, "which", return_value=None), \
                    mock.patch.object(
                        self.restore_module, "reload_fancontrol") as reload_fan:
                self.restore_module.restore_fan_curve(canonical, target)

            self.assertEqual(target.read_text(), canonical.read_text())
            reload_fan.assert_called_once_with()

    def test_skips_runtime_sysfs_values_from_an_older_boot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy = root / "policy0"
            policy.mkdir()
            (policy / "scaling_governor").write_text("performance")
            (policy / "scaling_min_freq").write_text("1000")
            (policy / "scaling_max_freq").write_text("2000")
            control = {
                "id": "0", "path": str(policy),
                "baseline": {"governor": "schedutil", "minimum": 100, "maximum": 2200},
                "applied": {"governor": "performance", "minimum": 1000, "maximum": 2000},
            }

            result, marker, _ = self.run_restore(
                root, control, boot_id="different-boot")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((policy / "scaling_governor").read_text(), "performance")
            self.assertEqual((policy / "scaling_max_freq").read_text(), "2000")
            self.assertFalse(marker.exists())


class GameWatchRuntimeSessionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.setdefault("decky", types.SimpleNamespace(
            logger=types.SimpleNamespace(
                info=lambda *_: None, warning=lambda *_: None,
                error=lambda *_: None)))
        import main
        cls.main = main

    def plugin(self, root, appid="123"):
        plugin = self.main.Plugin.__new__(self.main.Plugin)
        plugin.runtime_marker = root / "runtime-session.active"
        plugin.active_appid = appid
        plugin.active_preset = self.main.DEFAULT_PRESET
        plugin.gpu_fdinfo_paths = []
        plugin.gpu_fdinfo_refresh = 0.0
        plugin.last_gpu_sample = None
        plugin.startup_rgb_pending = False
        profile = {"profile": "steam-default"}
        plugin._load = mock.Mock(return_value={
            "presets": {self.main.DEFAULT_PRESET: profile},
            "steam_default": self.main.DEFAULT_PRESET,
            "game_profiles": {},
        })
        plugin._apply = mock.Mock(return_value=True)
        plugin._require_mutations_allowed = mock.Mock(return_value=True)
        plugin._run_hardware_mutation = (
            lambda function, *arguments: function(*arguments))
        plugin._install_status = mock.Mock(return_value={"active": False})
        plugin._plugin_conflict_state = mock.Mock(return_value={
            "blocked": False,
        })
        plugin._steam_scope_pids = mock.Mock(return_value=(100, 101))
        plugin._detect_steam_app = mock.Mock(return_value=appid)
        return plugin, profile

    async def test_same_appid_reapplies_when_runtime_marker_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            plugin, profile = self.plugin(Path(temporary))

            async def inline_to_thread(function, *arguments):
                return function(*arguments)

            with mock.patch.object(
                    self.main, "_get_setting", return_value="quiet"), \
                    mock.patch.object(
                        self.main.asyncio, "to_thread", new=inline_to_thread):
                result = await plugin.activate_game("123")

        self.assertEqual(result, {
            "applied": True, "preset": self.main.DEFAULT_PRESET,
        })
        plugin._apply.assert_called_once_with(profile)

    async def test_same_appid_skips_when_runtime_session_is_still_active(self):
        with tempfile.TemporaryDirectory() as temporary:
            plugin, _ = self.plugin(Path(temporary))
            plugin.runtime_marker.write_text("active\n")

            async def inline_to_thread(function, *arguments):
                return function(*arguments)

            with mock.patch.object(
                    self.main, "_get_setting", return_value="quiet"), \
                    mock.patch.object(
                        self.main.asyncio, "to_thread", new=inline_to_thread):
                result = await plugin.activate_game("123")

        self.assertEqual(result, {
            "applied": False, "preset": self.main.DEFAULT_PRESET,
        })
        plugin._apply.assert_not_called()

    async def test_new_appid_is_published_only_after_successful_apply(self):
        with tempfile.TemporaryDirectory() as temporary:
            plugin, _ = self.plugin(Path(temporary), appid="123")
            plugin.active_preset = "Old"
            plugin.gpu_fdinfo_paths = [Path("/proc/fake/fdinfo/4")]
            plugin.gpu_fdinfo_refresh = 42.0
            plugin.last_gpu_sample = (10, 20)
            old_profile = {"profile": "old"}
            new_profile = {"profile": "new"}
            plugin._load.return_value = {
                "presets": {"Old": old_profile, "New": new_profile},
                "steam_default": "Old",
                "game_profiles": {"456": "New"},
            }
            observed_during_apply = []

            def apply(_profile):
                observed_during_apply.append(
                    (plugin.active_appid, plugin.active_preset,
                     list(plugin.gpu_fdinfo_paths)))
                return True

            plugin._apply.side_effect = apply

            async def inline_to_thread(function, *arguments):
                return function(*arguments)

            with mock.patch.object(
                    self.main, "_get_setting", return_value="quiet"), \
                    mock.patch.object(
                        self.main.asyncio, "to_thread", new=inline_to_thread):
                result = await plugin.activate_game("456")

        self.assertEqual(observed_during_apply, [
            ("123", "Old", [Path("/proc/fake/fdinfo/4")]),
        ])
        self.assertEqual(result, {"applied": True, "preset": "New"})
        self.assertEqual(plugin.active_appid, "456")
        self.assertEqual(plugin.active_preset, "New")
        self.assertEqual(plugin.gpu_fdinfo_paths, [])
        self.assertEqual(plugin.gpu_fdinfo_refresh, 0.0)
        self.assertIsNone(plugin.last_gpu_sample)

    async def test_failed_apply_does_not_publish_new_appid_or_reset_gpu_sample(self):
        with tempfile.TemporaryDirectory() as temporary:
            plugin, _ = self.plugin(Path(temporary), appid="123")
            plugin.active_preset = "Old"
            plugin.gpu_fdinfo_paths = [Path("/proc/fake/fdinfo/4")]
            plugin.gpu_fdinfo_refresh = 42.0
            plugin.last_gpu_sample = (10, 20)
            plugin._load.return_value = {
                "presets": {
                    "Old": {"profile": "old"},
                    "New": {"profile": "new"},
                },
                "steam_default": "Old",
                "game_profiles": {"456": "New"},
            }
            plugin._apply.side_effect = RuntimeError("apply failed")

            async def inline_to_thread(function, *arguments):
                return function(*arguments)

            with mock.patch.object(
                    self.main, "_get_setting", return_value="quiet"), \
                    mock.patch.object(
                        self.main.asyncio, "to_thread", new=inline_to_thread):
                with self.assertRaisesRegex(RuntimeError, "apply failed"):
                    await plugin.activate_game("456")

        self.assertEqual(plugin.active_appid, "123")
        self.assertEqual(plugin.active_preset, "Old")
        self.assertEqual(
            plugin.gpu_fdinfo_paths, [Path("/proc/fake/fdinfo/4")])
        self.assertEqual(plugin.gpu_fdinfo_refresh, 42.0)
        self.assertEqual(plugin.last_gpu_sample, (10, 20))

    async def test_watcher_reapplies_same_appid_after_marker_disappears(self):
        with tempfile.TemporaryDirectory() as temporary:
            plugin, profile = self.plugin(Path(temporary))
            plugin.runtime_marker.write_text("active\n")
            sleep_calls = 0

            async def inline_to_thread(function, *arguments):
                return function(*arguments)

            async def advance_then_stop(_seconds):
                nonlocal sleep_calls
                sleep_calls += 1
                if sleep_calls == 1:
                    plugin.runtime_marker.unlink()
                    return
                raise asyncio.CancelledError

            with mock.patch.object(
                    self.main, "_get_setting", return_value="quiet"), \
                    mock.patch.object(
                        self.main.asyncio, "to_thread", new=inline_to_thread), \
                    mock.patch.object(
                        self.main.asyncio, "sleep", new=advance_then_stop):
                with self.assertRaises(asyncio.CancelledError):
                    await plugin._game_watch_loop()

        plugin._apply.assert_called_once_with(profile)

    async def test_steam_transition_applies_default_when_appid_is_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            plugin, profile = self.plugin(Path(temporary), appid="")

            async def inline_to_thread(function, *arguments):
                return function(*arguments)

            async def stop_after_first_tick(_seconds):
                raise asyncio.CancelledError

            with mock.patch.object(
                    self.main, "_get_setting", return_value="quiet"), \
                    mock.patch.object(
                        self.main.asyncio, "to_thread", new=inline_to_thread), \
                    mock.patch.object(
                        self.main.asyncio, "sleep", new=stop_after_first_tick):
                with self.assertRaises(asyncio.CancelledError):
                    await plugin._game_watch_loop()

        plugin._apply.assert_called_once_with(profile)
        self.assertEqual(plugin.active_appid, "")
        self.assertEqual(plugin.active_preset, self.main.DEFAULT_PRESET)

    async def test_watcher_reuses_detection_while_scope_pids_are_stable(self):
        with tempfile.TemporaryDirectory() as temporary:
            plugin, _ = self.plugin(Path(temporary))
            plugin.runtime_marker.write_text("active\n")
            sleep_calls = 0

            async def inline_to_thread(function, *arguments):
                return function(*arguments)

            async def stop_after_second_tick(_seconds):
                nonlocal sleep_calls
                sleep_calls += 1
                if sleep_calls >= 2:
                    raise asyncio.CancelledError

            with mock.patch.object(
                    self.main, "_get_setting", return_value="quiet"), \
                    mock.patch.object(
                        self.main.asyncio, "to_thread", new=inline_to_thread), \
                    mock.patch.object(
                        self.main.asyncio, "sleep", new=stop_after_second_tick):
                with self.assertRaises(asyncio.CancelledError):
                    await plugin._game_watch_loop()

        self.assertEqual(plugin._steam_scope_pids.call_count, 2)
        plugin._detect_steam_app.assert_called_once_with((100, 101))

    def test_steam_scope_pids_unions_all_available_cgroup_hierarchies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            systemd = root / "systemd.procs"
            unified = root / "unified.procs"
            pids = root / "pids.procs"
            systemd.write_text("")
            unified.write_text("202\ninvalid\n")
            pids.write_text("101\n202\n")
            plugin = self.main.Plugin.__new__(self.main.Plugin)

            with mock.patch.object(
                    self.main, "STEAM_SCOPE_CGROUPS",
                    (systemd, unified, pids)):
                result = plugin._steam_scope_pids()

        self.assertEqual(result, (101, 202))

    async def test_steam_closed_idle_tick_skips_install_and_manifest_scans(self):
        with tempfile.TemporaryDirectory() as temporary:
            plugin, _ = self.plugin(Path(temporary))
            plugin._steam_scope_pids.return_value = ()

            async def inline_to_thread(function, *arguments):
                return function(*arguments)

            async def stop_after_idle_tick(_seconds):
                raise asyncio.CancelledError

            with mock.patch.object(
                    self.main.asyncio, "to_thread", new=inline_to_thread), \
                    mock.patch.object(
                        self.main.asyncio, "sleep", new=stop_after_idle_tick):
                with self.assertRaises(asyncio.CancelledError):
                    await plugin._game_watch_loop()

        plugin._install_status.assert_not_called()
        plugin._plugin_conflict_state.assert_not_called()
        plugin._detect_steam_app.assert_not_called()
        self.assertEqual(plugin.active_appid, "")
        self.assertEqual(plugin.active_preset, self.main.DEFAULT_PRESET)


class CpuBoostDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.setdefault("decky", types.SimpleNamespace(
            logger=types.SimpleNamespace(info=lambda *_: None, error=lambda *_: None)))
        import main
        cls.main = main

    def test_discovers_each_policys_device_specific_boost_ceiling(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "boost").write_text("1")
            first = root / "policy0"
            first.mkdir()
            (first / "scaling_available_frequencies").write_text("100 200")
            (first / "scaling_boost_frequencies").write_text("250 300")
            (first / "boost").write_text("1")
            (first / "cpuinfo_max_freq").write_text("300")
            (first / "affected_cpus").write_text("0 1")
            (first / "scaling_available_governors").write_text("performance schedutil")
            second = root / "policy2"
            second.mkdir()
            (second / "scaling_available_frequencies").write_text("150 350")
            (second / "cpuinfo_max_freq").write_text("400")
            (second / "affected_cpus").write_text("2")
            (second / "scaling_available_governors").write_text("schedutil")

            with mock.patch.object(self.main, "CPU_ROOT", root):
                policies = self.main._cpu_capabilities()

            self.assertEqual(policies[0]["maximum_frequencies"], [100, 200, 250, 300])
            self.assertEqual(policies[1]["boost_frequencies"], [400])
            self.assertEqual(policies[1]["maximum_frequencies"], [150, 350, 400])


class RkeCpuTelemetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.setdefault("decky", types.SimpleNamespace(
            logger=types.SimpleNamespace(info=lambda *_: None, error=lambda *_: None)))
        import main
        cls.main = main

    def plugin(self, session="monitor-a", generation=1):
        plugin = self.main.Plugin.__new__(self.main.Plugin)
        plugin.monitor_lock = threading.RLock()
        plugin.monitor_session = session
        plugin.monitor_generation = generation
        plugin.monitor_revision = 0
        plugin.monitor_charging_valid = False
        plugin.monitor_bypass_active = False
        plugin.battery_discharge_ema = None
        plugin.battery_discharge_samples = 0
        plugin.battery_discharge_last_sample = 0.0
        plugin.last_rke_cpu_sample = None
        plugin.last_rke_cpu_percent = None
        plugin.rke_cpu_sample_interval = 0
        plugin.lifecycle_guard_unit = ""
        plugin.lifecycle_guard_identity = None
        plugin.runtime_guard_unit = ""
        plugin.runtime_guard_identity = None
        plugin.backend_identity = {"pid": 999, "start_time_ticks": 9999}
        return plugin

    def test_top_style_usage_combines_backend_and_live_child(self):
        plugin = self.plugin()
        samples = [
            {(999, 9999): 1.0},
            {(999, 9999): 1.72},
        ]
        with mock.patch.object(
                 plugin, "_rke_cpu_snapshot", side_effect=samples), \
             mock.patch.object(self.main.time, "monotonic",
                               side_effect=[10.0, 11.0]):
            first = plugin._sample_rke_cpu_percent("monitor-a", 1)
            second = plugin._sample_rke_cpu_percent("monitor-a", 1)

        self.assertIsNone(first)
        self.assertAlmostEqual(second, 72.0)

    def test_usage_allows_more_than_one_busy_core(self):
        plugin = self.plugin()
        samples = [
            {(999, 9999): 1.0},
            {(999, 9999): 2.5},
        ]
        with mock.patch.object(
                 plugin, "_rke_cpu_snapshot", side_effect=samples), \
             mock.patch.object(self.main.time, "monotonic",
                               side_effect=[20.0, 21.0]):
            self.assertIsNone(
                plugin._sample_rke_cpu_percent("monitor-a", 1))
            self.assertAlmostEqual(
                plugin._sample_rke_cpu_percent("monitor-a", 1), 150.0)

    def test_short_helper_between_polls_is_retained_by_root_counter(self):
        plugin = self.plugin()
        samples = [
            {(999, 9999): 1.0},
            {(999, 9999): 1.25},
        ]
        with mock.patch.object(
                 plugin, "_rke_cpu_snapshot", side_effect=samples), \
             mock.patch.object(self.main.time, "monotonic",
                               side_effect=[5.0, 6.0]):
            self.assertIsNone(
                plugin._sample_rke_cpu_percent("monitor-a", 1))
            self.assertAlmostEqual(
                plugin._sample_rke_cpu_percent("monitor-a", 1), 25.0)

    def test_process_tree_snapshot_is_sampled_at_two_second_cadence(self):
        plugin = self.plugin()
        plugin.rke_cpu_sample_interval = 2.0
        with mock.patch.object(
                plugin, "_rke_cpu_snapshot", side_effect=[
                    {(999, 9999): 1.0}, {(999, 9999): 1.2},
                ]) as snapshot, mock.patch.object(
                    self.main.time, "monotonic",
                    side_effect=[10.0, 11.0, 12.0, 13.0]):
            self.assertIsNone(
                plugin._sample_rke_cpu_percent("monitor-a", 1))
            self.assertIsNone(
                plugin._sample_rke_cpu_percent("monitor-a", 1))
            self.assertAlmostEqual(
                plugin._sample_rke_cpu_percent("monitor-a", 1), 10.0)
            self.assertAlmostEqual(
                plugin._sample_rke_cpu_percent("monitor-a", 1), 10.0)

        self.assertEqual(snapshot.call_count, 2)

    def test_usage_combines_exact_lifecycle_and_runtime_guards(self):
        plugin = self.plugin()
        plugin.lifecycle_guard_identity = {
            "pid": 111, "start_time_ticks": 1111,
        }
        plugin.runtime_guard_identity = {
            "pid": 222, "start_time_ticks": 2222,
        }
        process_snapshots = [
            {(999, 9999): 1.0, (1000, 10000): 0.1},
            {(111, 1111): 0.2},
            {(222, 2222): 0.3},
        ]
        with mock.patch.object(
                self.main, "_process_tree_cpu_snapshot",
                side_effect=process_snapshots) as process_tree:
            self.assertEqual(plugin._rke_cpu_snapshot(), {
                (999, 9999): 1.1,
                (111, 1111): 0.2,
                (222, 2222): 0.3,
            })

        self.assertEqual(process_tree.call_args_list, [
            mock.call(plugin.backend_identity),
            mock.call(plugin.lifecycle_guard_identity),
            mock.call(plugin.runtime_guard_identity),
        ])

    def test_exact_process_cpu_time_rejects_pid_reuse_and_malformed_stat(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc_root = Path(temporary)
            process_root = proc_root / "432"
            process_root.mkdir()
            fields = ["0"] * 30
            fields[0] = "S"
            fields[1] = "1"
            fields[11:15] = ["10", "20", "30", "40"]
            fields[19] = "9876"
            stat_path = process_root / "stat"
            stat_path.write_text(
                "432 (RK-E guard worker) " + " ".join(fields) + "\n")
            identity = {"pid": 432, "start_time_ticks": 9876}

            with mock.patch.object(
                    self.main.os, "sysconf", return_value=100):
                self.assertEqual(
                    self.main._process_cpu_seconds(identity, proc_root), 1.0)
                self.assertIsNone(self.main._process_cpu_seconds(
                    {"pid": 432, "start_time_ticks": 9877}, proc_root))
                stat_path.write_text("not a process stat\n")
                self.assertIsNone(
                    self.main._process_cpu_seconds(identity, proc_root))

    def test_process_tree_snapshot_follows_exact_live_children(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc_root = Path(temporary)

            def create_process(pid, parent, start, user, system, children=""):
                root = proc_root / str(pid)
                (root / "task" / str(pid)).mkdir(parents=True)
                fields = ["0"] * 30
                fields[0] = "S"
                fields[1] = str(parent)
                fields[11] = str(user)
                fields[12] = str(system)
                fields[19] = str(start)
                (root / "stat").write_text(
                    f"{pid} (RK-E helper) " + " ".join(fields) + "\n")
                (root / "task" / str(pid) / "children").write_text(
                    children + "\n")

            create_process(10, 1, 100, 10, 5, "11")
            create_process(11, 10, 110, 7, 3, "12")
            create_process(12, 11, 120, 2, 3)
            with mock.patch.object(
                    self.main.os, "sysconf", return_value=100):
                snapshot = self.main._process_tree_cpu_snapshot(
                    {"pid": 10, "start_time_ticks": 100}, proc_root)

            self.assertEqual(snapshot, {
                (10, 100): 0.15,
                (11, 110): 0.1,
                (12, 120): 0.05,
            })

    def test_live_child_to_reaped_counter_handoff_is_counted_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc_root = Path(temporary)

            def write_stat(pid, parent, start, own, children):
                root = proc_root / str(pid)
                (root / "task" / str(pid)).mkdir(
                    parents=True, exist_ok=True)
                fields = ["0"] * 30
                fields[0] = "S"
                fields[1] = str(parent)
                fields[11] = str(own)
                fields[13] = str(children)
                fields[19] = str(start)
                (root / "stat").write_text(
                    f"{pid} (RK-E helper) " + " ".join(fields) + "\n")

            write_stat(10, 1, 100, 50, 10)
            write_stat(11, 10, 110, 20, 0)
            children_path = proc_root / "10" / "task" / "10" / "children"
            children_path.write_text("11\n")
            (proc_root / "11" / "task" / "11" / "children").write_text("\n")
            identity = {"pid": 10, "start_time_ticks": 100}

            with mock.patch.object(
                    self.main.os, "sysconf", return_value=100):
                live = sum(self.main._process_tree_cpu_snapshot(
                    identity, proc_root).values())
                # The child is now waited/reaped. Its 20 ticks transfer once
                # into the root's cumulative child counter.
                write_stat(10, 1, 100, 60, 30)
                children_path.write_text("\n")
                reaped = sum(self.main._process_tree_cpu_snapshot(
                    identity, proc_root).values())

            self.assertEqual(live, 0.8)
            self.assertEqual(reaped, 0.9)
            self.assertAlmostEqual(reaped - live, 0.1)

    def test_process_membership_changes_never_create_lifetime_spikes(self):
        plugin = self.plugin()
        samples = [
            {(999, 9999): 1.0, (111, 1111): 10.0},
            {(999, 9999): 1.1},
            {(999, 9999): 1.2, (111, 1111): 20.0},
            {(999, 9999): 1.3, (111, 1111): 20.1},
        ]
        with mock.patch.object(
                 plugin, "_rke_cpu_snapshot", side_effect=samples), \
             mock.patch.object(self.main.time, "monotonic",
                               side_effect=[1.0, 2.0, 3.0, 4.0]):
            self.assertIsNone(
                plugin._sample_rke_cpu_percent("monitor-a", 1))
            self.assertAlmostEqual(
                plugin._sample_rke_cpu_percent("monitor-a", 1), 10.0)
            self.assertAlmostEqual(
                plugin._sample_rke_cpu_percent("monitor-a", 1), 10.0)
            self.assertAlmostEqual(
                plugin._sample_rke_cpu_percent("monitor-a", 1), 20.0)

    def test_unresolved_expected_guard_makes_tracking_unavailable(self):
        plugin = self.plugin()
        self.assertTrue(plugin._rke_cpu_tracking_available())

        plugin.lifecycle_guard_unit = (
            "rke-plugin-lifecycle-guard-999-abcdef12")
        self.assertFalse(plugin._rke_cpu_tracking_available())

        plugin.lifecycle_guard_identity = {
            "pid": 111, "start_time_ticks": 1111,
        }
        self.assertTrue(plugin._rke_cpu_tracking_available())

    def test_runtime_guard_identity_is_captured_and_cleared(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = self.main.Plugin.__new__(self.main.Plugin)
            plugin.runtime_marker = root / "runtime-session.active"
            plugin.runtime_restore_request = (
                root / "runtime-session.active.restore-request")
            plugin.runtime_state_path = root / "runtime-session.json"
            plugin.runtime_guard_path = root / "runtime-restore-guard.sh"
            plugin.runtime_restore_path = root / "runtime-restore.py"
            plugin.canonical_fan_config = root / "canonical.conf"
            plugin.runtime_guard_unit = ""
            plugin.runtime_guard_identity = None
            plugin._install_runtime_restore_tools = mock.Mock()
            plugin._capture_runtime_state = mock.Mock(
                return_value={"version": 1, "controls": {}})
            guard_identity = {
                "pid": 765, "start_time_ticks": 4321, "parent_pid": 1,
            }

            with mock.patch.object(self.main, "_run") as run, \
                 mock.patch.object(
                     self.main, "_guard_unit_identity",
                     return_value=guard_identity
                 ) as identify, \
                 mock.patch.object(
                     self.main.time, "monotonic_ns", return_value=123456):
                plugin._ensure_runtime_session_locked({})

            expected_unit = (
                "rke-runtime-restore-guard-" +
                str(self.main.os.getpid()) + "-123456")
            self.assertEqual(plugin.runtime_guard_unit, expected_unit)
            self.assertEqual(plugin.runtime_guard_identity, guard_identity)
            identify.assert_called_once_with(expected_unit)
            self.assertEqual(run.call_args.args[0][0], "systemd-run")

            def restore(command, **_kwargs):
                plugin.runtime_marker.unlink()
                return "restored"

            with mock.patch.object(self.main, "_run", side_effect=restore):
                self.assertTrue(plugin._restore_runtime_session())
            self.assertEqual(plugin.runtime_guard_unit, "")
            self.assertIsNone(plugin.runtime_guard_identity)

    def test_stale_generation_cannot_replace_current_sampler(self):
        plugin = self.plugin(session="monitor-new", generation=2)
        plugin.last_rke_cpu_sample = (
            "monitor-new", 2, {(999, 9999): 4.0}, 30.0)
        previous = plugin.last_rke_cpu_sample
        with mock.patch.object(
                plugin, "_rke_cpu_snapshot",
                return_value={(999, 9999): 9.0}), \
             mock.patch.object(self.main.time, "monotonic", return_value=31.0):
            result = plugin._sample_rke_cpu_percent("monitor-old", 1)

        self.assertIsNone(result)
        self.assertEqual(plugin.last_rke_cpu_sample, previous)

    def test_nonpositive_elapsed_or_regressing_cpu_time_is_unavailable(self):
        plugin = self.plugin()
        samples = [
            {(999, 9999): 1.0},
            {(999, 9999): 1.5},
            {(999, 9999): 1.0},
        ]
        with mock.patch.object(
                 plugin, "_rke_cpu_snapshot", side_effect=samples), \
             mock.patch.object(self.main.time, "monotonic",
                               side_effect=[50.0, 50.0, 51.0]):
            self.assertIsNone(
                plugin._sample_rke_cpu_percent("monitor-a", 1))
            self.assertIsNone(
                plugin._sample_rke_cpu_percent("monitor-a", 1))
            self.assertIsNone(
                plugin._sample_rke_cpu_percent("monitor-a", 1))

    def test_regressed_tree_snapshot_cannot_rebound_into_false_spike(self):
        plugin = self.plugin()
        samples = [
            {(999, 9999): 10.0},
            {(999, 9999): 1.0},
            {(999, 9999): 10.2},
            {(999, 9999): 10.3},
        ]
        with mock.patch.object(
                 plugin, "_rke_cpu_snapshot", side_effect=samples), \
             mock.patch.object(self.main.time, "monotonic",
                               side_effect=[1.0, 2.0, 3.0, 4.0]):
            self.assertIsNone(
                plugin._sample_rke_cpu_percent("monitor-a", 1))
            self.assertIsNone(
                plugin._sample_rke_cpu_percent("monitor-a", 1))
            self.assertIsNone(
                plugin._sample_rke_cpu_percent("monitor-a", 1))
            self.assertAlmostEqual(
                plugin._sample_rke_cpu_percent("monitor-a", 1), 10.0)

    def test_monitor_activation_and_end_reset_the_cpu_baseline(self):
        plugin = self.plugin(session="monitor-old", generation=1)
        plugin.last_rke_cpu_sample = (
            "monitor-old", 1, {(999, 9999): 5.0}, 40.0)

        asyncio.run(plugin.begin_monitor_session("monitor-new", 2))

        self.assertEqual(plugin.monitor_session, "monitor-new")
        self.assertEqual(plugin.monitor_generation, 2)
        self.assertIsNone(plugin.last_rke_cpu_sample)
        self.assertIsNone(plugin.last_rke_cpu_percent)

        plugin.last_rke_cpu_sample = (
            "monitor-new", 2, {(999, 9999): 6.0}, 41.0)
        asyncio.run(plugin.end_monitor_session("monitor-new", 2))

        self.assertEqual(plugin.monitor_session, "")
        self.assertIsNone(plugin.last_rke_cpu_sample)
        self.assertIsNone(plugin.last_rke_cpu_percent)


class BatteryPowerTelemetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.setdefault("decky", types.SimpleNamespace(
            logger=types.SimpleNamespace(info=lambda *_: None, error=lambda *_: None)))
        import main
        cls.main = main

    def test_exact_zero_current_is_a_valid_power_sample(self):
        with tempfile.TemporaryDirectory() as temporary:
            battery = Path(temporary)
            (battery / "voltage_now").write_text("4050000\n")
            (battery / "current_now").write_text("0\n")

            available, watts, current = self.main._battery_power(battery)

        self.assertTrue(available)
        self.assertEqual(watts, 0.0)
        self.assertEqual(current, 0)

    def test_signed_battery_flow_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            battery = Path(temporary)
            (battery / "voltage_now").write_text("4050000\n")
            (battery / "current_now").write_text("-1200000\n")

            available, watts, current = self.main._battery_power(battery)

        self.assertTrue(available)
        self.assertAlmostEqual(watts, -4.86)
        self.assertEqual(current, -1200000)

    def test_missing_or_malformed_power_attributes_are_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            battery = Path(temporary)
            (battery / "voltage_now").write_text("not-a-number\n")
            (battery / "current_now").write_text("0\n")
            self.assertEqual(
                self.main._battery_power(battery), (False, 0.0, 0))
            (battery / "voltage_now").write_text("4050000\n")
            (battery / "current_now").unlink()
            self.assertEqual(
                self.main._battery_power(battery), (False, 0.0, 0))
            (battery / "voltage_now").write_text("0\n")
            (battery / "current_now").write_text("0\n")
            self.assertEqual(
                self.main._battery_power(battery), (False, 0.0, 0))


class DeviceNetworkInfoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.setdefault("decky", types.SimpleNamespace(
            logger=types.SimpleNamespace(info=lambda *_: None, error=lambda *_: None)))
        import main
        cls.main = main

    def test_prefers_eth0_then_wlan_then_another_active_interface(self):
        output = "\n".join([
            "5: tailscale0 inet 100.64.0.2/32 scope global tailscale0",
            "3: wlan1 inet 192.168.1.117/24 brd 192.168.1.255 scope global wlan1",
            "2: eth0 inet 10.0.0.8/24 brd 10.0.0.255 scope global eth0",
        ])

        self.assertEqual(
            self.main._parse_device_network_info(output),
            {"ip": "10.0.0.8", "interface": "eth0"},
        )
        self.assertEqual(
            self.main._parse_device_network_info("\n".join(output.splitlines()[:2])),
            {"ip": "192.168.1.117", "interface": "wlan1"},
        )
        self.assertEqual(
            self.main._parse_device_network_info(output.splitlines()[0]),
            {"ip": "100.64.0.2", "interface": "tailscale0"},
        )

    def test_ignores_invalid_and_non_connectable_addresses(self):
        output = "\n".join([
            "1: lo inet 127.0.0.1/8 scope global lo",
            "2: eth0 inet not-an-address scope global eth0",
            "3: wlan0 inet 169.254.3.4/16 scope global wlan0",
        ])

        self.assertEqual(
            self.main._parse_device_network_info(output),
            {"ip": "Offline", "interface": ""},
        )

    def test_rpc_source_is_a_bounded_one_shot_active_ipv4_read(self):
        output = "2: wlan0 inet 192.168.0.74/24 scope global wlan0"
        with mock.patch.object(self.main, "_run", return_value=output) as run:
            result = self.main._device_network_info()

        self.assertEqual(result, {"ip": "192.168.0.74", "interface": "wlan0"})
        run.assert_called_once_with(
            ["ip", "-o", "-4", "address", "show", "up", "scope", "global"],
            check=False, timeout=3,
        )

    def test_missing_ip_tool_reports_offline(self):
        with mock.patch.object(self.main, "_run", side_effect=FileNotFoundError):
            self.assertEqual(
                self.main._device_network_info(),
                {"ip": "Offline", "interface": ""},
            )


class RocknixSettingHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.setdefault("decky", types.SimpleNamespace(
            logger=types.SimpleNamespace(info=lambda *_: None, error=lambda *_: None)))
        import main
        cls.main = main

    def test_native_profile_setter_receives_setting_as_positional_arguments(self):
        calls = []

        def fake_run(command, check=True, timeout=15):
            calls.append((command, check))
            return "yes" if "declare -F" in command[2] else ""

        with mock.patch.object(self.main.shutil, "which", return_value=None), \
                mock.patch.object(self.main, "_run", side_effect=fake_run):
            self.main._set_setting(
                "analogsticks.led", "127 255 204 204 255 204 204")

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0][-3:], [
            "rke-set-setting", "analogsticks.led",
            "127 255 204 204 255 204 204",
        ])
        self.assertIn('set_setting "$1" "$2"', calls[1][0][2])


class MonitorBypassFreshnessTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.setdefault("decky", types.SimpleNamespace(
            logger=types.SimpleNamespace(
                info=lambda *_: None, warning=lambda *_: None,
                error=lambda *_: None)))
        import main
        cls.main = main

    def plugin(self, charging=None):
        plugin = self.main.Plugin.__new__(self.main.Plugin)
        plugin.charging = charging
        plugin.monitor_lock = threading.RLock()
        plugin.monitor_session = ""
        plugin.monitor_generation = 0
        plugin.monitor_revision = 0
        plugin.monitor_bypass_active = False
        plugin.monitor_charging_valid = None
        plugin.battery_discharge_ema = None
        plugin.battery_discharge_samples = 0
        plugin.battery_discharge_last_sample = 0.0
        plugin.charging_status_warning = ""
        return plugin

    async def test_delayed_old_begin_cannot_replace_new_activation(self):
        plugin = self.plugin()

        newest = await plugin.begin_monitor_session("monitor-new", 200)
        with self.assertRaisesRegex(RuntimeError, "activation is stale"):
            await plugin.begin_monitor_session("monitor-old", 100)

        self.assertEqual(newest, {"generation": 200, "revision": 1})
        self.assertEqual(plugin.monitor_session, "monitor-new")
        self.assertEqual(plugin.monitor_generation, 200)
        self.assertEqual(plugin.monitor_revision, 1)

    async def test_end_overtaking_begin_tombstones_activation(self):
        plugin = self.plugin()

        ended = await plugin.end_monitor_session("monitor-hidden", 300)
        with self.assertRaisesRegex(RuntimeError, "activation is stale"):
            await plugin.begin_monitor_session("monitor-hidden", 300)

        self.assertEqual(ended, {"generation": 300, "revision": 1})
        self.assertEqual(plugin.monitor_session, "")

    async def test_close_and_reopen_requires_a_higher_generation(self):
        plugin = self.plugin()

        first = await plugin.begin_monitor_session("monitor-first", 350)
        closed = await plugin.end_monitor_session("monitor-first", 350)
        with self.assertRaisesRegex(RuntimeError, "activation is stale"):
            await plugin.begin_monitor_session("monitor-first", 350)
        reopened = await plugin.begin_monitor_session("monitor-second", 351)

        self.assertEqual(first, {"generation": 350, "revision": 1})
        self.assertEqual(closed, {"generation": 350, "revision": 2})
        self.assertEqual(reopened, {"generation": 351, "revision": 3})
        self.assertEqual(plugin.monitor_session, "monitor-second")
        self.assertFalse(plugin.monitor_bypass_active)

    async def test_same_session_invalidation_discards_old_telemetry_estimate(self):
        plugin = self.plugin()
        await plugin.begin_monitor_session("monitor-current", 400)
        with plugin.monitor_lock:
            plugin.monitor_bypass_active = True
            plugin.monitor_revision += 1
            old_revision = plugin.monitor_revision
            plugin.battery_discharge_ema = 1000.0
            plugin.battery_discharge_samples = 5
            plugin.battery_discharge_last_sample = 99.0

        epoch = await plugin.invalidate_monitor_charging_status(
            "monitor-current", 400)
        with self.assertRaisesRegex(RuntimeError, "state changed during telemetry"):
            plugin._update_bypass_estimate(
                "monitor-current", 400, old_revision, True,
                -5.0, -1_000_000, 2_000_000, 0, False)

        self.assertEqual(epoch, {"generation": 400, "revision": 3})
        self.assertFalse(plugin.monitor_bypass_active)
        self.assertIsNone(plugin.battery_discharge_ema)
        self.assertEqual(plugin.battery_discharge_samples, 0)

        # Telemetry may resume in the same activation, but only against the
        # invalidated revision and with Bypass-specific estimation disabled.
        seconds, ready = plugin._update_bypass_estimate(
            "monitor-current", 400, epoch["revision"], False,
            -5.0, -1_000_000, 2_000_000, 321, True)
        self.assertEqual((seconds, ready), (321, True))
        self.assertIsNone(plugin.battery_discharge_ema)

    async def test_monitor_status_failure_invalidates_cache_and_estimate(self):
        async def inline_to_thread(function, *arguments):
            return function(*arguments)

        class FailedCharging:
            @staticmethod
            def get_status():
                raise RuntimeError("status failed")

        plugin = self.plugin(FailedCharging())
        await plugin.begin_monitor_session("monitor-new", 500)
        plugin.monitor_bypass_active = True
        plugin.battery_discharge_ema = 1234.0
        plugin.battery_discharge_samples = 5
        plugin.battery_discharge_last_sample = 99.0

        with mock.patch.object(
                self.main.asyncio, "to_thread", new=inline_to_thread):
            with self.assertRaisesRegex(RuntimeError, "status failed"):
                await plugin.get_charging_status("monitor-new", 500)

        self.assertFalse(plugin.monitor_bypass_active)
        self.assertEqual(plugin.monitor_revision, 2)
        self.assertIsNone(plugin.battery_discharge_ema)
        self.assertEqual(plugin.battery_discharge_samples, 0)
        self.assertEqual(plugin.battery_discharge_last_sample, 0.0)

    async def test_incoherent_refresh_advances_revision_once_when_bypass_is_clear(self):
        async def inline_to_thread(function, *arguments):
            return function(*arguments)

        class IncoherentCharging:
            @staticmethod
            def get_status():
                return {
                    "captured_at": 1.0,
                    "battery": {"available": True, "valid": True,
                                "stale": False, "transitional": False,
                                "mode": "normal", "command": {}},
                    "pump": {"available": True, "valid": True,
                             "stale": False, "transitional": False,
                             "command": {}},
                    "coherent": False,
                    "operation": None,
                }

        plugin = self.plugin(IncoherentCharging())
        await plugin.begin_monitor_session("monitor-current", 550)
        initial_revision = plugin.monitor_revision

        with mock.patch.object(
                self.main.asyncio, "to_thread", new=inline_to_thread):
            status = await plugin.get_charging_status("monitor-current", 550)
            repeated = await plugin.get_charging_status("monitor-current", 550)

        self.assertEqual(status["charging_revision"], initial_revision + 1)
        self.assertEqual(
            repeated["charging_revision"], status["charging_revision"])
        self.assertFalse(plugin.monitor_bypass_active)
        self.assertFalse(plugin.monitor_charging_valid)
        self.assertIsNone(plugin.battery_discharge_ema)

    async def test_coherent_recovery_allows_one_later_failure_invalidation(self):
        async def inline_to_thread(function, *arguments):
            return function(*arguments)

        class ChangingCharging:
            outcome = "incoherent"

            @classmethod
            def get_status(cls):
                if cls.outcome == "failure":
                    raise RuntimeError("status failed")
                return {
                    "captured_at": 1.0,
                    "battery": {"available": True, "valid": True,
                                "stale": False, "transitional": False,
                                "mode": "normal", "command": {}},
                    "pump": {"available": True, "valid": True,
                             "stale": False, "transitional": False,
                             "command": {}},
                    "coherent": cls.outcome == "coherent",
                    "operation": None,
                }

        plugin = self.plugin(ChangingCharging())
        await plugin.begin_monitor_session("monitor-current", 560)

        with mock.patch.object(
                self.main.asyncio, "to_thread", new=inline_to_thread):
            invalid = await plugin.get_charging_status("monitor-current", 560)
            ChangingCharging.outcome = "coherent"
            recovered = await plugin.get_charging_status("monitor-current", 560)
            ChangingCharging.outcome = "failure"
            with self.assertRaisesRegex(RuntimeError, "status failed"):
                await plugin.get_charging_status("monitor-current", 560)
            failed_revision = plugin.monitor_revision
            # The frontend also invalidates after an RPC failure; this must be
            # idempotent with the backend failure invalidation.
            explicit = await plugin.invalidate_monitor_charging_status(
                "monitor-current", 560)
            with self.assertRaisesRegex(RuntimeError, "status failed"):
                await plugin.get_charging_status("monitor-current", 560)

        self.assertEqual(
            recovered["charging_revision"], invalid["charging_revision"])
        self.assertTrue(recovered["coherent"])
        self.assertEqual(failed_revision, recovered["charging_revision"] + 1)
        self.assertEqual(explicit["revision"], failed_revision)
        self.assertEqual(plugin.monitor_revision, failed_revision)
        self.assertFalse(plugin.monitor_charging_valid)

    async def test_usb_input_power_status_is_bound_to_generation_and_revision(self):
        async def inline_to_thread(function, *arguments):
            return function(*arguments)

        class InputPowerCharging:
            path = "qcom"
            valid = True
            microwatts = "39681000"

            @classmethod
            def get_status(cls):
                return {
                    "captured_at": 1.0,
                    "battery": {
                        "available": True, "valid": True, "stale": False,
                        "transitional": False, "mode": "normal", "command": {},
                    },
                    "pump": {
                        "available": True, "valid": True, "stale": False,
                        "transitional": False, "command": {},
                        "input_power": {
                            "available": True, "valid": cls.valid,
                            "stale": False, "path": cls.path,
                            "microwatts": cls.microwatts, "error": "",
                        },
                    },
                    "coherent": True,
                    "operation": None,
                }

        plugin = self.plugin(InputPowerCharging())
        opened = await plugin.begin_monitor_session("monitor-input", 575)
        with mock.patch.object(
                self.main.asyncio, "to_thread", new=inline_to_thread):
            first = await plugin.get_charging_status("monitor-input", 575)

        self.assertEqual(first["monitor_generation"], 575)
        self.assertEqual(first["charging_revision"], opened["revision"])
        self.assertEqual(
            first["pump"]["input_power"]["microwatts"], "39681000")

        invalidated = await plugin.invalidate_monitor_charging_status(
            "monitor-input", 575)
        InputPowerCharging.path = "transition"
        InputPowerCharging.valid = False
        InputPowerCharging.microwatts = None
        with mock.patch.object(
                self.main.asyncio, "to_thread", new=inline_to_thread):
            second = await plugin.get_charging_status("monitor-input", 575)

        self.assertEqual(
            second["charging_revision"], invalidated["revision"])
        self.assertEqual(second["pump"]["input_power"]["path"], "transition")
        self.assertIsNone(second["pump"]["input_power"]["microwatts"])

    async def test_previous_session_failure_cannot_reset_current_estimate(self):
        class FailedCharging:
            @staticmethod
            def get_status():
                raise RuntimeError("old status failed")

        plugin = self.plugin(FailedCharging())
        await plugin.begin_monitor_session("monitor-current", 600)
        plugin.monitor_bypass_active = True
        plugin.battery_discharge_ema = 1234.0
        plugin.battery_discharge_samples = 5
        plugin.battery_discharge_last_sample = 99.0

        with self.assertRaisesRegex(RuntimeError, "activation is stale"):
            await plugin.get_charging_status("monitor-old", 599)

        self.assertEqual(plugin.monitor_session, "monitor-current")
        self.assertTrue(plugin.monitor_bypass_active)
        self.assertEqual(plugin.battery_discharge_ema, 1234.0)
        self.assertEqual(plugin.battery_discharge_samples, 5)
        self.assertEqual(plugin.battery_discharge_last_sample, 99.0)


class RgbPackagingContractTests(unittest.TestCase):
    def test_release_build_compiles_and_packages_rgb_backend(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()

        self.assertIn(
            "python3 -m py_compile main.py charging.py rgb.py runtime-restore.py",
            workflow,
        )
        self.assertIn(
            "dist docs main.py charging.py rgb.py runtime-restore.py",
            workflow,
        )

    def test_installers_require_rgb_only_for_rgb_aware_releases(self):
        installer = (ROOT / "install.sh").read_text()
        updater = (ROOT / "updater.sh").read_text()

        self.assertIn(
            "grep -q 'rgb\\.py' \"${work_dir}/plugin/RK-Enhanced/main.py\"",
            installer,
        )
        self.assertIn(
            '[ ! -f "${work_dir}/plugin/RK-Enhanced/rgb.py" ]', installer)
        self.assertNotIn(
            '[ ! -f "${work_dir}/plugin/RK-Enhanced/rgb.py" ] ||', installer)
        self.assertIn("grep -q 'rgb\\.py' \"${staged}/main.py\"", updater)
        self.assertIn('[ ! -f "${staged}/rgb.py" ]', updater)
        self.assertNotIn('[ ! -f "${staged}/rgb.py" ] ||', updater)


class FrontendIntegrityPackagingTests(unittest.TestCase):
    PREFIX = "rke-frontend-sha256-v1:"
    PLACEHOLDER = PREFIX + ("0" * 64)

    def run_integrity(self, root, action):
        return subprocess.run(
            ["node", str(ROOT / "scripts" / "frontend-integrity.mjs"), action],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_stamp_and_verify_bind_the_exact_built_bundle(self):
        if shutil.which("node") is None:
            self.skipTest("Node.js is required for the frontend integrity tool")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dist = root / "dist"
            dist.mkdir()
            original = (
                f'const frontendBundleId = "{self.PLACEHOLDER}";\n'
                "const release = 'same-version-build-a';\n"
            ).encode()
            index = dist / "index.js"
            index.write_bytes(original)

            stamped = self.run_integrity(root, "stamp")

            self.assertEqual(stamped.returncode, 0, stamped.stderr)
            bundle = index.read_bytes()
            matches = re.findall(
                rb"rke-frontend-sha256-v1:[0-9a-f]{64}", bundle)
            self.assertEqual(len(matches), 1)
            bundle_id = matches[0].decode()
            normalized = bundle.replace(matches[0], self.PLACEHOLDER.encode())
            self.assertEqual(normalized, original)
            self.assertEqual(
                bundle_id,
                self.PREFIX + hashlib.sha256(normalized).hexdigest(),
            )
            manifest = json.loads(
                (dist / "frontend-integrity.json").read_text())
            self.assertEqual(manifest, {
                "protocol": 1,
                "algorithm": "sha256-normalized-v1",
                "bundle_id": bundle_id,
                "index_sha256": hashlib.sha256(bundle).hexdigest(),
            })
            verified = self.run_integrity(root, "verify")
            self.assertEqual(verified.returncode, 0, verified.stderr)

            index.write_bytes(bundle + b"// post-stamp mutation\n")
            rejected = self.run_integrity(root, "verify")
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("frontend integrity verification failed", rejected.stderr)

    def test_release_build_stamps_verifies_and_packages_health_metadata(self):
        package = json.loads((ROOT / "package.json").read_text())
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()

        self.assertEqual(
            package["scripts"]["build"],
            "rollup -c && node scripts/frontend-integrity.mjs stamp",
        )
        self.assertEqual(
            package["scripts"]["verify:frontend"],
            "node scripts/frontend-integrity.mjs verify",
        )
        self.assertIn("pnpm verify:frontend", workflow)
        self.assertIn("install-health.json", workflow)

    def test_tag_release_uses_descriptive_title_and_visible_changelog(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()
        notes = (ROOT / "RELEASE_NOTES.md").read_text()

        self.assertIn(
            "name: RK-Enhanced ${{ github.ref_name }} — "
            "Odin 3 RGB restart persistence",
            workflow,
        )
        self.assertIn("body_path: RELEASE_NOTES.md", workflow)
        self.assertNotIn("generate_release_notes: true", workflow)
        self.assertIn("## Changelog", notes)
        self.assertIn("**From beta.9 or older:**", notes)
        self.assertIn(
            "curl -fL https://raw.githubusercontent.com/mrdidit/RK-Enhanced/main/install.sh | sh",
            notes,
        )


class PluginLoaderRecoveryPackagingContractTests(unittest.TestCase):
    def test_release_build_compiles_packages_and_marks_recovery_executable(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()

        self.assertIn(
            "python3 -m py_compile main.py charging.py rgb.py runtime-restore.py plugin_loader_recovery.py",
            workflow,
        )
        self.assertIn(
            "main.py charging.py rgb.py runtime-restore.py plugin_loader_recovery.py",
            workflow,
        )
        self.assertIn(
            "chmod 755 release/RK-Enhanced/runtime-restore.py release/RK-Enhanced/plugin_loader_recovery.py",
            workflow,
        )

    def test_staged_helper_is_required_only_when_backend_references_it(self):
        installer = (ROOT / "install.sh").read_text()
        updater = (ROOT / "updater.sh").read_text()

        self.assertIn(
            "grep -q 'plugin_loader_recovery\\.py' \\", installer)
        self.assertIn(
            '"${work_dir}/plugin/RK-Enhanced/main.py" &&', installer)
        self.assertIn(
            '[ ! -f "${work_dir}/plugin/RK-Enhanced/plugin_loader_recovery.py" ]',
            installer,
        )
        self.assertNotIn(
            '[ ! -f "${work_dir}/plugin/RK-Enhanced/plugin_loader_recovery.py" ] ||',
            installer,
        )
        self.assertIn(
            "grep -q 'plugin_loader_recovery\\.py' \"${staged}/main.py\" &&",
            updater,
        )
        self.assertIn(
            '[ ! -f "${staged}/plugin_loader_recovery.py" ]', updater)
        self.assertNotIn(
            '[ ! -f "${staged}/plugin_loader_recovery.py" ] ||', updater)
        self.assertIn(
            'chmod 755 "${PLUGINS_DIR}/RK-Enhanced/plugin_loader_recovery.py"',
            installer,
        )
        self.assertIn(
            'chmod 755 "${PLUGIN_DIR}/plugin_loader_recovery.py"', updater)

    def test_installer_and_updater_use_only_bounded_unit_scoped_cleanup(self):
        for name in ("install.sh", "updater.sh"):
            with self.subTest(name=name):
                source = (ROOT / name).read_text()
                self.assertIn("stop_plugin_loader_bounded()", source)
                self.assertIn(
                    'systemctl_bounded stop --no-block \\', source)
                self.assertIn("wait_for_plugin_loader_stop 15", source)
                self.assertIn("wait_for_plugin_loader_stop 3", source)
                self.assertIn("wait_for_plugin_loader_start 15", source)
                self.assertIn(
                    "systemctl_bounded kill --kill-who=all --signal=SIGTERM", source)
                self.assertIn(
                    "systemctl_bounded kill --kill-who=all --signal=SIGKILL", source)
                self.assertIn('timeout 5 systemctl "$@"', source)
                self.assertIn(
                    "for command in cmp curl cut flock grep jq sed sha256sum stat systemctl timeout tr unzip wc; do",
                    source,
                )
                self.assertNotIn('""|inactive|failed)', source)
                self.assertNotIn("pkill", source)
                self.assertNotIn("killall", source)
                self.assertNotIn("pgrep", source)
                self.assertNotRegex(source, r"(?i)(kill|pkill|killall).*\b(FEX|Python)\b")

    def test_maintenance_inhibitor_is_held_across_stop_replace_and_start(self):
        for name in ("install.sh", "updater.sh"):
            with self.subTest(name=name):
                source = (ROOT / name).read_text()
                self.assertIn(
                    'RUN_ROOT="${RKE_RUN_ROOT:-/run}"',
                    source,
                )
                self.assertIn(
                    'RECOVERY_LOCK_PATH="${RUN_ROOT}/lock/rk-enhanced-plugin-loader-recovery.lock"',
                    source,
                )
                self.assertIn(
                    'RECOVERY_MARKER_PATH="${RUN_ROOT}/rk-enhanced-plugin-loader-recovery.active"',
                    source,
                )
                self.assertIn("flock -n 9", source)
                self.assertIn("flock -u 9", source)
                self.assertIn('rm -f "${RECOVERY_MARKER_PATH}"', source)
                self.assertGreaterEqual(
                    source.count("end_plugin_loader_maintenance"), 3)
                begin = source.index(
                    "if ! begin_plugin_loader_maintenance; then")
                stop = source.index(
                    "if ! stop_plugin_loader_bounded; then", begin)
                start = source.index(
                    'systemctl_bounded start "${PLUGIN_LOADER_UNIT}"', stop)
                end = source.index("end_plugin_loader_maintenance", start)
                self.assertLess(begin, stop)
                self.assertLess(stop, start)
                self.assertLess(start, end)

    def test_install_health_is_nonce_hash_and_process_generation_bound(self):
        for name in ("install.sh", "updater.sh"):
            with self.subTest(name=name):
                source = (ROOT / name).read_text()
                self.assertIn("write_install_health_request()", source)
                self.assertIn(
                    'rke_health_nonce="$(cat "${PROC_ROOT}/sys/kernel/random/uuid"',
                    source,
                )
                self.assertIn(
                    'rke_health_main_hash="$(sha256sum "', source)
                self.assertIn(
                    'rke_health_dist_hash="$(sha256sum "', source)
                self.assertIn(".nonce == $nonce", source)
                self.assertIn(".main_sha256 == $main_sha256", source)
                self.assertIn(".dist_sha256 == $dist_sha256", source)
                self.assertIn(
                    ".frontend_bundle_id == $frontend_bundle_id", source)
                self.assertIn(".require_frontend ==", source)
                self.assertIn(".loader.pid == $loader_pid", source)
                self.assertIn(
                    ".loader.start_time_ticks == $loader_start", source)
                self.assertIn("process_start_time()", source)
                self.assertIn("wait_for_rke_health()", source)
                self.assertIn("frontend_integrity_id()", source)
                self.assertIn(
                    '--arg frontend_bundle_id "${HEALTH_BUNDLE_ID}"', source)
                self.assertIn(
                    '"$(frontend_integrity_id ', source)
                self.assertIn('"${HEALTH_BUNDLE_ID}" ] || return 1', source)

    def test_shells_validate_declared_frontend_integrity_metadata(self):
        placeholder = (
            "rke-frontend-sha256-v1:" + ("0" * 64))
        for name in ("install.sh", "updater.sh"):
            with self.subTest(name=name):
                source = (ROOT / name).read_text()
                self.assertIn("health_protocol_supported()", source)
                self.assertIn(
                    '.frontend_integrity == "sha256-normalized-v1"', source)
                self.assertIn(
                    'rke_integrity_manifest="${rke_integrity_root}/dist/frontend-integrity.json"',
                    source,
                )
                self.assertIn(
                    '.algorithm == "sha256-normalized-v1"', source)
                self.assertIn(".index_sha256 // empty", source)
                self.assertIn(
                    'rke_integrity_count="$(grep -o "${rke_integrity_id}"',
                    source,
                )
                self.assertIn(
                    f'rke_integrity_placeholder="{placeholder}"', source)
                self.assertIn("rke_integrity_normalized_hash", source)
                self.assertIn(
                    '[ "${rke_integrity_normalized_hash}" = "${rke_integrity_digest}" ]',
                    source,
                )
                self.assertIn(
                    'frontend_bundle_id: $frontend_bundle_id', source)

        installer = (ROOT / "install.sh").read_text()
        updater = (ROOT / "updater.sh").read_text()
        self.assertIn(
            '[ ! -f "${work_dir}/plugin/RK-Enhanced/install-health.json" ]',
            installer,
        )
        self.assertIn(
            '[ ! -f "${work_dir}/plugin/RK-Enhanced/dist/frontend-integrity.json" ]',
            installer,
        )
        self.assertIn(
            '[ -e "${staged}/install-health.json" ] ||', updater)
        self.assertIn(
            '[ -e "${staged}/dist/frontend-integrity.json" ]', updater)
        self.assertIn(
            '--argjson require_frontend "${rke_health_frontend_json}"',
            installer,
        )
        self.assertIn(
            '--argjson require_frontend "${rke_health_frontend_json}"',
            updater,
        )
        self.assertIn(
            "elif health_response_matches \\", updater)
        self.assertIn(
            '"${FRONTEND_READY_FILE}" "${rke_health_loader_pid}"', updater)
        self.assertIn(
            'rke_health_backend_fingerprint="${HEALTH_RESPONSE_FINGERPRINT}"',
            updater,
        )
        self.assertIn(
            '[ "${HEALTH_RESPONSE_FINGERPRINT}" = \\', updater)
        self.assertIn(
            '"${rke_health_backend_fingerprint}" ]; then', updater)

        self.assertIn(
            "systemctl_bounded is-active --quiet steam-bigpicture.scope",
            installer,
        )
        self.assertIn(
            "systemctl_bounded is-active --quiet steam-bigpicture.scope",
            updater,
        )
        self.assertIn(
            '[ -z "${frontend_requirement}" ]', updater)
        self.assertIn(
            'frontend_requirement="require-frontend"', installer)
        self.assertIn(
            'write_install_health_request "${rke_version}" '
            '"${frontend_requirement}"',
            installer,
        )
        self.assertIn(
            'wait_for_rke_health "${frontend_requirement}"', installer)
        self.assertIn(
            'elif health_response_matches \\', installer)
        self.assertIn(
            '"${FRONTEND_READY_FILE}" "${rke_health_loader_pid}"',
            installer,
        )
        self.assertIn(
            "frontend not tested because Steam is inactive", installer)
        self.assertIn(
            "frontend not tested because Steam is inactive", updater)

    def test_updater_limits_metadata_free_releases_to_explicit_legacy_tags(self):
        source = (ROOT / "updater.sh").read_text()
        helper = source.split("legacy_release_allowed() {", 1)[1].split(
            "\n}\n", 1)[0]
        selection = source.split("health_supported=0", 1)[1].split(
            'if [ "${preserve_updater}" -eq 1 ]; then', 1)[0]

        self.assertIn(
            "v0.1.0-alpha.[1-6]|0.1.0-alpha.[1-6]|"
            "v0.2.0-beta.[1-7]|0.2.0-beta.[1-7]",
            helper,
        )
        self.assertIn("return 0", helper)
        self.assertIn("return 1", helper)
        self.assertNotIn("index", helper)
        self.assertIn(
            '[ -e "${staged}/install-health.json" ] || \\', selection)
        self.assertIn(
            '[ -e "${staged}/dist/frontend-integrity.json" ]; then',
            selection,
        )
        self.assertIn(
            'if ! health_protocol_supported "${staged}"; then', selection)
        self.assertIn(
            'elif [ -n "${requested_version}" ] && \\', selection)
        self.assertIn(
            'legacy_release_allowed "${version}"; then', selection)
        self.assertIn(
            "Never infer legacy status from release order.", selection)
        self.assertNotIn("requested_index", selection)
        self.assertIn(
            'write_status "Update failed: release is missing required '
            'install-health metadata"',
            selection,
        )

    def test_health_wait_uses_a_separate_transaction_lock(self):
        for name in ("install.sh", "updater.sh"):
            with self.subTest(name=name):
                source = (ROOT / name).read_text()
                self.assertIn(
                    'TRANSACTION_LOCK_PATH="${RUN_ROOT}/lock/rk-enhanced-install-transaction.lock"',
                    source,
                )
                self.assertIn("begin_install_transaction()", source)
                self.assertIn("end_install_transaction()", source)
                self.assertIn('exec 8>"${TRANSACTION_LOCK_PATH}"', source)
                self.assertIn("flock -n 8", source)
                self.assertIn('exec 9>"${RECOVERY_LOCK_PATH}"', source)
                self.assertIn("flock -n 9", source)

                health_wait_marker = (
                    'if ! wait_for_rke_health "${frontend_requirement}"; then'
                )
                health_wait = source.index(health_wait_marker)
                release = source.rfind(
                    "\nend_plugin_loader_maintenance\n", 0, health_wait)
                loader_wait = source.rfind(
                    "if ! wait_for_plugin_loader_start 15; then",
                    0,
                    release,
                )
                transaction_end = source.index(
                    "\nend_install_transaction\n", health_wait)
                self.assertGreaterEqual(release, 0)
                self.assertGreaterEqual(loader_wait, 0)
                self.assertLess(loader_wait, release)
                self.assertLess(release, health_wait)
                self.assertLess(health_wait, transaction_end)

    def test_rollback_stops_tentative_loader_before_restoring_files(self):
        cases = {
            "install.sh": (
                "cleanup_install() {",
                "trap cleanup_install EXIT INT TERM",
                'rm -rf "${PLUGINS_DIR}/RK-Enhanced"',
            ),
            "updater.sh": (
                "cleanup_failure() {",
                "trap cleanup_failure EXIT INT TERM",
                'rm -rf "${PLUGIN_DIR}"',
            ),
        }
        for name, (start_marker, end_marker, restore_marker) in cases.items():
            with self.subTest(name=name):
                source = (ROOT / name).read_text()
                cleanup = source.split(start_marker, 1)[1].split(
                    end_marker, 1)[0]
                lock = cleanup.index(
                    "begin_plugin_loader_maintenance_bounded 10")
                stop = cleanup.index("stop_plugin_loader_bounded")
                restore = cleanup.index(restore_marker)
                self.assertLess(lock, stop)
                self.assertLess(stop, restore)

    def test_rollback_restores_metadata_then_revalidates_old_backend(self):
        cases = {
            "install.sh": (
                "cleanup_install() {",
                "trap cleanup_install EXIT",
                'wait_for_rke_health ""; then',
            ),
            "updater.sh": (
                "cleanup_failure() {",
                "trap cleanup_failure EXIT",
                'wait_for_rke_health ""; then',
            ),
        }
        for name, (start_marker, end_marker, wait_marker) in cases.items():
            with self.subTest(name=name):
                source = (ROOT / name).read_text()
                cleanup = source.split(start_marker, 1)[1].split(
                    end_marker, 1)[0]
                self.assertIn(
                    'cp -p "${installed_version_backup}" \\', cleanup)
                self.assertIn('"${INSTALLED_VERSION_FILE}"; then', cleanup)
                challenge = cleanup.index("write_install_health_request")
                self.assertIn(
                    '"${rollback_version}"',
                    cleanup[challenge:challenge + 120],
                )
                self.assertRegex(
                    cleanup[challenge:challenge + 180],
                    r'write_install_health_request\s+\\?\s*'
                    r'"\$\{rollback_version\}" ""',
                )
                self.assertNotIn(
                    '"${rollback_version}" "${frontend_requirement}"',
                    cleanup[challenge:challenge + 180],
                )
                start = cleanup.index(
                    'systemctl_bounded start "${PLUGIN_LOADER_UNIT}"',
                    challenge,
                )
                release = cleanup.index(
                    "end_plugin_loader_maintenance", start)
                health = cleanup.index(wait_marker, release)
                self.assertLess(challenge, start)
                self.assertLess(start, release)
                self.assertLess(release, health)

    def test_invalid_declared_rollback_metadata_is_never_labeled_legacy(self):
        cases = {
            "install.sh": (
                "cleanup_install() {",
                "trap cleanup_install EXIT",
                '${PLUGINS_DIR}/RK-Enhanced',
            ),
            "updater.sh": (
                "cleanup_failure() {",
                "trap cleanup_failure EXIT",
                '${PLUGIN_DIR}',
            ),
        }
        for name, (start_marker, end_marker, plugin_root) in cases.items():
            with self.subTest(name=name):
                source = (ROOT / name).read_text()
                cleanup = source.split(start_marker, 1)[1].split(
                    end_marker, 1)[0]
                declared = cleanup.index(
                    f'{{ [ -e "{plugin_root}/install-health.json" ] ||')
                self.assertIn(
                    f'[ -e "{plugin_root}/dist/frontend-integrity.json" ]',
                    cleanup[declared:declared + 220],
                )
                supported = cleanup.index(
                    "if health_protocol_supported", declared)
                legacy = cleanup.index(
                    'elif [ "${rollback_ok}" -eq 1 ] && \\', supported)
                declared_branch = cleanup[declared:legacy]
                failure_else = declared_branch.index("else")
                invalid = declared_branch.index(
                    "rollback_ok=0", failure_else)

                self.assertLess(supported - declared, failure_else)
                self.assertGreater(invalid, failure_else)
                self.assertNotIn("rollback_legacy=1", declared_branch)
                self.assertIn(
                    'legacy_release_allowed "${rollback_version}"',
                    cleanup[legacy:legacy + 180],
                )
                self.assertIn(
                    "rollback_legacy=1", cleanup[legacy:legacy + 220])

    def test_signals_flow_through_exit_cleanup_before_transaction_commit(self):
        cases = {
            "install.sh": "cleanup_install",
            "updater.sh": "cleanup_failure",
        }
        for name, cleanup_name in cases.items():
            with self.subTest(name=name):
                source = (ROOT / name).read_text()
                cleanup = source.split(f"{cleanup_name}() {{", 1)[1].split(
                    f"trap {cleanup_name} EXIT", 1)[0]
                self.assertIn("trap - EXIT HUP INT TERM", cleanup)
                self.assertIn(
                    '[ "${transaction_committed}" -ne 1 ]', cleanup)
                self.assertIn(f"trap {cleanup_name} EXIT", source)
                self.assertIn("trap 'exit 129' HUP", source)
                self.assertIn("trap 'exit 130' INT", source)
                self.assertIn("trap 'exit 143' TERM", source)
                commit = source.rindex("transaction_committed=1")
                transaction_end = source.rindex("end_install_transaction")
                self.assertLess(commit, transaction_end)

    def test_installers_fetch_newest_published_decky_and_roll_back(self):
        cases = {
            "install.sh": ("cleanup_install() {", "trap cleanup_install EXIT"),
            "updater.sh": ("cleanup_failure() {", "trap cleanup_failure EXIT"),
        }
        release_fixture = [
            {
                "tag_name": "v9.0.0-draft",
                "draft": True,
                "prerelease": False,
                "assets": [{
                    "name": "PluginLoader",
                    "browser_download_url": "https://invalid/draft",
                    "digest": "sha256:draft",
                }],
            },
            {
                "tag_name": "v3.2.9-pre1",
                "draft": False,
                "prerelease": True,
                "assets": [{
                    "name": "source.tar.gz",
                    "browser_download_url": "https://invalid/no-loader",
                    "digest": "sha256:no-loader",
                }],
            },
            {
                "tag_name": "v3.2.8-pre1",
                "draft": False,
                "prerelease": True,
                "assets": [{
                    "name": "PluginLoader",
                    "browser_download_url": "https://valid/pre",
                    "digest": "sha256:pre",
                }],
            },
            {
                "tag_name": "v3.2.6",
                "draft": False,
                "prerelease": False,
                "assets": [{
                    "name": "PluginLoader",
                    "browser_download_url": "https://valid/stable",
                    "digest": "sha256:stable",
                }],
            },
        ]

        for name, (cleanup_start, cleanup_end) in cases.items():
            with self.subTest(name=name):
                source = (ROOT / name).read_text()
                cleanup = source.split(cleanup_start, 1)[1].split(
                    cleanup_end, 1)[0]

                self.assertIn(
                    'DECKY_REPOSITORY="SteamDeckHomebrew/decky-loader"',
                    source,
                )
                self.assertIn(
                    'https://api.github.com/repos/${DECKY_REPOSITORY}/'
                    'releases?per_page=20',
                    source,
                )
                self.assertNotIn(
                    'repos/${DECKY_REPOSITORY}/releases/latest', source)
                filter_match = re.search(
                    r"decky_release_filter='([^']+)'", source)
                self.assertIsNotNone(filter_match)
                release_filter = filter_match.group(1)
                self.assertIn("select(.draft == false)", release_filter)
                self.assertNotIn("prerelease", release_filter)
                result = subprocess.run(
                    ["jq", "-c", release_filter],
                    input=json.dumps(release_fixture),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), {
                    "version": "v3.2.8-pre1",
                    "url": "https://valid/pre",
                    "digest": "sha256:pre",
                })
                self.assertIn(
                    'curl -fL "${decky_url}" -o '
                    '"${work_dir}/PluginLoader"',
                    source,
                )
                self.assertIn(
                    'cp -p "${PLUGIN_LOADER_PATH}" "${loader_backup}"',
                    source,
                )
                self.assertIn(
                    'cp "${work_dir}/PluginLoader" '
                    '"${PLUGIN_LOADER_PATH}"',
                    source,
                )
                self.assertIn(
                    'cp -p "${loader_backup}" "${PLUGIN_LOADER_PATH}"',
                    cleanup,
                )
                self.assertIn(
                    'cp -p "${loader_version_backup}" \\', cleanup)
                self.assertIn(
                    '"${PLUGIN_LOADER_VERSION_FILE}"; then', cleanup)

    def test_documentation_describes_runtime_guard_not_update_recovery(self):
        readme = (ROOT / "README.md").read_text()
        changelog = (ROOT / "CHANGELOG.md").read_text()

        for phrase in (
            "general runtime watchdog",
            "PID plus its `/proc` start-time ticks",
            "Clean unload removes the active marker",
            "Steam Big Picture is active",
            "same-boot 120-second cooldown",
            "short-lived, same-boot AppID request",
            "Manual Decky restarts, installs, updates, and Utils actions never create",
            "not an updater retry mechanism",
            "plugin_loader_recovery.py",
            "maintenance takes the lifecycle helper's exact",
            "/run/lock/rk-enhanced-plugin-loader-recovery.lock",
            "/run/rk-enhanced-plugin-loader-recovery.active",
            "held across stop, file replacement, and the tentative service start",
            "released so the new backend can publish its lifecycle generation",
            "install-transaction lock remains held throughout readiness verification",
            "rollback, preventing overlapping installs",
        ):
            self.assertIn(phrase, readme)
        self.assertIn("out-of-cgroup runtime watchdog", changelog)
        self.assertIn("separate from updater failures", changelog)
        self.assertIn("Manual Decky", changelog)
        self.assertIn("restarts and maintenance never request focus", changelog)


class FrontendLifecycleContractTests(unittest.TestCase):
    def test_conflict_removal_is_on_monitor_and_fan_heading_is_compact(self):
        content = (ROOT / "src" / "Content.tsx").read_text()
        monitor = content.split("const monitorContent = <>", 1)[1].split(
            "const availableTabs = [", 1)[0]

        self.assertIn(
            'onClick={confirmConflictRemoval}>Remove conflicting plugin',
            monitor,
        )
        self.assertIn('label="Permanent removal"', monitor)
        self.assertNotIn(">Open Utils</ButtonItem>", monitor.split(
            ": runtimeMutationBlocked", 1)[0])
        self.assertIn(
            '<PerformanceHeading title="Rocknix Fan Curve" />', content
        )
        self.assertIn('"Edit custom curve"', content)
        self.assertNotIn("Remove conflict &", content)

    def test_automatic_recovery_focus_is_one_shot_and_non_launching(self):
        index = (ROOT / "src" / "index.tsx").read_text()
        backend = (ROOT / "src" / "backend.ts").read_text()
        focus = (ROOT / "src" / "recoveryFocus.ts").read_text()

        self.assertIn("restoreAutomaticRecoveryGameFocus()", index)
        self.assertIn(
            'call<[], string | null>("consume_automatic_recovery_focus_request")',
            backend,
        )
        self.assertEqual(focus.count("consumeAutomaticRecoveryFocusRequest()"), 1)
        self.assertIn("SteamUIStore", focus)
        self.assertIn("store.SetRunningApp!(appid)", focus)
        self.assertIn("store.NavigateToRunningApp()", focus)
        self.assertIn('Navigation.Navigate("/apprunning")', focus)
        self.assertLess(
            focus.index("store.SetRunningApp!(appid)"),
            focus.index("store.NavigateToRunningApp()"),
        )
        self.assertIn("RegisterForFocusChangeEvents", focus)
        self.assertIn("STEAM_UI_SETTLE_MS = 3000", focus)
        self.assertIn("STEAM_UI_READY_TIMEOUT_MS = 15000", focus)
        self.assertIn("MAX_NAVIGATION_ATTEMPTS = 3", focus)
        self.assertNotIn("RaiseWindowForGame", focus)
        self.assertNotIn("RunGame", focus)
        self.assertNotIn("currentGame", focus)
        self.assertNotIn("setInterval", focus)
        self.assertIn("report_automatic_recovery_focus_result", backend)

    def test_device_ip_is_read_once_per_visible_utils_activation(self):
        content = (ROOT / "src" / "Content.tsx").read_text()
        backend = (ROOT / "src" / "backend.ts").read_text()

        self.assertIn(
            'if (!panelVisible || tab !== "Utils") return;', content)
        self.assertEqual(content.count("getDeviceNetworkInfo()"), 1)
        self.assertIn("return () => { cancelled = true; };", content)
        self.assertIn(
            'call<[], DeviceNetworkInfo>("get_device_network_info")', backend)
        self.assertNotIn("Run this on the PC you connect from: ssh-keygen -R", content)
        self.assertNotIn("Reset SSH trust on your PC", content)
        self.assertNotIn("clear_ssh", backend)

    def test_rgb_tab_is_capability_and_quick_access_gated_without_polling(self):
        content = (ROOT / "src" / "Content.tsx").read_text()
        rgb = (ROOT / "src" / "RGB.tsx").read_text()
        rgb_model = (ROOT / "src" / "rgbModel.ts").read_text()
        typescript = (ROOT / "src" / "types.ts").read_text()
        backend = (ROOT / "src" / "backend.ts").read_text()
        main = (ROOT / "main.py").read_text()

        self.assertRegex(
            content,
            r'state\.capabilities\.rgb\?\.available\s*\?\s*\[\{',
        )
        self.assertRegex(
            content,
            r'active=\{panelVisible\s*&&\s*tab\s*===\s*"RGB"\}',
        )
        self.assertIn("if (!active)", rgb)
        self.assertIn("requestGeneration !== generation.current", rgb)
        self.assertIn(
            "if (dirtyRef.current && !forceRefresh) return;", rgb)
        self.assertIn("needsRefreshAfterStaleApply.current = true;", rgb)
        self.assertIn("activeRef.current", rgb)
        self.assertIn("[active, refreshRequest]", rgb)
        # One read activates/reloads the page; the second reconciles every
        # native attribute after a failed mutation. Neither is a poll loop.
        self.assertEqual(rgb.count("getRgbState()"), 2)
        self.assertIn("reconcileFailedOperation", rgb)
        self.assertIn("Actual RGB state could not be refreshed", rgb)
        self.assertIn("rgbFailureDisposition", rgb)
        self.assertIn("transport is suspended; retry after resume", rgb_model)
        self.assertIn("baseline.revision = actual.revision", rgb)
        self.assertIn("setSaved(baseline)", rgb)
        self.assertIn('draft ? "RGB change not saved"', rgb)
        self.assertIn("preMutationRejection", rgb)
        self.assertIn("state changed; refresh before applying", rgb_model)
        self.assertNotIn("setInterval", rgb)
        self.assertNotIn("setTimeout", rgb)
        for provider in (
                '"none"', '"sysfs-effects"', '"analog-static"',
                '"pocket-evo-v3"', '"htr3212-static"'):
            self.assertIn(provider, typescript)
        self.assertIn('status?.provider === "analog-static"', rgb)
        self.assertIn(
            'status?.provider === "pocket-evo-v3"', rgb)
        self.assertIn(
            'draft?.provider === "htr3212-static"', rgb)
        self.assertIn('HTR3212_QUADRANT_LABELS', rgb)
        self.assertIn('htrDraft ? HTR3212_QUADRANT_LABELS', rgb)
        self.assertIn(
            'state.resume_lighting || defaultNonOffLighting(state)', rgb)
        self.assertIn('label="Layout"', rgb)
        self.assertIn('both: "Both rings"', rgb)
        self.assertIn('"per-stick": "Per stick"', rgb)
        self.assertIn('quadrants: "Quadrants"', rgb)
        self.assertIn('"rgb-breath": "RGB Breath"', rgb)
        self.assertIn('reactive: "Reactive"', rgb)
        self.assertIn('label="Saved calibration override"', rgb)
        self.assertIn('setEvoStaticGroup', rgb_model)
        self.assertIn('setEvoLayoutMode', rgb_model)
        self.assertIn(
            'call<[RgbCalibrationRequest], RgbState>("set_rgb_calibration", request)',
            backend)
        self.assertIn('async def set_rgb_calibration(self, request):', main)
        self.assertIn('provider: state.provider', rgb)
        self.assertIn('revision: state.revision', rgb)
        self.assertIn('left.provider === right.provider', rgb)
        self.assertIn('left.revision === right.revision', rgb)
        self.assertIn('state.provider !== "none"', rgb)
        self.assertIn('const busyRef = useRef(false)', rgb)
        self.assertIn('busyRef.current = true', rgb)
        self.assertIn('busyRef.current = false', rgb)
        self.assertIn('<ToggleField label="Stick lighting"', rgb)
        self.assertIn('checked={draft.mode === "rgb"}', rgb)
        self.assertIn('request.mode = checked ? "rgb" : "off";', rgb)
        self.assertIn('effects.length > 1', rgb)
        self.assertIn('min={analogStatic ? 1 : 0}', rgb)
        self.assertIn('status?.zones_differ', rgb)
        self.assertIn('Saved ring colours differ', rgb)
        self.assertIn('needsRefreshAfterStaleApply.current = true;', rgb)
        self.assertIn('Reload current RGB state', rgb)
        self.assertIn('get_runtime_capability=_rocknix_env', main)
        tabs = content.split("const availableTabs = [", 1)[1].split("];", 1)[0]
        expected = [
            'id: "Monitor"', 'id: "Performance"', 'id: "Fan"',
            'id: "Presets"', 'id: "RGB"', 'id: "Utils"',
            'id: "Experimental"',
        ]
        positions = [tabs.index(marker) for marker in expected]
        self.assertEqual(positions, sorted(positions))

    def test_quick_access_visibility_gates_both_charging_pollers(self):
        content = (ROOT / "src" / "Content.tsx").read_text()
        index = (ROOT / "src" / "index.tsx").read_text()

        self.assertIn("alwaysRender: true", index)
        self.assertIn(
            "useQuickAccessVisible as useLegacyQuickAccessVisible", content)
        self.assertIn(
            "useQuickAccessVisible as useLoaderQuickAccessVisible", content)
        self.assertIn(
            'typeof useLoaderQuickAccessVisible === "function"', content)
        self.assertIn(": useLegacyQuickAccessVisible;", content)
        self.assertIn(
            "const panelVisible = useQuickAccessVisibleCompat();", content)
        self.assertIn(
            'active={panelVisible && tab === "Monitor"}', content)
        self.assertIn(
            'active={panelVisible && tab === "Experimental"}', content)

    def test_frontend_reports_readiness_from_registered_bundle_after_state_rpc(self):
        content = (ROOT / "src" / "Content.tsx").read_text()
        index = (ROOT / "src" / "index.tsx").read_text()
        readiness = (ROOT / "src" / "installReadiness.ts").read_text()
        backend = (ROOT / "src" / "backend.ts").read_text()

        self.assertNotIn("reportFrontendReady", content)
        self.assertNotIn("frontendBundleId", content)
        self.assertNotIn("frontendHydrated", content)
        self.assertIn(
            'import { startInstallReadinessProbe } from "./installReadiness";',
            index,
        )
        descriptor = index.index("const plugin = {")
        start = index.index(
            "cancelInstallReadiness = startInstallReadinessProbe();")
        returned = index.index("return plugin;", start)
        self.assertLess(descriptor, start)
        self.assertLess(start, returned)
        self.assertIn(
            "onDismount: () => cancelInstallReadiness(),", index)

        get_state = readiness.index("const state = await getState();")
        validate_state = readiness.index(
            'throw new Error("RK-Enhanced returned an invalid initial state")')
        report = readiness.index(
            "reportFrontendReady(frontendBundleId)", validate_state)
        self.assertLess(get_state, validate_state)
        self.assertLess(validate_state, report)
        self.assertIn(
            "for (let attempt = 0; attempt < 240 && !cancelled;",
            readiness,
        )
        self.assertIn("if (cancelled) return;", readiness)
        self.assertIn("if (cancelled || ready !== false) return;", readiness)
        self.assertIn("cancelActiveProbe?.();", readiness)
        self.assertIn("return cancel;", readiness)
        self.assertIn(
            "await new Promise(resolve => window.setTimeout(resolve, 500));",
            readiness,
        )

        # The visible panel keeps its independent bounded hydration retry, but
        # install success no longer depends on this React tree mounting.
        request_state = content.index("const requestState = useCallback(() => {")
        panel_get_state = content.index("const request = getState();", request_state)
        metadata = content.index("const acceptStateMetadata", panel_get_state)
        full = content.index("const acceptStateFull", metadata)
        load = content.index("const loadState = useCallback(async (", full)
        load_end = content.index(
            "}, [acceptStateFull, acceptStateMetadata, requestState]);", load)
        load_source = content[load:load_end]
        metadata_source = content[metadata:full]
        full_source = content[full:load]
        self.assertIn("setState(next);", metadata_source)
        self.assertNotIn("setDraft", metadata_source)
        self.assertIn("setDraft(clone(next.presets[wanted]));", full_source)
        self.assertIn("const next = await requestState();", load_source)
        self.assertIn("canAcceptGameState(", load_source)
        self.assertIn("return true;", load_source)
        self.assertIn("return false;", load_source)

        boot_effect = content.index(
            "useEffect(() => {", content.index("const installActionBlocked"))
        boot_end = content.index("}, []);", boot_effect)
        boot_source = content[boot_effect:boot_end]
        self.assertIn("let cancelled = false;", boot_source)
        self.assertIn(
            "const hydrate = async (attempt: number)",
            boot_source,
        )
        self.assertIn(
            'if (await loadState(expectedAppid, "full", isCurrent)) return;',
            boot_source,
        )
        self.assertIn(
            "attempt + 1 >= STATE_SYNC_ATTEMPTS", boot_source)
        self.assertIn(
            "window.setTimeout(() => { void hydrate(attempt + 1); }, STATE_SYNC_DELAY)",
            boot_source,
        )
        self.assertIn("cancelled = true;", boot_source)
        self.assertIn("window.clearTimeout(retryTimer);", boot_source)
        self.assertIn(
            'import { frontendBundleId } from "./frontendIntegrity";',
            readiness,
        )
        self.assertIn(
            'call<[string], boolean | null>("report_frontend_ready", buildId)',
            backend,
        )

    def test_monitor_always_renders_policy_failure_or_unsupported_state(self):
        monitor = (ROOT / "src" / "Monitor.tsx").read_text()

        self.assertIn(
            'const batteryPolicyRow = <Metric label="Battery policy"', monitor)
        self.assertGreaterEqual(monitor.count("{batteryPolicyRow}"), 2)
        self.assertIn('const batteryPolicyLabel = chargingError ? "Unavailable"', monitor)
        self.assertNotIn(
            'batteryPolicy?.available && <Metric label="Battery policy"', monitor)

    def test_unsupported_charging_ui_hides_helper_paths_and_raw_details(self):
        experimental = (ROOT / "src" / "Experimental.tsx").read_text()
        monitor = (ROOT / "src" / "Monitor.tsx").read_text()

        self.assertIn('label="Battery policy unsupported"', experimental)
        self.assertIn('label="Pump profiles unsupported"', experimental)
        self.assertNotIn(
            "/usr/bin/charging_mode is unavailable", experimental)
        self.assertNotIn(
            "/usr/bin/kpfe_fast_charge is unavailable", experimental)
        self.assertIn(
            "battery?.available && statusError(battery)", experimental)
        self.assertIn(
            "pump?.available && statusError(pump)", experimental)
        self.assertIn(
            "chargingError || (batteryPolicy?.available", monitor)

    def test_monitor_separates_policy_level_and_signed_battery_flow(self):
        monitor = (ROOT / "src" / "Monitor.tsx").read_text()
        typescript = (ROOT / "src" / "types.ts").read_text()

        self.assertIn('label="Battery flow"', monitor)
        self.assertIn('data.battery_flow_watts >= 0.2 ? "Charging"', monitor)
        self.assertIn('data.battery_flow_watts <= -0.2 ? "Discharging" : "Idle"', monitor)
        self.assertIn('batteryFlowState === "Idle" ? 0 : data.battery_watts', monitor)
        self.assertIn('label="Battery level"', monitor)
        self.assertIn('label="Time estimate"', monitor)
        self.assertIn('`${batteryFlowWatts.toFixed(1)} W in`', monitor)
        self.assertIn('`${batteryFlowWatts.toFixed(1)} W out`', monitor)
        self.assertIn(': "0.0 W"', monitor)
        self.assertIn('const batteryEstimateDirection = batteryFilling ? "to full"', monitor)
        self.assertIn(': batteryDraining ? "left" : ""', monitor)
        self.assertIn('? `${duration(data.battery_seconds)} ${batteryEstimateDirection}`', monitor)
        self.assertIn('batteryStatus === "charging"', monitor)
        self.assertIn('batteryStatus === "discharging"', monitor)
        self.assertIn("batteryFlowWatts.toFixed(1)", monitor)
        self.assertIn("battery_power_available: boolean;", typescript)
        self.assertIn("data.battery_power_available", monitor)
        self.assertIn('batteryPolicy?.mode === "limit"', monitor)
        self.assertIn('batteryPolicy.charge_behaviour === "inhibit-charge"', monitor)
        self.assertIn('charging?.pump.usb_online === true ? "Active" : "Selected"', monitor)
        self.assertIn('charging?.pump.usb_online !== true ? "Selected"', monitor)
        self.assertIn('? "Paused"', monitor)
        self.assertIn('? "Charging" : "Active"', monitor)
        self.assertNotIn('"Holding charge"', monitor)
        self.assertNotIn('"Power draw"', monitor)
        self.assertNotIn('"Battery until full"', monitor)
        self.assertNotIn('"Battery remaining"', monitor)
        self.assertLess(
            monitor.rfind('<Heading headingRef={monitorTopRef}>Live Performance</Heading>'),
            monitor.rfind('<Heading>Clocks</Heading>'),
        )
        self.assertLess(
            monitor.rfind('<Heading>Clocks</Heading>'),
            monitor.rfind('<Heading>Power &amp; Battery</Heading>'),
        )
        self.assertLess(
            monitor.rfind('<Heading>Power &amp; Battery</Heading>'),
            monitor.rfind('<Heading>Runtime</Heading>'),
        )
        self.assertIn("monitorTopRef.current?.focus()", monitor)
        self.assertLess(
            monitor.rfind('<Metric label="Thermal limit"'),
            monitor.rfind('<Metric label="CPU load"'),
        )
        self.assertIn("rke_cpu_percent: number | null;", typescript)
        self.assertIn("rke_cpu_available: boolean;", typescript)
        self.assertIn('label="RK-E CPU Load"', monitor)
        self.assertIn('typeof data.rke_cpu_percent === "number"', monitor)
        self.assertIn(
            'rkeCpuUsage / logicalCpus', monitor)
        self.assertIn('rkeCpuLoad === null ? "Calculating…"', monitor)
        self.assertIn('!rkeCpuLoadAvailable ? "Unavailable"', monitor)
        self.assertIn('value >= 50 ? "#fc5c65"', monitor)
        self.assertIn('value >= 25 ? "#f39c3d"', monitor)
        self.assertIn('value >= 10 ? "#fed330"', monitor)
        self.assertLess(
            monitor.rfind('<Metric label="CPU queue"'),
            monitor.rfind('<Metric label="RK-E CPU Load"'),
        )
        self.assertLess(
            monitor.rfind('<Metric label="RK-E CPU Load"'),
            monitor.rfind('<Heading onActivate={backToTop}>Back to top</Heading>'),
        )
        self.assertEqual(monitor.count("getTelemetry("), 1)
        self.assertNotIn("setInterval", monitor)

    def test_experimental_uses_compact_qcom_normal_label(self):
        experimental = (ROOT / "src" / "Experimental.tsx").read_text()

        self.assertEqual(experimental.count('"Qcom Normal"'), 2)
        self.assertIn('choice("normal", "Qcom Normal")', experimental)
        self.assertNotIn('choice("normal", "Qualcomm/Normal")', experimental)

    def test_experimental_usb_input_power_uses_current_status_lifecycle(self):
        experimental = (ROOT / "src" / "Experimental.tsx").read_text()
        backend = (ROOT / "src" / "backend.ts").read_text()

        self.assertIn('label="USB input power"', experimental)
        self.assertIn("status.pump.input_power", experimental)
        self.assertIn(
            "inputPowerDisplay(currentStatus, pairReady && !busy)", experimental)
        self.assertIn(
            "statusSession === activationSession.current", experimental)
        self.assertIn("request === generation.current", experimental)
        self.assertIn("setCurrentRefreshSucceeded(false);", experimental)
        self.assertIn('return { value: "Offline" }', experimental)
        self.assertIn('return { value: "Transitioning…" }', experimental)
        self.assertIn("const value = BigInt(microwatts);", experimental)
        self.assertIn("(value + 50000n) / 100000n", experimental)
        self.assertIn("`${tenths / 10n}.${tenths % 10n} W`", experimental)
        self.assertNotIn('"Qualcomm" : "Dual pump"', experimental)
        self.assertIn("return { value: watts };", experimental)
        self.assertEqual(experimental.count("const poll = async () =>"), 1)
        self.assertNotIn("input_power", backend)

    def test_experimental_battery_temperature_uses_existing_status_refresh(self):
        charging = (ROOT / "charging.py").read_text()
        experimental = (ROOT / "src" / "Experimental.tsx").read_text()
        types = (ROOT / "src" / "types.ts").read_text()
        backend = (ROOT / "src" / "backend.ts").read_text()

        self.assertIn(
            'BATTERY_TEMPERATURE = Path("/sys/class/power_supply/battery/temp")',
            charging,
        )
        self.assertIn('"battery_temperature_deci_c": battery_temperature_deci_c', charging)
        self.assertIn("battery_temperature_deci_c: number | null;", types)
        self.assertIn('label="Battery temperature"', experimental)
        self.assertIn("batteryTemperatureDisplay(currentStatus.battery_temperature_deci_c)", experimental)
        self.assertNotIn("battery_temperature", backend)
        self.assertEqual(experimental.count("const poll = async () =>"), 1)

    def test_experimental_status_polish_is_semantic_and_conditional(self):
        experimental = (ROOT / "src" / "Experimental.tsx").read_text()

        self.assertIn('batterySelection(battery) === "limit-100"', experimental)
        self.assertIn('label="Battery charging"', experimental)
        self.assertNotIn('label="Observed behaviour"', experimental)
        for value in ("Allowed", "Paused", 'normalized === "charging"',
                      'normalized === "discharging"',
                      'normalized === "not charging"', "Off", "Starting",
                      "Active", "Transitional/Unknown", "Error", "PD-PPS"):
            self.assertIn(value, experimental)
        for color in ("#26de81", "#45aaf2", "#fed330", "#f39c3d", "#fc5c65"):
            self.assertIn(color, experimental)


if __name__ == "__main__":
    unittest.main()
