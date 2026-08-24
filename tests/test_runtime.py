import json
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

    async def test_incoherent_refresh_advances_revision_even_when_bypass_is_clear(self):
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

        self.assertEqual(status["charging_revision"], initial_revision + 1)
        self.assertFalse(plugin.monitor_bypass_active)
        self.assertIsNone(plugin.battery_discharge_ema)

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


class FrontendLifecycleContractTests(unittest.TestCase):
    def test_quick_access_visibility_gates_both_charging_pollers(self):
        content = (ROOT / "src" / "Content.tsx").read_text()
        index = (ROOT / "src" / "index.tsx").read_text()

        self.assertIn("alwaysRender: true", index)
        self.assertIn("const panelVisible = useQuickAccessVisible();", content)
        self.assertIn(
            'active={panelVisible && tab === "Monitor"}', content)
        self.assertIn(
            'active={panelVisible && tab === "Experimental"}', content)

    def test_monitor_always_renders_policy_failure_or_unsupported_state(self):
        monitor = (ROOT / "src" / "Monitor.tsx").read_text()

        self.assertIn(
            'const batteryPolicyRow = <Metric label="Battery policy"', monitor)
        self.assertGreaterEqual(monitor.count("{batteryPolicyRow}"), 2)
        self.assertIn('const batteryPolicyLabel = chargingError ? "Unavailable"', monitor)
        self.assertNotIn(
            'batteryPolicy?.available && <Metric label="Battery policy"', monitor)

    def test_monitor_labels_existing_battery_side_charging_power(self):
        monitor = (ROOT / "src" / "Monitor.tsx").read_text()

        self.assertIn(
            'data.battery_status === "Charging" ? "Battery charge power" : "Power draw"',
            monitor,
        )
        self.assertNotIn(
            'data.battery_status === "Charging" ? "Charging power" : "Power draw"',
            monitor,
        )
        self.assertIn("data.battery_watts.toFixed(1)", monitor)

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
