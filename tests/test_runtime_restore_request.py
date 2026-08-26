import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.modules.setdefault("decky", types.SimpleNamespace(
    logger=types.SimpleNamespace(
        info=lambda *_: None, warning=lambda *_: None, error=lambda *_: None)))

import main


class RuntimeRestoreRequestTests(unittest.TestCase):
    def test_guard_observes_explicit_clean_unload_request(self):
        guard = (ROOT / "runtime-restore-guard.sh").read_text()

        self.assertIn('restore_request="${marker}.restore-request"', guard)
        self.assertIn('[ ! -e "$restore_request" ]', guard)

    def test_restore_consumes_request_after_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "runtime-session.active"
            request = root / "runtime-session.active.restore-request"
            state = root / "runtime-session.json"
            canonical = root / "canonical.conf"
            target = root / "fancontrol.conf"
            marker.write_text("123\n")
            request.write_text("restore\n")
            canonical.write_text("SPEEDS=(0)\nTEMPS=(0)\n")
            state.write_text(json.dumps({
                "version": 1,
                "boot_id": "different-boot",
                "controls": {"cpu": [], "gpu": None,
                             "scheduler": None,
                             "fan": {"applied": False}},
            }))

            result = subprocess.run([
                sys.executable, str(ROOT / "runtime-restore.py"),
                str(marker), str(state), str(canonical), str(target),
            ], check=False, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists())
            self.assertFalse(state.exists())
            self.assertFalse(request.exists())

    def test_failed_detached_launch_leaves_request_for_existing_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = main.Plugin.__new__(main.Plugin)
            plugin.runtime_marker = root / "runtime-session.active"
            plugin.runtime_marker.write_text("123\n")
            plugin.runtime_restore_request = (
                root / "runtime-session.active.restore-request")
            plugin.runtime_restore_path = root / "runtime-restore.py"
            plugin.runtime_state_path = root / "runtime-session.json"
            plugin.canonical_fan_config = root / "canonical.conf"
            plugin._install_runtime_restore_tools = mock.Mock()

            with mock.patch.object(
                    main, "_run", side_effect=RuntimeError("systemd failed")):
                result = plugin._request_detached_runtime_restore()

            self.assertEqual(result, "guard")
            self.assertTrue(plugin.runtime_restore_request.exists())

    def test_tool_refresh_failure_still_leaves_request_for_existing_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = main.Plugin.__new__(main.Plugin)
            plugin.runtime_marker = root / "runtime-session.active"
            plugin.runtime_marker.write_text("123\n")
            plugin.runtime_restore_request = (
                root / "runtime-session.active.restore-request")
            plugin._install_runtime_restore_tools = mock.Mock(
                side_effect=RuntimeError("copy failed"))

            with self.assertRaisesRegex(RuntimeError, "copy failed"):
                plugin._request_detached_runtime_restore()

            self.assertTrue(plugin.runtime_restore_request.exists())


if __name__ == "__main__":
    unittest.main()
