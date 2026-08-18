import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class RuntimeRestoreTests(unittest.TestCase):
    def run_restore(self, root, control=None, gpu=None, charging=None,
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
                "charging": charging,
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

    def test_restores_gpu_charging_and_protected_fan_curve(self):
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
            charging = {
                "path": str(charging_path), "baseline": "auto",
                "applied": "inhibit-charge",
            }

            result, _, _ = self.run_restore(
                root, gpu=gpu, charging=charging, fan_applied=True)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((gpu_path / "governor").read_text(), "msm-adreno-tz")
            self.assertEqual((gpu_path / "min_freq").read_text(), "100")
            self.assertEqual((gpu_path / "max_freq").read_text(), "800")
            self.assertEqual(charging_path.read_text(), "auto")
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


if __name__ == "__main__":
    unittest.main()
