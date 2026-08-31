import json
import os
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
    def run_guard(self, statuses, *, show_pids=None, request=False,
                  owner_pid=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            marker = root / "runtime-session.active"
            restore_request = root / "runtime-session.active.restore-request"
            state = root / "runtime-session.json"
            canonical = root / "canonical.conf"
            target = root / "fancontrol.conf"
            restored = root / "restored"
            status_index = root / "status-index"
            show_index = root / "show-index"
            status_values = root / "status-values"
            show_values = root / "show-values"

            marker.write_text(f"{owner_pid or os.getpid()}\n")
            state.write_text("{}\n")
            canonical.write_text("SPEEDS=(0)\nTEMPS=(0)\n")
            if request:
                restore_request.write_text("restore\n")
            status_index.write_text("0\n")
            show_index.write_text("0\n")
            status_values.write_text("\n".join(statuses) + "\n")
            show_values.write_text("\n".join(
                str(value) for value in (show_pids or [os.getpid()])) + "\n")

            systemctl = fake_bin / "systemctl"
            systemctl.write_text("""#!/bin/sh
case "$1" in
    show)
        index=$(cat "$FAKE_SHOW_INDEX")
        index=$((index + 1))
        printf '%s\n' "$index" > "$FAKE_SHOW_INDEX"
        value=$(sed -n "${index}p" "$FAKE_SHOW_VALUES")
        [ -n "$value" ] || value=$(tail -n 1 "$FAKE_SHOW_VALUES")
        printf '%s\n' "$value"
        ;;
    is-active)
        index=$(cat "$FAKE_STATUS_INDEX")
        index=$((index + 1))
        printf '%s\n' "$index" > "$FAKE_STATUS_INDEX"
        value=$(sed -n "${index}p" "$FAKE_STATUS_VALUES")
        [ -n "$value" ] || value=$(tail -n 1 "$FAKE_STATUS_VALUES")
        [ "$value" = active ]
        ;;
    *) exit 1 ;;
esac
""")
            systemctl.chmod(0o755)
            sleep = fake_bin / "sleep"
            sleep.write_text("#!/bin/sh\nexit 0\n")
            sleep.chmod(0o755)
            restore = root / "restore"
            restore.write_text("""#!/bin/sh
printf 'restored\n' >> "$FAKE_RESTORED"
rm -f "$1" "$2"
exit 0
""")
            restore.chmod(0o755)

            environment = os.environ.copy()
            environment.update({
                "PATH": f"{fake_bin}:{environment.get('PATH', '')}",
                "FAKE_SHOW_INDEX": str(show_index),
                "FAKE_SHOW_VALUES": str(show_values),
                "FAKE_STATUS_INDEX": str(status_index),
                "FAKE_STATUS_VALUES": str(status_values),
                "FAKE_RESTORED": str(restored),
            })
            result = subprocess.run([
                "sh", str(ROOT / "runtime-restore-guard.sh"), str(marker),
                str(state), str(restore), str(canonical), str(target),
            ], check=False, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=3, env=environment)
            return {
                "result": result,
                "status_calls": int(status_index.read_text()),
                "show_calls": int(show_index.read_text()),
                "restore_calls": (
                    restored.read_text().splitlines() if restored.exists()
                    else []),
            }

    def test_guard_observes_explicit_clean_unload_request(self):
        guard = (ROOT / "runtime-restore-guard.sh").read_text()

        self.assertIn('restore_request="${marker}.restore-request"', guard)
        self.assertIn('[ ! -e "$restore_request" ]', guard)
        self.assertIn('loader_start=$(process_start_ticks "$loader_pid"', guard)
        self.assertIn('same_process "$loader_pid" "$loader_start"', guard)

    def test_guard_requires_three_consecutive_inactive_probes(self):
        outcome = self.run_guard(["inactive", "inactive", "inactive"])

        self.assertEqual(outcome["result"].returncode, 0)
        self.assertEqual(outcome["status_calls"], 3)
        self.assertEqual(outcome["restore_calls"], ["restored"])

    def test_active_probe_resets_the_inactive_exit_count(self):
        outcome = self.run_guard([
            "inactive", "active", "inactive", "inactive", "inactive",
        ])

        self.assertEqual(outcome["result"].returncode, 0)
        self.assertEqual(outcome["status_calls"], 5)
        self.assertEqual(outcome["restore_calls"], ["restored"])

    def test_explicit_request_bypasses_steam_exit_debounce(self):
        outcome = self.run_guard(["active"], request=True)

        self.assertEqual(outcome["result"].returncode, 0)
        self.assertEqual(outcome["status_calls"], 0)
        self.assertEqual(outcome["restore_calls"], ["restored"])

    def test_owner_death_bypasses_steam_exit_debounce(self):
        outcome = self.run_guard(["active"], owner_pid=99999999)

        self.assertEqual(outcome["result"].returncode, 0)
        self.assertEqual(outcome["status_calls"], 0)
        self.assertEqual(outcome["restore_calls"], ["restored"])

    def test_loader_pid_change_bypasses_steam_exit_debounce(self):
        owner = os.getpid()
        outcome = self.run_guard(
            ["active"], show_pids=[owner, owner, 1])

        self.assertEqual(outcome["result"].returncode, 0)
        self.assertEqual(outcome["status_calls"], 1)
        self.assertEqual(outcome["show_calls"], 3)
        self.assertEqual(outcome["restore_calls"], ["restored"])

    def test_invalid_initial_loader_identity_fails_closed(self):
        outcome = self.run_guard(["active"], show_pids=[0])

        self.assertEqual(outcome["result"].returncode, 0)
        self.assertEqual(outcome["status_calls"], 0)
        self.assertEqual(outcome["restore_calls"], ["restored"])

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
