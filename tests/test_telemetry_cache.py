import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import shutil
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock


sys.modules.setdefault("decky", types.SimpleNamespace(
    logger=types.SimpleNamespace(
        info=lambda *_: None, warning=lambda *_: None,
        error=lambda *_: None, exception=lambda *_: None)))

import main


class TelemetryCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.cpu_root = self.root / "cpufreq"
        self.gpu_root = self.root / "devfreq"
        self.thermal_root = self.root / "thermal"
        self.cpu_root.mkdir()
        self.gpu_root.mkdir()
        self.thermal_root.mkdir()

        self.policy = self.cpu_root / "policy0"
        self.policy.mkdir()
        self._write(self.policy, {
            "scaling_available_frequencies": "100 200\n",
            "scaling_boost_frequencies": "300\n",
            "cpuinfo_min_freq": "100\n",
            "cpuinfo_max_freq": "300\n",
            "affected_cpus": "0 1\n",
            "scaling_cur_freq": "100\n",
            "scaling_min_freq": "100\n",
            "scaling_max_freq": "300\n",
            "scaling_governor": "schedutil\n",
            "boost": "1\n",
        })
        (self.cpu_root / "boost").write_text("1\n")

        self.gpu = self.gpu_root / "soc:gpu"
        self.gpu.mkdir()
        self._write(self.gpu, {
            "available_frequencies": "200 400\n",
            "cur_freq": "200\n",
            "min_freq": "200\n",
            "max_freq": "400\n",
            "governor": "msm-adreno-tz\n",
        })

        self.cpu_zone = self.thermal_root / "thermal_zone0"
        self.cpu_zone.mkdir()
        self._write(self.cpu_zone, {
            "type": "cpuss0-thermal\n", "temp": "50000\n",
        })
        self.gpu_zone = self.thermal_root / "thermal_zone1"
        self.gpu_zone.mkdir()
        self._write(self.gpu_zone, {
            "type": "gpuss0-thermal\n", "temp": "60000\n",
        })
        self.cpu_cooling = self.thermal_root / "cooling_device0"
        self.cpu_cooling.mkdir()
        self._write(self.cpu_cooling, {
            "type": "cpufreq-0\n", "cur_state": "0\n",
        })
        self.gpu_cooling = self.thermal_root / "cooling_device1"
        self.gpu_cooling.mkdir()
        self._write(self.gpu_cooling, {
            "type": "devfreq-gpu\n", "cur_state": "0\n",
        })
        self.pwm = self.root / "pwm1"
        self.pwm.write_text("51\n")

        self.plugin = main.Plugin.__new__(main.Plugin)
        self.plugin.monitor_lock = threading.RLock()
        self.plugin.telemetry_sample_lock = threading.Lock()
        self.plugin.telemetry_topology_epoch = 0
        self.plugin.telemetry_topology_cache = None
        self.plugin.gpu_fdinfo_paths = []
        self.plugin.gpu_fdinfo_refresh = 0.0
        self.plugin.gamescope_pid = None
        self.plugin.gamescope_identity = None
        self.plugin.gpu_drm_lock = threading.RLock()
        self.plugin.gpu_drm_revision = 0
        self.plugin.gpu_drm_cache = {
            "kind": "none", "context": "", "revision": 0,
            "identities": (),
            "clients": (), "signature": ("invalid",),
            "refreshed_at": 0.0, "generation": 0,
        }
        self.plugin.active_appid = ""
        self.plugin.last_gpu_sample = None
        self.plugin.monitor_session = ""
        self.plugin.monitor_generation = 0
        self.plugin.monitor_revision = 0
        self.plugin.monitor_bypass_active = False
        self.plugin.monitor_charging_valid = None
        self.plugin.last_rke_cpu_sample = None
        self.plugin.battery_discharge_ema = None
        self.plugin.battery_discharge_samples = 0
        self.plugin.battery_discharge_last_sample = 0.0

        self.patches = (
            mock.patch.object(main, "CPU_ROOT", self.cpu_root),
            mock.patch.object(main, "GPU_ROOT", self.gpu_root),
            mock.patch.object(main, "THERMAL_ROOT", self.thermal_root),
            mock.patch.object(main, "_fan_pwm_path", return_value=self.pwm),
        )
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temporary.cleanup()

    @staticmethod
    def _write(directory, values):
        for name, value in values.items():
            (directory / name).write_text(value)

    def test_static_discovery_is_cached_but_all_live_values_are_reread(self):
        with mock.patch.object(
                self.plugin, "_discover_telemetry_topology",
                wraps=self.plugin._discover_telemetry_topology) as discover, \
                mock.patch.object(
                    main, "_get_setting", side_effect=["quiet", "custom"]):
            topology = self.plugin._telemetry_topology()
            first_cpu = self.plugin._cpu_telemetry(topology)
            first_gpu = self.plugin._gpu_telemetry(topology)
            first_temps = self.plugin._temperature_telemetry(topology)
            first_cooling = self.plugin._cooling_telemetry(topology)
            first_fan = self.plugin._fan_status_from_topology(topology)

            self._write(self.policy, {
                "scaling_cur_freq": "175\n",
                "scaling_min_freq": "150\n",
                "scaling_max_freq": "200\n",
                "scaling_governor": "performance\n",
                "boost": "0\n",
            })
            self._write(self.gpu, {
                "cur_freq": "350\n", "min_freq": "250\n",
                "max_freq": "350\n", "governor": "performance\n",
            })
            (self.cpu_zone / "temp").write_text("55000\n")
            (self.gpu_zone / "temp").write_text("65000\n")
            (self.cpu_cooling / "cur_state").write_text("1\n")
            (self.gpu_cooling / "cur_state").write_text("2\n")
            self.pwm.write_text("102\n")

            same_topology = self.plugin._telemetry_topology()
            second_cpu = self.plugin._cpu_telemetry(same_topology)
            second_gpu = self.plugin._gpu_telemetry(same_topology)
            second_temps = self.plugin._temperature_telemetry(same_topology)
            second_cooling = self.plugin._cooling_telemetry(same_topology)
            second_fan = self.plugin._fan_status_from_topology(same_topology)

        self.assertIs(topology, same_topology)
        discover.assert_called_once_with()
        self.assertEqual(first_cpu[0]["current"], 100)
        self.assertEqual(second_cpu[0]["current"], 175)
        self.assertEqual(second_cpu[0]["minimum"], 150)
        self.assertEqual(second_cpu[0]["maximum"], 200)
        self.assertEqual(second_cpu[0]["governor"], "performance")
        self.assertEqual(first_cpu[0]["effective_maximum"], 300)
        self.assertEqual(second_cpu[0]["effective_maximum"], 200)
        self.assertEqual(first_gpu["current"], 200)
        self.assertEqual(second_gpu["current"], 350)
        self.assertEqual(second_gpu["governor"], "performance")
        self.assertEqual(first_temps[:2], ([50000], []))
        self.assertEqual(second_temps[0], [55000])
        self.assertEqual(second_temps[2:], ([65000], [65000]))
        self.assertEqual(first_cooling, (False, False))
        self.assertEqual(second_cooling, (True, True))
        self.assertEqual(first_fan, {
            "fan_pwm": 51, "fan_percent": 20,
            "cooling_profile": "quiet",
        })
        self.assertEqual(second_fan, {
            "fan_pwm": 102, "fan_percent": 40,
            "cooling_profile": "custom",
        })

    def test_new_monitor_activation_invalidates_static_and_drm_discovery(self):
        with mock.patch.object(
                self.plugin, "_discover_telemetry_topology",
                wraps=self.plugin._discover_telemetry_topology) as discover:
            first = self.plugin._telemetry_topology()
            self.plugin.gpu_fdinfo_paths = [Path("/proc/fake/fdinfo/1")]
            self.plugin.gpu_fdinfo_refresh = 123.0
            self.plugin.gpu_drm_cache["refreshed_at"] = 123.0

            asyncio.run(self.plugin.begin_monitor_session("monitor-a", 1))
            second = self.plugin._telemetry_topology()

        self.assertIsNot(first, second)
        self.assertEqual(discover.call_count, 2)
        self.assertEqual(self.plugin.telemetry_topology_epoch, 1)
        self.assertEqual(self.plugin.gpu_fdinfo_paths, [])
        self.assertEqual(self.plugin.gpu_fdinfo_refresh, 0.0)
        self.assertEqual(self.plugin.gpu_drm_cache["kind"], "none")
        self.assertEqual(self.plugin.gpu_drm_cache["refreshed_at"], 0.0)

    def test_incomplete_discovery_retries_only_after_bounded_delay(self):
        missing = {
            "cpu": (), "gpu": None, "thermal": (), "cooling": (),
            "fan_pwm_path": None, "incomplete": True,
        }
        with mock.patch.object(
                self.plugin, "_discover_telemetry_topology",
                side_effect=lambda: dict(missing)) as discover, mock.patch.object(
                    main.time, "monotonic", side_effect=[100.0, 105.0, 111.0]):
            first = self.plugin._telemetry_topology()
            second = self.plugin._telemetry_topology()
            third = self.plugin._telemetry_topology()

        self.assertIs(first, second)
        self.assertIsNot(second, third)
        self.assertEqual(discover.call_count, 2)

    def test_partially_initialized_cpu_policy_marks_discovery_incomplete(self):
        pending = self.cpu_root / "policy1"
        pending.mkdir()
        (pending / "scaling_cur_freq").write_text("100\n")

        topology = self.plugin._discover_telemetry_topology()

        self.assertEqual([item["id"] for item in topology["cpu"]], ["0"])
        self.assertTrue(topology["incomplete"])

    def test_failed_discovery_is_not_cached_and_next_request_retries(self):
        original = self.plugin._discover_telemetry_topology
        with mock.patch.object(
                self.plugin, "_discover_telemetry_topology",
                side_effect=[OSError("sysfs unavailable"),
                             original()]) as discover:
            with self.assertRaisesRegex(OSError, "sysfs unavailable"):
                self.plugin._telemetry_topology()
            recovered = self.plugin._telemetry_topology()

        self.assertEqual(discover.call_count, 2)
        self.assertIs(self.plugin.telemetry_topology_cache, recovered)

    def test_disappearing_cached_path_is_rediscovered_immediately(self):
        replacement = self.root / "pwm2"
        replacement.write_text("153\n")
        fan_path = mock.Mock(side_effect=[self.pwm, replacement])
        with mock.patch.object(main, "_fan_pwm_path", fan_path), \
                mock.patch.object(
                    self.plugin, "_discover_telemetry_topology",
                    wraps=self.plugin._discover_telemetry_topology) as discover:
            first = self.plugin._telemetry_topology()
            self.pwm.unlink()
            second = self.plugin._telemetry_topology()

        self.assertEqual(first["fan_pwm_path"], self.pwm)
        self.assertEqual(second["fan_pwm_path"], replacement)
        self.assertEqual(discover.call_count, 2)

    def test_overtaken_discovery_cannot_populate_new_activation_cache(self):
        entered = threading.Event()
        release = threading.Event()
        original = self.plugin._discover_telemetry_topology

        def delayed_discovery():
            entered.set()
            self.assertTrue(release.wait(2))
            return original()

        with mock.patch.object(
                self.plugin, "_discover_telemetry_topology",
                side_effect=delayed_discovery):
            with ThreadPoolExecutor(max_workers=1) as executor:
                request = executor.submit(self.plugin._telemetry_topology)
                self.assertTrue(entered.wait(2))
                with self.plugin.monitor_lock:
                    self.plugin._invalidate_telemetry_topology_locked()
                release.set()
                stale = request.result(timeout=2)

        self.assertEqual(stale["epoch"], 0)
        self.assertEqual(self.plugin.telemetry_topology_epoch, 1)
        self.assertIsNone(self.plugin.telemetry_topology_cache)

    def test_telemetry_samples_are_serialized(self):
        entered_first = threading.Event()
        entered_second = threading.Event()
        release_first = threading.Event()
        state_lock = threading.Lock()
        active = 0
        maximum = 0
        calls = 0

        def sample(*_arguments):
            nonlocal active, maximum, calls
            with state_lock:
                calls += 1
                call = calls
                active += 1
                maximum = max(maximum, active)
            if call == 1:
                entered_first.set()
                self.assertTrue(release_first.wait(2))
            else:
                entered_second.set()
            with state_lock:
                active -= 1
            return call

        self.plugin._telemetry_sample = sample
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.plugin._telemetry)
            self.assertTrue(entered_first.wait(2))
            second = executor.submit(self.plugin._telemetry)
            self.assertFalse(entered_second.wait(0.05))
            release_first.set()
            self.assertEqual(first.result(timeout=2), 1)
            self.assertEqual(second.result(timeout=2), 2)

        self.assertEqual(maximum, 1)

    def test_empty_fdinfo_result_is_cached_for_ten_seconds(self):
        proc_root = self.root / "proc-empty"
        proc_root.mkdir()
        with mock.patch.object(main, "PROC_ROOT", proc_root), \
                mock.patch.object(
                    self.plugin, "_steam_scope_pids", return_value=()), \
                mock.patch.object(
                    self.plugin, "_refresh_gpu_fdinfo_paths",
                    wraps=self.plugin._refresh_gpu_fdinfo_paths) as refresh, \
                mock.patch.object(
                    main.time, "monotonic",
                    side_effect=[100.0, 105.0, 111.0]):
            self.assertEqual(self.plugin._gpu_engine_time(), 0)
            self.assertEqual(self.plugin._gpu_engine_time(), 0)
            self.assertEqual(self.plugin._gpu_engine_time(), 0)

        self.assertEqual(refresh.call_count, 2)

    @staticmethod
    def _add_proc_process(
            proc_root, pid, comm, command=None, clients=(),
            start_time=None, environment=(), parent_pid=1):
        process = proc_root / str(pid)
        (process / "fd").mkdir(parents=True)
        (process / "fdinfo").mkdir()
        (process / "comm").write_text(f"{comm}\n")
        command = command if command is not None else (comm, "--test")
        if isinstance(command, str):
            command = (command, "--test")
        (process / "cmdline").write_bytes(
            b"\0".join(value.encode() for value in command) + b"\0")
        (process / "environ").write_bytes(
            b"\0".join(value.encode() for value in environment) + b"\0")
        fields = ["0"] * 30
        fields[0] = "S"
        fields[1] = str(parent_pid)
        fields[19] = str(pid if start_time is None else start_time)
        (process / "stat").write_text(
            f"{pid} ({comm}) " + " ".join(fields) + "\n")
        paths = []
        for client in clients:
            if len(client) == 3:
                descriptor, client_id, has_gpu_engine = client
                target = "/dev/dri/renderD128"
                engine_time = 100 if has_gpu_engine else None
            else:
                descriptor, target, client_id, engine_time = client
            os.symlink(target, process / "fd" / str(descriptor))
            info = process / "fdinfo" / str(descriptor)
            lines = [f"drm-client-id:\t{client_id}"]
            if engine_time is not None:
                lines.append(f"drm-engine-gpu:\t{engine_time} ns")
            info.write_text("\n".join(lines) + "\n")
            paths.append(info)
        return process, paths

    def test_gamescope_selection_rejects_reaper_even_when_it_is_first(self):
        proc_root = self.root / "proc-reaper-first"
        proc_root.mkdir()
        self._add_proc_process(
            proc_root, 100, "gamescopereaper", "gamescopereaper")
        _, compositor_paths = self._add_proc_process(
            proc_root, 200, "gamescope-wl", "gamescope",
            clients=((12, "4", True),))

        with mock.patch.object(main, "PROC_ROOT", proc_root), \
                mock.patch.object(
                    self.plugin, "_steam_scope_pids", return_value=(100, 200)):
            self.assertEqual(self.plugin._find_gamescope_pid(), 200)
            self.plugin.active_appid = ""
            self.plugin._refresh_gpu_fdinfo_paths()

        self.assertEqual(self.plugin.gamescope_pid, 200)
        self.assertEqual(self.plugin.gpu_fdinfo_paths, compositor_paths)

    def test_cached_reaper_is_rejected_after_backend_reload(self):
        proc_root = self.root / "proc-cached-reaper"
        proc_root.mkdir()
        self._add_proc_process(
            proc_root, 100, "gamescopereaper", "gamescopereaper")
        self._add_proc_process(
            proc_root, 200, "gamescope-wl", "gamescope",
            clients=((12, "4", True),))
        self.plugin.gamescope_pid = 100

        with mock.patch.object(main, "PROC_ROOT", proc_root), \
                mock.patch.object(
                    self.plugin, "_steam_scope_pids", return_value=(100, 200)):
            self.assertEqual(self.plugin._find_gamescope_pid(), 200)

        self.assertEqual(self.plugin.gamescope_pid, 200)

    def test_gamescope_respawn_replaces_cached_compositor_without_drm(self):
        proc_root = self.root / "proc-respawn"
        proc_root.mkdir()
        self._add_proc_process(
            proc_root, 100, "gamescope-wl", "gamescope")
        self._add_proc_process(
            proc_root, 300, "gamescope-wl", "gamescope",
            clients=((14, "9", True),))
        self.plugin.gamescope_pid = 100

        with mock.patch.object(main, "PROC_ROOT", proc_root), \
                mock.patch.object(
                    self.plugin, "_steam_scope_pids", return_value=(100, 300)):
            self.assertEqual(self.plugin._find_gamescope_pid(), 300)

        self.assertEqual(self.plugin.gamescope_pid, 300)

    def test_gamescope_without_ready_drm_is_not_a_measurable_source(self):
        proc_root = self.root / "proc-no-drm-yet"
        proc_root.mkdir()
        self._add_proc_process(
            proc_root, 100, "gamescopereaper", "gamescopereaper")
        self._add_proc_process(
            proc_root, 200, "gamescope-wl", "gamescope")

        with mock.patch.object(main, "PROC_ROOT", proc_root), \
                mock.patch.object(
                    self.plugin, "_steam_scope_pids", return_value=(100, 200)):
            self.assertIsNone(self.plugin._find_gamescope_pid())
            cache = self.plugin._refresh_gpu_fdinfo_paths(100.0)

        self.assertIsNone(self.plugin.gamescope_pid)
        self.assertEqual(cache["kind"], "none")
        self.assertEqual(cache["clients"], ())

    def test_gamescope_selection_prefers_most_measurable_gpu_clients(self):
        proc_root = self.root / "proc-multiple-compositors"
        proc_root.mkdir()
        self._add_proc_process(
            proc_root, 200, "gamescope-wl", "gamescope",
            clients=((12, "4", True),))
        self._add_proc_process(
            proc_root, 300, "gamescope", "/usr/bin/gamescope",
            clients=((13, "5", True), (14, "6", True)))

        with mock.patch.object(main, "PROC_ROOT", proc_root), \
                mock.patch.object(
                    self.plugin, "_steam_scope_pids", return_value=(200, 300)):
            self.assertEqual(self.plugin._find_gamescope_pid(), 300)

        self.assertEqual(self.plugin.gamescope_pid, 300)

    def test_steam_scope_compositor_beats_newer_outside_session(self):
        proc_root = self.root / "proc-scope-owner"
        proc_root.mkdir()
        _, scope_paths = self._add_proc_process(
            proc_root, 110, "gamescope-wl",
            ("/usr/bin/gamescope", "--backend", "drm"),
            clients=((12, "/dev/dri/renderD128", "4", 100),),
            start_time=100)
        self._add_proc_process(
            proc_root, 900, "gamescope-wl",
            ("/usr/bin/gamescope", "--backend", "drm"),
            clients=(
                (20, "/dev/dri/renderD128", "7", 200),
                (21, "/dev/dri/renderD128", "8", 300),
                (22, "/dev/dri/renderD128", "9", 400),
            ), start_time=900)

        with mock.patch.object(main, "PROC_ROOT", proc_root), \
                mock.patch.object(
                    self.plugin, "_steam_scope_pids", return_value=(110,)):
            cache = self.plugin._refresh_gpu_fdinfo_paths(100.0)

        self.assertEqual(cache["kind"], "gamescope")
        self.assertEqual(self.plugin.gamescope_identity, (110, 100))
        self.assertEqual(self.plugin.gpu_fdinfo_paths, scope_paths)

    def test_authoritative_scope_never_falls_back_to_other_compositor(self):
        proc_root = self.root / "proc-scope-no-global-fallback"
        proc_root.mkdir()
        self._add_proc_process(
            proc_root, 110, "gamescope-wl",
            ("/usr/bin/gamescope", "--backend", "drm"),
            clients=((12, "/dev/dri/renderD128", "4", None),),
            start_time=100)
        self._add_proc_process(
            proc_root, 900, "gamescope-wl",
            ("/usr/bin/gamescope", "--backend", "drm"),
            clients=((20, "/dev/dri/renderD128", "7", 500),),
            start_time=900)

        with mock.patch.object(main, "PROC_ROOT", proc_root), \
                mock.patch.object(
                    self.plugin, "_steam_scope_pids", return_value=(110,)):
            cache = self.plugin._refresh_gpu_fdinfo_paths(100.0)

        self.assertEqual(cache["kind"], "none")
        self.assertIsNone(self.plugin.gamescope_pid)
        self.assertEqual(self.plugin.gpu_fdinfo_paths, [])

    def test_exact_global_appid_beats_scope_compositor_fallback(self):
        proc_root = self.root / "proc-global-appid"
        proc_root.mkdir()
        self._add_proc_process(
            proc_root, 110, "gamescope-wl",
            ("/usr/bin/gamescope", "--backend", "drm"),
            clients=((12, "/dev/dri/renderD128", "4", 100),),
            start_time=100)
        _, game_paths = self._add_proc_process(
            proc_root, 500, "container-game", clients=(
                (30, "/dev/dri/renderD128", "12", 600),),
            start_time=200, environment=("SteamGameId=42",))
        self.plugin.active_appid = "42"

        with mock.patch.object(main, "PROC_ROOT", proc_root), \
                mock.patch.object(
                    self.plugin, "_steam_scope_pids", return_value=(110,)):
            cache = self.plugin._refresh_gpu_fdinfo_paths(100.0)

        self.assertEqual(cache["kind"], "appid")
        self.assertIsNone(self.plugin.gamescope_pid)
        self.assertEqual(self.plugin.gpu_fdinfo_paths, game_paths)

    def test_active_appid_without_engine_falls_back_to_scope_compositor(self):
        proc_root = self.root / "proc-app-no-engine"
        proc_root.mkdir()
        _, compositor_paths = self._add_proc_process(
            proc_root, 110, "gamescope-wl",
            ("/usr/bin/gamescope", "--backend", "drm"),
            clients=((12, "/dev/dri/renderD128", "4", 250),),
            start_time=100)
        self._add_proc_process(
            proc_root, 500, "game-launcher", clients=(
                (30, "/dev/dri/renderD128", "12", None),),
            start_time=200, environment=("SteamAppId=42",))
        self.plugin.active_appid = "42"

        with mock.patch.object(main, "PROC_ROOT", proc_root), \
                mock.patch.object(
                    self.plugin, "_steam_scope_pids",
                    return_value=(110, 500)):
            cache = self.plugin._refresh_gpu_fdinfo_paths(100.0)
            total = self.plugin._cached_gpu_engine_time(cache)

        self.assertEqual(cache["kind"], "gamescope")
        self.assertEqual(self.plugin.gamescope_pid, 110)
        self.assertEqual(self.plugin.gpu_fdinfo_paths, compositor_paths)
        self.assertEqual(total, 250)

    def test_newer_lower_pid_replaces_lingering_compositor_at_refresh(self):
        proc_root = self.root / "proc-lower-pid-newer"
        proc_root.mkdir()
        self._add_proc_process(
            proc_root, 900, "gamescope-wl", "gamescope",
            clients=((12, "/dev/dri/renderD128", "4", 100),),
            start_time=100)

        with mock.patch.object(main, "PROC_ROOT", proc_root), \
                mock.patch.object(
                    self.plugin, "_steam_scope_pids",
                    return_value=(100, 900)):
            first = self.plugin._refresh_gpu_fdinfo_paths(100.0)
            self._add_proc_process(
                proc_root, 100, "gamescope-wl", "gamescope",
                clients=((14, "/dev/dri/renderD128", "8", 200),),
                start_time=200)
            with mock.patch.object(main.time, "monotonic", return_value=111.0):
                _, total = self.plugin._gpu_engine_sample()

        self.assertEqual(first["identities"], ((900, 100),))
        self.assertEqual(self.plugin.gamescope_identity, (100, 200))
        self.assertEqual(total, 200)

    def test_same_pid_new_starttime_invalidates_cached_source(self):
        proc_root = self.root / "proc-pid-reuse"
        proc_root.mkdir()
        self._add_proc_process(
            proc_root, 200, "gamescope-wl", "gamescope",
            clients=((12, "/dev/dri/renderD128", "4", 100),),
            start_time=10)

        with mock.patch.object(main, "PROC_ROOT", proc_root), \
                mock.patch.object(
                    self.plugin, "_steam_scope_pids", return_value=(200,)):
            first = self.plugin._refresh_gpu_fdinfo_paths(100.0)
            self.plugin.last_gpu_sample = (
                first["generation"], 100, 100_000_000_000)
            shutil.rmtree(proc_root / "200")
            self._add_proc_process(
                proc_root, 200, "gamescope-wl", "gamescope",
                clients=((12, "/dev/dri/renderD128", "9", 300),),
                start_time=20)
            with mock.patch.object(main.time, "monotonic", return_value=101.0):
                generation, total = self.plugin._gpu_engine_sample()

        self.assertGreater(generation, first["generation"])
        self.assertEqual(self.plugin.gamescope_identity, (200, 20))
        self.assertEqual(total, 300)
        self.assertIsNone(self.plugin.last_gpu_sample)

    def test_same_process_recycled_fd_is_rediscovered_before_deadline(self):
        proc_root = self.root / "proc-fd-reuse"
        proc_root.mkdir()
        process, _ = self._add_proc_process(
            proc_root, 200, "gamescope-wl", "gamescope",
            clients=((12, "/dev/dri/renderD128", "4", 100),),
            start_time=10)

        with mock.patch.object(main, "PROC_ROOT", proc_root), \
                mock.patch.object(
                    self.plugin, "_steam_scope_pids", return_value=(200,)):
            first = self.plugin._refresh_gpu_fdinfo_paths(100.0)
            self.plugin.last_gpu_sample = (
                first["generation"], 100, 100_000_000_000)
            (process / "fd" / "12").unlink()
            os.symlink("/dev/dri/renderD129", process / "fd" / "12")
            (process / "fdinfo" / "12").write_text(
                "drm-client-id:\t9\ndrm-engine-gpu:\t300 ns\n")
            with mock.patch.object(main.time, "monotonic", return_value=101.0):
                generation, total = self.plugin._gpu_engine_sample()

        self.assertGreater(generation, first["generation"])
        self.assertEqual(total, 300)
        self.assertIsNone(self.plugin.last_gpu_sample)

    def test_client_ids_are_deduplicated_per_physical_drm_device(self):
        proc_root = self.root / "proc-device-client-keys"
        proc_root.mkdir()
        self._add_proc_process(
            proc_root, 200, "gamescope-wl", "gamescope", clients=(
                (10, "/dev/dri/renderD128", "7", 100),
                (11, "/dev/dri/renderD128", "7", 100),
                (12, "/dev/dri/renderD129", "7", 200),
            ), start_time=10)

        with mock.patch.object(main, "PROC_ROOT", proc_root), \
                mock.patch.object(
                    self.plugin, "_steam_scope_pids", return_value=(200,)):
            cache = self.plugin._refresh_gpu_fdinfo_paths(100.0)
            total = self.plugin._cached_gpu_engine_time(cache)

        self.assertEqual(len(cache["clients"]), 2)
        self.assertEqual(total, 300)

    def test_app_source_change_resets_gamescope_delta_baseline(self):
        proc_root = self.root / "proc-source-change"
        proc_root.mkdir()
        self._add_proc_process(
            proc_root, 110, "gamescope-wl", "gamescope",
            clients=((12, "/dev/dri/renderD128", "4", 100),),
            start_time=100)
        self.plugin.active_appid = "42"

        with mock.patch.object(main, "PROC_ROOT", proc_root), \
                mock.patch.object(
                    self.plugin, "_steam_scope_pids",
                    return_value=(110, 500)):
            first = self.plugin._refresh_gpu_fdinfo_paths(100.0)
            self.plugin.last_gpu_sample = (
                first["generation"], 100, 100_000_000_000)
            self._add_proc_process(
                proc_root, 500, "active-game", clients=(
                    (30, "/dev/dri/renderD128", "12", 5000),),
                start_time=200, environment=("SteamGameId=42",))
            second = self.plugin._refresh_gpu_fdinfo_paths(111.0)

        self.assertEqual(first["kind"], "gamescope")
        self.assertEqual(second["kind"], "appid")
        self.assertGreater(second["generation"], first["generation"])
        self.assertIsNone(self.plugin.last_gpu_sample)

    def test_dead_source_is_rediscovered_before_refresh_deadline(self):
        proc_root = self.root / "proc-dead-source"
        proc_root.mkdir()
        self._add_proc_process(
            proc_root, 100, "gamescope-wl", "gamescope",
            clients=((12, "/dev/dri/renderD128", "4", 100),),
            start_time=10)

        with mock.patch.object(main, "PROC_ROOT", proc_root), \
                mock.patch.object(
                    self.plugin, "_steam_scope_pids",
                    return_value=(100, 300)):
            self.plugin._refresh_gpu_fdinfo_paths(100.0)
            shutil.rmtree(proc_root / "100")
            _, replacement_paths = self._add_proc_process(
                proc_root, 300, "gamescope-wl", "gamescope",
                clients=((14, "/dev/dri/renderD128", "9", 350),),
                start_time=20)
            with mock.patch.object(main.time, "monotonic", return_value=101.0):
                _, total = self.plugin._gpu_engine_sample()

        self.assertEqual(self.plugin.gamescope_identity, (300, 20))
        self.assertEqual(self.plugin.gpu_fdinfo_paths, replacement_paths)
        self.assertEqual(total, 350)

    def test_no_engine_source_recovers_after_bounded_refresh(self):
        proc_root = self.root / "proc-drm-not-ready"
        proc_root.mkdir()
        _, paths = self._add_proc_process(
            proc_root, 200, "gamescope-wl", "gamescope",
            clients=((12, "/dev/dri/renderD128", "4", None),),
            start_time=10)

        with mock.patch.object(main, "PROC_ROOT", proc_root), \
                mock.patch.object(
                    self.plugin, "_steam_scope_pids", return_value=(200,)):
            first = self.plugin._refresh_gpu_fdinfo_paths(100.0)
            paths[0].write_text(
                "drm-client-id:\t4\ndrm-engine-gpu:\t225 ns\n")
            with mock.patch.object(
                    main.time, "monotonic", side_effect=[105.0, 111.0]):
                _, before_deadline = self.plugin._gpu_engine_sample()
                _, after_deadline = self.plugin._gpu_engine_sample()

        self.assertEqual(first["kind"], "none")
        self.assertEqual(before_deadline, 0)
        self.assertEqual(after_deadline, 225)
        self.assertEqual(self.plugin.gamescope_identity, (200, 10))

    def test_unchanged_source_refresh_preserves_generation_and_baseline(self):
        proc_root = self.root / "proc-unchanged-source"
        proc_root.mkdir()
        _, paths = self._add_proc_process(
            proc_root, 200, "gamescope-wl", "gamescope",
            clients=((12, "/dev/dri/renderD128", "4", 100),),
            start_time=10)

        with mock.patch.object(main, "PROC_ROOT", proc_root), \
                mock.patch.object(
                    self.plugin, "_steam_scope_pids", return_value=(200,)):
            first = self.plugin._refresh_gpu_fdinfo_paths(100.0)
            baseline = (first["generation"], 100, 100_000_000_000)
            self.plugin.last_gpu_sample = baseline
            paths[0].write_text(
                "drm-client-id:\t4\ndrm-engine-gpu:\t200 ns\n")
            second = self.plugin._refresh_gpu_fdinfo_paths(111.0)

        self.assertEqual(second["generation"], first["generation"])
        self.assertEqual(self.plugin.last_gpu_sample, baseline)

    def test_kgsl_idle_zero_bypasses_fdinfo_and_clears_fallback(self):
        self.plugin.last_gpu_sample = (4, 100, 1000)
        with mock.patch.object(
                self.plugin, "_gpu_engine_sample",
                side_effect=AssertionError("fdinfo fallback must not run")):
            percent = self.plugin._gpu_utilization(0)

        self.assertEqual(percent, 0.0)
        self.assertIsNone(self.plugin.last_gpu_sample)

    def test_gpu_delta_is_never_crossed_between_source_generations(self):
        with mock.patch.object(
                self.plugin, "_gpu_engine_sample", side_effect=[
                    (1, 100), (2, 10_000), (2, 10_100)]), \
                mock.patch.object(
                    main.time, "monotonic_ns",
                    side_effect=[100, 1100, 2100]):
            first = self.plugin._gpu_utilization(-1)
            changed = self.plugin._gpu_utilization(-1)
            settled = self.plugin._gpu_utilization(-1)

        self.assertEqual(first, 0.0)
        self.assertEqual(changed, 0.0)
        self.assertEqual(settled, 10.0)

    def test_overtaken_gpu_discovery_cannot_seed_winning_lifecycle(self):
        proc_root = self.root / "proc-overtaken-gpu-discovery"
        proc_root.mkdir()
        self._add_proc_process(
            proc_root, 200, "gamescope-wl", "gamescope",
            clients=((12, "/dev/dri/renderD128", "4", 100),),
            start_time=10)
        self._add_proc_process(
            proc_root, 500, "active-game", clients=(
                (30, "/dev/dri/renderD128", "12", 500),),
            start_time=20, environment=("SteamAppId=42",))
        original = self.plugin._select_gamescope_candidate
        overtaken = False
        winning_cache = None

        def publish_new_appid_during_old_discovery(processes):
            nonlocal overtaken, winning_cache
            candidate = original(processes)
            if not overtaken:
                overtaken = True
                self.plugin.active_appid = "42"
                self.plugin._invalidate_gpu_drm_cache()
                winning_cache = self.plugin._refresh_gpu_fdinfo_paths(100.0)
            return candidate

        with mock.patch.object(main, "PROC_ROOT", proc_root), \
                mock.patch.object(
                    self.plugin, "_steam_scope_pids",
                    return_value=(200, 500)), \
                mock.patch.object(
                    self.plugin, "_select_gamescope_candidate",
                    side_effect=publish_new_appid_during_old_discovery), \
                mock.patch.object(main.time, "monotonic", return_value=100.0):
            generation, total = self.plugin._gpu_engine_sample()

        self.assertTrue(overtaken)
        self.assertIsNotNone(winning_cache)
        self.assertIs(self.plugin.gpu_drm_cache, winning_cache)
        self.assertEqual(self.plugin.gpu_drm_revision, 1)
        self.assertEqual(winning_cache["context"], "42")
        self.assertEqual(winning_cache["kind"], "appid")
        self.assertEqual(total, 500)
        self.assertEqual(generation, winning_cache["generation"])

    def test_narrow_fan_status_never_runs_full_telemetry(self):
        self.plugin._telemetry_sample = mock.Mock(
            side_effect=AssertionError("full telemetry must not run"))
        async def inline_to_thread(function, *arguments):
            return function(*arguments)

        with mock.patch.object(main, "_get_setting", return_value="custom"), \
                mock.patch.object(
                    main.asyncio, "to_thread", new=inline_to_thread):
            status = asyncio.run(self.plugin.get_fan_status())

        self.assertEqual(status, {
            "fan_pwm": 51, "fan_percent": 20,
            "cooling_profile": "custom",
        })
        self.plugin._telemetry_sample.assert_not_called()


if __name__ == "__main__":
    unittest.main()
