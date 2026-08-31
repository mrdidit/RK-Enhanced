import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest import mock


sys.modules.setdefault("decky", types.SimpleNamespace(
    logger=types.SimpleNamespace(
        info=lambda *_: None,
        warning=lambda *_: None,
        error=lambda *_: None,
        exception=lambda *_: None,
    )))

import main


class BackendSafetyBase:
    async def asyncSetUp(self):
        async def inline_to_thread(function, *arguments):
            return function(*arguments)
        self._to_thread_patcher = mock.patch.object(
            main.asyncio, "to_thread", new=inline_to_thread)
        self._to_thread_patcher.start()
        self._lock_temporary = tempfile.TemporaryDirectory()
        self._install_lock_patcher = mock.patch.object(
            main, "INSTALL_TRANSACTION_LOCK",
            Path(self._lock_temporary.name) / "install.lock")
        self._install_lock_patcher.start()

    async def asyncTearDown(self):
        self._install_lock_patcher.stop()
        self._lock_temporary.cleanup()
        self._to_thread_patcher.stop()

    def make_plugin(self, root):
        homebrew = root / "homebrew"
        plugin = main.Plugin.__new__(main.Plugin)
        plugin.settings_dir = homebrew / "settings" / "RK-Enhanced"
        plugin.settings_dir.mkdir(parents=True, exist_ok=True)
        plugin.plugins_root = homebrew / "plugins"
        plugin.plugins_root.mkdir(parents=True, exist_ok=True)
        plugin.backup_root = homebrew / "plugin-backups"
        plugin.install_progress_path = (
            plugin.settings_dir / main.INSTALL_PROGRESS_FILE)
        plugin.install_progress_ack_path = (
            plugin.settings_dir / main.INSTALL_PROGRESS_ACK_FILE)
        plugin.install_status_generation = 0
        plugin.install_status_transaction_id = ""
        plugin.plugin_conflict_fingerprint = ""
        plugin.log_offsets = {}
        plugin.latest_release_cache = (0.0, [])
        plugin.lock = None
        plugin.rgb_lock = None
        return plugin
    @staticmethod
    def write_plugin(root, directory, name="ROCKNIX Control", version="0.1.2"):
        target = root / directory
        target.mkdir()
        (target / "plugin.json").write_text(json.dumps({
            "name": name,
            "version": version,
        }))
        return target

    @staticmethod
    def progress(generation=1, active=True, **changes):
        now = int(time.time())
        identity = main._process_identity(os.getpid()) or {
            "pid": os.getpid(), "start_time_ticks": 1,
        }
        value = {
            "protocol": 1,
            "transaction_id": "12345678-1234-4234-8234-123456789abc",
            "generation": generation,
            "active": active,
            "terminal": not active,
            "kind": "update",
            "source_version": "v0.2.0-beta.9",
            "target_version": "v0.2.0-beta.10",
            "decky_version": "v3.2.8-pre1",
            "phase": "downloading" if active else "completed",
            "message": "Downloading" if active else "Completed",
            "outcome": "running" if active else "succeeded",
            "started_at": now - 10,
            "updated_at": now,
            "writer": {
                "pid": identity["pid"],
                "start_time_ticks": identity["start_time_ticks"],
                "boot_id": Path(
                    "/proc/sys/kernel/random/boot_id").read_text().strip(),
            },
            "success": None if active else True,
            "rolled_back": False,
            "error": "",
        }
        value.update(changes)
        return value


class AtomicUpdaterStagingTests(unittest.TestCase):
    def test_atomic_copy_replaces_regular_file_but_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.sh"
            source.write_text("#!/bin/sh\necho safe\n")
            target = root / "updater.sh"
            target.write_text("old\n")

            main._atomic_executable_copy(source, target)

            self.assertEqual(target.read_text(), source.read_text())
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)

            outside = root / "outside"
            outside.write_text("must remain\n")
            target.unlink()
            target.symlink_to(outside)
            with self.assertRaisesRegex(RuntimeError, "unsafe updater"):
                main._atomic_executable_copy(source, target)
            self.assertTrue(target.is_symlink())
            self.assertEqual(outside.read_text(), "must remain\n")


