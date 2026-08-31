import asyncio
from concurrent.futures import ThreadPoolExecutor
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

            asyncio.run(self.plugin.begin_monitor_session("monitor-a", 1))
            second = self.plugin._telemetry_topology()

        self.assertIsNot(first, second)
        self.assertEqual(discover.call_count, 2)
        self.assertEqual(self.plugin.telemetry_topology_epoch, 1)
        self.assertEqual(self.plugin.gpu_fdinfo_paths, [])
        self.assertEqual(self.plugin.gpu_fdinfo_refresh, 0.0)

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
        self.plugin.gpu_fdinfo_paths = []
        self.plugin.gpu_fdinfo_refresh = 0.0

        def refresh():
            self.plugin.gpu_fdinfo_paths = []
            self.plugin.gpu_fdinfo_refresh = main.time.monotonic()

        self.plugin._refresh_gpu_fdinfo_paths = mock.Mock(side_effect=refresh)
        with mock.patch.object(
                main.time, "monotonic",
                side_effect=[100.0, 100.0, 105.0, 111.0, 111.0]):
            self.assertEqual(self.plugin._gpu_engine_time(), 0)
            self.assertEqual(self.plugin._gpu_engine_time(), 0)
            self.assertEqual(self.plugin._gpu_engine_time(), 0)

        self.assertEqual(self.plugin._refresh_gpu_fdinfo_paths.call_count, 2)

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