class PluginConflictTests(BackendSafetyBase, unittest.IsolatedAsyncioTestCase):
    async def test_startup_logs_odin_rgb_restore_as_state_not_animation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = self.make_plugin(root)
            plugin.runtime_marker = root / "runtime-session.active"
            plugin.legacy_fan_guard_marker = root / "legacy-fan.active"
            plugin.lifecycle_heartbeat_task = None
            plugin.game_watch_task = None
            plugin.startup_rgb_pending = False
            plugin.rgb = mock.Mock()
            plugin.rgb.reapply_startup.return_value = (
                main.ODIN3_RGB_STARTUP_ACTION)
            plugin._start_plugin_lifecycle_guard = mock.Mock()
            plugin._load = mock.Mock(return_value={})
            plugin._restore_runtime_session = mock.Mock()
            plugin._restore_legacy_system_fan_curve = mock.Mock()
            plugin._publish_backend_install_health = mock.Mock()
            plugin._install_status = mock.Mock(return_value={"active": False})
            plugin._require_mutations_allowed = mock.Mock()
            plugin._hardware_mutation_guard = mock.Mock(
                return_value=main._exclusive_file_lock(
                    Path(self._lock_temporary.name) / "rgb-startup.lock"))

            async def completed_loop():
                return None

            plugin._lifecycle_heartbeat_loop = completed_loop
            plugin._game_watch_loop = completed_loop
            with mock.patch.object(main.decky.logger, "info") as info:
                await plugin._main()
                await asyncio.sleep(0)

            plugin.rgb.reapply_startup.assert_called_once_with()
            info.assert_any_call(
                "Restored the saved Odin 3 RGB state for this boot")
            self.assertFalse(any(
                call.args == ("Reapplied the saved native RGB animation",)
                for call in info.call_args_list))

    async def test_exact_normalized_manifest_detects_both_legacy_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            plugin = self.make_plugin(Path(temporary))
            first = self.write_plugin(
                plugin.plugins_root, "rocknix-control", "ROCKNIX Control")
            second = self.write_plugin(
                plugin.plugins_root, "enhanced", "  rocknix control  ", "0.1.3")
            self.write_plugin(
                plugin.plugins_root, "unrelated", "ROCKNIX Fan Viewer")

            result = await plugin.get_plugin_conflict()

            self.assertTrue(result["blocked"])
            self.assertEqual(
                [item["directory"] for item in result["conflicts"]],
                [str(second), str(first)],
            )
            self.assertTrue(all(
                item["removable"] for item in result["conflicts"]))

    async def test_symlink_conflict_blocks_but_cannot_be_auto_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = self.make_plugin(root)
            outside = root / "outside-control"
            outside.mkdir()
            (outside / "plugin.json").write_text(json.dumps({
                "name": "ROCKNIX Control", "version": "0.1.2",
            }))
            linked = plugin.plugins_root / "linked-control"
            linked.symlink_to(outside, target_is_directory=True)

            result = await plugin.get_plugin_conflict()

            self.assertTrue(result["blocked"])
            self.assertEqual(len(result["conflicts"]), 1)
            self.assertFalse(result["conflicts"][0]["removable"])
            with self.assertRaisesRegex(RuntimeError, "cannot be removed"):
                await plugin.remove_plugin_conflict()
            self.assertTrue(outside.exists())

    async def test_hardware_and_preset_apply_fail_before_capability_reads(self):
        with tempfile.TemporaryDirectory() as temporary:
            plugin = self.make_plugin(Path(temporary))
            self.write_plugin(plugin.plugins_root, "legacy")
            with mock.patch.object(main, "_capabilities") as capabilities:
                with self.assertRaisesRegex(RuntimeError, "ROCKNIX Control"):
                    await plugin.apply_profile({})
            capabilities.assert_not_called()

    async def test_conflict_blocks_experimental_visibility_settings_rpcs(self):
        with tempfile.TemporaryDirectory() as temporary:
            plugin = self.make_plugin(Path(temporary))
            self.write_plugin(plugin.plugins_root, "legacy")
            plugin._load = mock.Mock()
            plugin._save = mock.Mock()

            with self.assertRaisesRegex(RuntimeError, "ROCKNIX Control"):
                await plugin.unlock_experimental("bypasstest")
            with self.assertRaisesRegex(RuntimeError, "ROCKNIX Control"):
                await plugin.lock_experimental()

            plugin._load.assert_not_called()
            plugin._save.assert_not_called()

    async def test_install_lock_blocks_mutation_before_first_progress_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            plugin = self.make_plugin(Path(temporary))
            with main._exclusive_file_lock(main.INSTALL_TRANSACTION_LOCK):
                with mock.patch.object(main, "_capabilities") as capabilities:
                    with self.assertRaisesRegex(
                            RuntimeError, "installation is starting"):
                        await plugin.apply_profile({})
            capabilities.assert_not_called()

    async def test_profile_holds_install_lock_for_complete_hardware_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            plugin = self.make_plugin(Path(temporary))

            def hardware(_profile, _capabilities=None):
                with self.assertRaises(TimeoutError):
                    with main._exclusive_file_lock(
                            main.INSTALL_TRANSACTION_LOCK, timeout=0):
                        pass
                return True

            plugin._apply_hardware = hardware

            self.assertTrue(await plugin.apply_profile({"ignored": True}))

    async def test_startup_restores_owned_runtime_but_defers_legacy_fan_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = self.make_plugin(root)
            self.write_plugin(plugin.plugins_root, "legacy")
            plugin.runtime_marker = root / "runtime-session.active"
            plugin.runtime_marker.write_text("1\n")
            plugin.legacy_fan_guard_marker = root / "fan-curve-session.active"
            plugin.legacy_fan_guard_marker.write_text("1\n")
            plugin.lifecycle_heartbeat_task = None
            plugin.game_watch_task = None
            plugin.startup_rgb_pending = False
            plugin.rgb = mock.Mock()
            plugin._start_plugin_lifecycle_guard = mock.Mock()
            plugin._load = mock.Mock(return_value={})
            plugin._restore_runtime_session = mock.Mock(return_value=True)
            plugin._restore_legacy_system_fan_curve = mock.Mock(return_value=True)
            plugin._publish_backend_install_health = mock.Mock()

            async def completed_loop():
                return None

            plugin._lifecycle_heartbeat_loop = completed_loop
            plugin._game_watch_loop = completed_loop

            await plugin._main()
            await asyncio.sleep(0)

            plugin._restore_runtime_session.assert_called_once_with()
            plugin._restore_legacy_system_fan_curve.assert_not_called()
            plugin.rgb.reapply_startup.assert_not_called()

    async def test_detached_removal_never_deletes_inside_pluginloader(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = self.make_plugin(root)
            conflict = self.write_plugin(plugin.plugins_root, "legacy")
            before = self.progress(generation=4, active=False)
            after = self.progress(
                generation=5,
                kind="remove-conflict",
                transaction_id="87654321-4321-4321-8321-cba987654321",
            )
            commands = []

            def fake_run(command, check=True, timeout=15):
                commands.append(command)
                return ""

            with mock.patch.object(
                    plugin, "_install_status",
                    side_effect=[before, before, after]), \
                    mock.patch.object(main, "_run", side_effect=fake_run):
                result = await plugin.remove_plugin_conflict()

            self.assertTrue(result["started"])
            self.assertEqual(result["transaction_id"], after["transaction_id"])
            self.assertTrue(conflict.exists())
            detached = plugin.settings_dir / "maintenance-updater.sh"
            self.assertTrue(detached.is_file())
            removal_commands = [
                command for command in commands
                if "--remove-rocknix-control" in command
            ]
            self.assertEqual(len(removal_commands), 1)
            removal = removal_commands[0]
            self.assertEqual(
                removal[-4:-1],
                [str(detached), "--remove-rocknix-control", str(conflict)],
            )
            self.assertRegex(removal[-1], r"^[0-9a-f]{64}$")


class InstallProgressTests(BackendSafetyBase, unittest.IsolatedAsyncioTestCase):
    async def test_live_progress_accepts_null_success_and_is_generation_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            plugin = self.make_plugin(Path(temporary))
            plugin.install_progress_path.write_text(json.dumps(
                self.progress(generation=2)))

            status = await plugin.get_install_status()

            self.assertTrue(status["active"])
            self.assertIsNone(status["success"])
            self.assertFalse(status["acknowledged"])
            self.assertEqual(plugin.install_status_generation, 2)

            plugin.install_progress_path.write_text(json.dumps(
                self.progress(generation=1)))
            rejected = await plugin.get_install_status()
            self.assertEqual(rejected["phase"], "unavailable")
            self.assertIn("stale", rejected["message"].lower())

    async def test_terminal_acknowledgement_is_exact_and_persistent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = self.make_plugin(root)
            payload = self.progress(active=False)
            plugin.install_progress_path.write_text(json.dumps(payload))

            with self.assertRaisesRegex(ValueError, "current completed"):
                await plugin.ack_install_status(
                    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
            self.assertTrue(await plugin.ack_install_status(
                payload["transaction_id"]))
            self.assertTrue((await plugin.get_install_status())["acknowledged"])

            restarted = self.make_plugin(root)
            self.assertTrue(
                (await restarted.get_install_status())["acknowledged"])

    async def test_stale_active_progress_is_tombstoned_only_without_live_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = self.make_plugin(root)
            lock = root / "run" / "install.lock"
            payload = self.progress(
                started_at=100, updated_at=200,
            )
            plugin.install_progress_path.write_text(json.dumps(payload))

            with mock.patch.object(main, "INSTALL_TRANSACTION_LOCK", lock), \
                    mock.patch.object(main.time, "time", return_value=2_000):
                with main._exclusive_file_lock(lock):
                    live = await plugin.get_install_status()
                self.assertTrue(live["active"])

                interrupted = await plugin.get_install_status()

            self.assertFalse(interrupted["active"])
            self.assertTrue(interrupted["terminal"])
            self.assertEqual(interrupted["phase"], "failed")
            self.assertIn("interrupted", interrupted["message"].lower())
            self.assertFalse(json.loads(
                plugin.install_progress_path.read_text())["active"])


class BackupCleanupTests(BackendSafetyBase, unittest.IsolatedAsyncioTestCase):
    async def test_creation_marker_beats_preserved_live_directory_mtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            plugin = self.make_plugin(Path(temporary))
            plugin.backup_root.mkdir()
            older = plugin.backup_root / (
                "RK-Enhanced-before-beta10-20990829-120000.aaaaaa")
            newest = plugin.backup_root / (
                "RK-Enhanced-before-beta11-20260830-120000.bbbbbb")
            older.mkdir()
            newest.mkdir()
            # Moving the formerly live directory preserves this misleading
            # old inode mtime; it must not make the new snapshot disposable.
            os.utime(newest, (1, 1))
            os.utime(older, (4_000_000_000, 4_000_000_000))
            marker = {
                "protocol": 1,
                "created_at": int(time.time()),
                "transaction_id": "12345678-1234-4234-8234-123456789abc",
            }
            (newest / ".rke-backup-created.json").write_text(
                json.dumps(marker))
            # Writing the marker mutates mtime; put it back to reproduce mv.
            os.utime(newest, (1, 1))

            info = await plugin.get_backup_cleanup_info()

            self.assertEqual(info["kept"]["name"], newest.name)
            self.assertEqual(info["kept"]["created_source"], "marker")
            self.assertEqual(
                [item["name"] for item in info["removable"]], [older.name])

    async def test_cleanup_keeps_newest_and_never_touches_unknown_or_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = self.make_plugin(root)
            plugin.backup_root.mkdir()
            oldest = plugin.backup_root / "RK-Enhanced-before-beta8-old"
            middle = plugin.backup_root / "RK-Enhanced-before-beta9-middle"
            newest = plugin.backup_root / "RK-Enhanced-before-beta10-new"
            for index, path in enumerate((oldest, middle, newest), 1):
                path.mkdir()
                (path / "payload").write_bytes(b"x" * index)
                os.utime(path, (index, index))
            recovery = plugin.backup_root / "update-recovery-beta10-keep"
            recovery.mkdir()
            unknown = plugin.backup_root / "another-plugin-before-old"
            unknown.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (outside / "must-remain").write_text("safe")
            linked = plugin.backup_root / "RK-Enhanced-before-linked"
            linked.symlink_to(outside, target_is_directory=True)
            inside_link = middle / "external-link"
            inside_link.symlink_to(outside, target_is_directory=True)
            os.utime(middle, (2, 2))
            lock = root / "run" / "install.lock"

            info = await plugin.get_backup_cleanup_info()
            self.assertEqual(info["eligible_count"], 2)
            self.assertEqual(info["eligible_bytes"], 3)
            self.assertEqual(info["kept"]["name"], newest.name)

            with mock.patch.object(main, "INSTALL_TRANSACTION_LOCK", lock):
                result = await plugin.clean_old_backups()

            self.assertEqual(result["removed_count"], 2)
            self.assertEqual(result["removed_bytes"], 3)
            self.assertTrue(newest.is_dir())
            self.assertFalse(oldest.exists())
            self.assertFalse(middle.exists())
            self.assertTrue(recovery.is_dir())
            self.assertTrue(unknown.is_dir())
            self.assertTrue(linked.is_symlink())
            self.assertEqual((outside / "must-remain").read_text(), "safe")


class CombinedLogAndReleaseTests(BackendSafetyBase, unittest.IsolatedAsyncioTestCase):
    async def test_installer_and_plugin_logs_are_merged_in_time_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = self.make_plugin(root)
            log_dir = root / "logs"
            log_dir.mkdir()
            (log_dir / "backend.log").write_text(
                "[2026-08-30 12:00:01] backend ready\n")
            (log_dir / "installer.log").write_text(
                "2026-08-30 12:00:00 [tx] [install] [starting] [running] started\n"
                "2026-08-30 12:00:02 [tx] [install] [completed] [succeeded] done\n")

            with mock.patch.dict(
                    os.environ, {"DECKY_PLUGIN_LOG_DIR": str(log_dir)}):
                merged = await plugin.get_log()

            lines = merged.splitlines()
            self.assertEqual(len(lines), 3)
            self.assertTrue(lines[0].startswith("[2026-08-30 12:00:00]"))
            self.assertIn("backend ready", lines[1])
            self.assertTrue(lines[2].startswith("[2026-08-30 12:00:02]"))

    async def test_previous_published_and_actual_last_installed_are_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = self.make_plugin(root)
            plugin_dir = root / "plugin" / "RK-Enhanced"
            plugin_dir.mkdir(parents=True)
            (plugin_dir / "VERSION").write_text("v0.2.0-beta.10\n")
            (plugin.settings_dir / main.LAST_INSTALLED_VERSION_FILE).write_text(
                "v0.2.0-beta.8\n")
            releases = [
                {"tag_name": version, "draft": False,
                 "assets": [{"name": "RK-Enhanced.zip"}]}
                for version in (
                    "v0.2.0-beta.10", "v0.2.0-beta.9", "v0.2.0-beta.8")
            ]

            with mock.patch.object(main, "__file__", str(plugin_dir / "main.py")), \
                    mock.patch.object(main, "_run", return_value=json.dumps(releases)):
                info = await plugin.get_update_info()

            self.assertEqual(info["previous"], "v0.2.0-beta.9")
            self.assertEqual(info["previous_published"], "v0.2.0-beta.9")
            self.assertEqual(info["last_installed"], "v0.2.0-beta.8")
            self.assertTrue(info["last_installed_available"])


if __name__ == "__main__":
    unittest.main()
