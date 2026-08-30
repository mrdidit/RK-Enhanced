import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock


sys.modules.setdefault("decky", types.SimpleNamespace(
    logger=types.SimpleNamespace(
        info=lambda *_: None, warning=lambda *_: None, error=lambda *_: None)))

import main


class PluginLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def lifecycle_plugin(self, root):
        plugin = main.Plugin.__new__(main.Plugin)
        plugin.settings_dir = root / "settings"
        plugin.plugin_loader_recovery_path = (
            plugin.settings_dir / "plugin_loader_recovery.py")
        plugin.lifecycle_heartbeat_task = None
        plugin.lifecycle_token = ""
        plugin.lifecycle_lease_path = None
        plugin.lifecycle_active_path = None
        plugin.lifecycle_heartbeat_path = None
        plugin.lifecycle_ready_path = None
        plugin.lifecycle_guard_unit = ""
        plugin.lifecycle_guard_identity = None
        return plugin

    async def test_guard_lease_uses_exact_owner_and_loader_identities(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_root = root / "run" / "rk-enhanced"
            current = run_root / "plugin-lifecycle-current.json"
            lifecycle_lock = root / "run" / "lock" / "recovery.lock"
            plugin = self.lifecycle_plugin(root)
            owner = {
                "pid": 111, "start_time_ticks": 1111, "parent_pid": 100,
            }
            loader = {
                "pid": 222, "start_time_ticks": 2222, "parent_pid": 1,
            }
            guard = {
                "pid": 333, "start_time_ticks": 3333, "parent_pid": 1,
            }
            commands = []

            def fake_run(command, check=True, timeout=15):
                commands.append((command, check, timeout))
                if command[:2] == ["systemctl", "show"]:
                    return ("222" if command[-1] == main.PLUGIN_LOADER_SERVICE
                            else "333")
                if command[0] == "systemd-run":
                    (run_root / (
                        "plugin-lifecycle-" + "a" * 32 + ".ready")
                     ).write_text("a" * 32 + "\n")
                return ""

            def fake_read(path, default=""):
                if str(path) == "/proc/sys/kernel/random/boot_id":
                    return "boot-id"
                try:
                    return Path(path).read_text().strip()
                except OSError:
                    return default

            with mock.patch.object(main, "LIFECYCLE_RUN_ROOT", run_root), \
                    mock.patch.object(main, "LIFECYCLE_CURRENT", current), \
                    mock.patch.object(main, "LIFECYCLE_LOCK", lifecycle_lock), \
                    mock.patch.object(
                        main, "_process_identity",
                        side_effect=[owner, loader, guard]), \
                    mock.patch.object(main.os, "getpid", return_value=111), \
                    mock.patch.object(main.secrets, "token_hex", return_value="a" * 32), \
                    mock.patch.object(main, "_read", side_effect=fake_read), \
                    mock.patch.object(main, "_run", side_effect=fake_run):
                plugin._start_plugin_lifecycle_guard()

                lease = json.loads(plugin.lifecycle_lease_path.read_text())
                self.assertEqual(lease["owner"], owner)
                self.assertEqual(lease["loader"], loader)
                self.assertEqual(lease["boot_id"], "boot-id")
                self.assertEqual(lease["token"], "a" * 32)
                self.assertEqual(json.loads(current.read_text()), lease)
                self.assertTrue(plugin.lifecycle_active_path.exists())
                self.assertTrue(plugin.lifecycle_heartbeat_path.exists())
                self.assertEqual(
                    plugin.lifecycle_ready_path.read_text().strip(), "a" * 32)
                self.assertGreater(
                    int(plugin.lifecycle_heartbeat_path.read_text()), 0)
                self.assertEqual(
                    stat.S_IMODE(plugin.plugin_loader_recovery_path.stat().st_mode),
                    0o755,
                )
                self.assertEqual(
                    plugin.lifecycle_guard_unit,
                    "rke-plugin-lifecycle-guard-111-aaaaaaaa")
                self.assertEqual(plugin.lifecycle_guard_identity, guard)
                systemd_run = next(
                    command for command in commands
                    if command[0][0] == "systemd-run")
                self.assertEqual(commands[-1], ([
                    "systemctl", "show", "--property=MainPID", "--value",
                    "rke-plugin-lifecycle-guard-111-aaaaaaaa",
                ], True, 3))
                self.assertEqual(systemd_run, ([
                    "systemd-run",
                    "--unit=rke-plugin-lifecycle-guard-111-aaaaaaaa",
                    "--collect",
                    str(plugin.plugin_loader_recovery_path),
                    "guard",
                    str(plugin.lifecycle_lease_path),
                ], True, 3))

                active_path = plugin.lifecycle_active_path
                heartbeat_path = plugin.lifecycle_heartbeat_path
                ready_path = plugin.lifecycle_ready_path
                self.assertTrue(plugin._mark_lifecycle_guard_clean())
                self.assertFalse(active_path.exists())
                self.assertFalse(heartbeat_path.exists())
                self.assertFalse(ready_path.exists())
                self.assertFalse(current.exists())
                self.assertEqual(plugin.lifecycle_guard_unit, "")
                self.assertIsNone(plugin.lifecycle_guard_identity)

    async def test_clean_unload_tombstones_guard_before_restore_handoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            plugin = self.lifecycle_plugin(Path(temporary))
            events = []
            plugin.lifecycle_heartbeat_task = asyncio.create_task(
                asyncio.sleep(3600))
            plugin.game_watch_task = asyncio.create_task(asyncio.sleep(3600))

            def clean():
                events.append("clean")
                return True

            def restore():
                events.append("restore")
                return True

            plugin._mark_lifecycle_guard_clean = clean
            plugin._request_detached_runtime_restore = restore

            async def inline_to_thread(function, *arguments):
                return function(*arguments)

            with mock.patch.object(
                    main.asyncio, "to_thread", new=inline_to_thread):
                await plugin._unload()

            self.assertEqual(events, ["clean", "restore"])
            self.assertIsNone(plugin.lifecycle_heartbeat_task)
            self.assertIsNone(plugin.game_watch_task)

    async def test_guard_start_failure_removes_partial_generation_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_root = root / "run" / "rk-enhanced"
            current = run_root / "plugin-lifecycle-current.json"
            lifecycle_lock = root / "run" / "lock" / "recovery.lock"
            plugin = self.lifecycle_plugin(root)
            owner = {
                "pid": 111, "start_time_ticks": 1111, "parent_pid": 100,
            }
            loader = {
                "pid": 222, "start_time_ticks": 2222, "parent_pid": 1,
            }
            moments = iter((0.0, 1.0, 6.0, 7.0))

            def fake_run(command, check=True, timeout=15):
                return "222" if command[:2] == ["systemctl", "show"] else ""

            def fake_read(path, default=""):
                if str(path) == "/proc/sys/kernel/random/boot_id":
                    return "boot-id"
                try:
                    return Path(path).read_text().strip()
                except OSError:
                    return default

            with mock.patch.object(main, "LIFECYCLE_RUN_ROOT", run_root), \
                    mock.patch.object(main, "LIFECYCLE_CURRENT", current), \
                    mock.patch.object(main, "LIFECYCLE_LOCK", lifecycle_lock), \
                    mock.patch.object(
                        main, "_process_identity",
                        side_effect=[owner, loader]), \
                    mock.patch.object(main.os, "getpid", return_value=111), \
                    mock.patch.object(
                        main.secrets, "token_hex", return_value="a" * 32), \
                    mock.patch.object(main, "_read", side_effect=fake_read), \
                    mock.patch.object(main, "_run", side_effect=fake_run), \
                    mock.patch.object(
                        main.time, "monotonic", side_effect=lambda: next(moments)), \
                    mock.patch.object(main.time, "sleep", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "did not become ready"):
                    plugin._start_plugin_lifecycle_guard()

            self.assertFalse(current.exists())
            self.assertEqual(list(run_root.glob("plugin-lifecycle-*")), [])
            self.assertEqual(plugin.lifecycle_token, "")

    async def test_restore_handoff_failure_does_not_hold_loader_unload_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            plugin = self.lifecycle_plugin(Path(temporary))
            plugin.game_watch_task = None
            plugin._mark_lifecycle_guard_clean = lambda: True
            plugin._request_detached_runtime_restore = mock.Mock(
                side_effect=RuntimeError("systemd unavailable"))

            async def inline_to_thread(function, *arguments):
                return function(*arguments)

            with mock.patch.object(
                    main.asyncio, "to_thread", new=inline_to_thread):
                await plugin._unload()

            plugin._request_detached_runtime_restore.assert_called_once_with()


class InstallHealthProtocolTests(unittest.IsolatedAsyncioTestCase):
    NONCE = "0123456789abcdef0123456789abcdef"
    BOOT_ID = "11111111-2222-3333-4444-555555555555"
    VERSION = "v9.8.7-test"
    LIFECYCLE_TOKEN = "abcdef0123456789abcdef0123456789"
    MAIN_CONTENT = b"test backend release\n"
    FRONTEND_PREFIX = "rke-frontend-sha256-v1:"
    FRONTEND_PLACEHOLDER = FRONTEND_PREFIX + ("0" * 64)
    DIST_TEMPLATE = (
        f'const frontendBundleId = "{FRONTEND_PLACEHOLDER}";\n'
    ).encode()
    FRONTEND_DIGEST = hashlib.sha256(DIST_TEMPLATE).hexdigest()
    FRONTEND_BUNDLE_ID = FRONTEND_PREFIX + FRONTEND_DIGEST
    DIST_CONTENT = DIST_TEMPLATE.replace(
        FRONTEND_PLACEHOLDER.encode(), FRONTEND_BUNDLE_ID.encode())

    @classmethod
    def health_plugin(cls, settings):
        plugin = main.Plugin.__new__(main.Plugin)
        plugin.settings_dir = settings
        plugin.install_health_request_path = (
            settings / main.INSTALL_HEALTH_REQUEST_FILE)
        plugin.install_backend_ready_path = (
            settings / main.INSTALL_BACKEND_READY_FILE)
        plugin.install_frontend_ready_path = (
            settings / main.INSTALL_FRONTEND_READY_FILE)
        plugin.install_health_response = None
        plugin.lifecycle_token = cls.LIFECYCLE_TOKEN
        return plugin

    def release_fixture(self, root):
        plugin_dir = root / "plugin"
        (plugin_dir / "dist").mkdir(parents=True)
        (plugin_dir / "main.py").write_bytes(self.MAIN_CONTENT)
        (plugin_dir / "dist" / "index.js").write_bytes(self.DIST_CONTENT)
        (plugin_dir / "dist" / "frontend-integrity.json").write_text(
            json.dumps({
                "protocol": 1,
                "algorithm": "sha256-normalized-v1",
                "bundle_id": self.FRONTEND_BUNDLE_ID,
                "index_sha256": hashlib.sha256(
                    self.DIST_CONTENT).hexdigest(),
            }) + "\n")
        (plugin_dir / "VERSION").write_text(self.VERSION + "\n")
        return plugin_dir

    def request_payload(self, **changes):
        payload = {
            "protocol": main.INSTALL_HEALTH_PROTOCOL,
            "nonce": self.NONCE,
            "version": self.VERSION,
            "boot_id": self.BOOT_ID,
            "main_sha256": hashlib.sha256(self.MAIN_CONTENT).hexdigest(),
            "dist_sha256": hashlib.sha256(self.DIST_CONTENT).hexdigest(),
            "frontend_bundle_id": self.FRONTEND_BUNDLE_ID,
            "require_frontend": True,
        }
        payload.update(changes)
        return payload

    @staticmethod
    def identities():
        return {
            111: {
                "pid": 111,
                "start_time_ticks": 1111,
                "parent_pid": 222,
            },
            222: {
                "pid": 222,
                "start_time_ticks": 2222,
                "parent_pid": 1,
            },
        }

    def publish_backend(self, plugin, plugin_dir, *, identities=None):
        real_read = main._read

        def fake_read(path, default=""):
            if str(path) == "/proc/sys/kernel/random/boot_id":
                return self.BOOT_ID
            return real_read(path, default)

        process_identities = self.identities() if identities is None else identities
        with mock.patch.object(main, "__file__", str(plugin_dir / "main.py")), \
                mock.patch.object(main, "_read", side_effect=fake_read), \
                mock.patch.object(main, "_run", return_value="222") as run, \
                mock.patch.object(main.os, "getpid", return_value=111), \
                mock.patch.object(
                    main, "_process_identity",
                    side_effect=lambda pid: process_identities.get(pid)), \
                mock.patch.object(main.time, "time", return_value=1700000000):
            result = plugin._publish_backend_install_health()
        return result, run

    def test_backend_response_binds_nonce_hashes_and_loader_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = root / "settings"
            settings.mkdir()
            plugin_dir = self.release_fixture(root)
            plugin = self.health_plugin(settings)
            plugin.install_health_request_path.write_text(
                json.dumps(self.request_payload()) + "\n")

            result, run = self.publish_backend(plugin, plugin_dir)

            self.assertTrue(result)
            run.assert_called_once_with([
                "systemctl", "show", "--property=MainPID", "--value",
                main.PLUGIN_LOADER_SERVICE,
            ], timeout=3)
            response = json.loads(
                plugin.install_backend_ready_path.read_text())
            self.assertEqual(response["protocol"], main.INSTALL_HEALTH_PROTOCOL)
            self.assertEqual(response["nonce"], self.NONCE)
            self.assertEqual(response["version"], self.VERSION)
            self.assertEqual(response["boot_id"], self.BOOT_ID)
            self.assertEqual(
                response["main_sha256"],
                hashlib.sha256(self.MAIN_CONTENT).hexdigest())
            self.assertEqual(
                response["dist_sha256"],
                hashlib.sha256(self.DIST_CONTENT).hexdigest())
            self.assertEqual(
                response["frontend_bundle_id"], self.FRONTEND_BUNDLE_ID)
            self.assertIs(response["require_frontend"], True)
            self.assertEqual(response["backend"], self.identities()[111])
            self.assertEqual(response["loader"], self.identities()[222])
            self.assertEqual(
                response["lifecycle_token"], self.LIFECYCLE_TOKEN)
            self.assertEqual(response["ready_at"], 1700000000)
            self.assertEqual(plugin.install_health_response, response)
            self.assertEqual(
                stat.S_IMODE(plugin.install_backend_ready_path.stat().st_mode),
                0o600,
            )

    def test_backend_rejects_a_challenge_for_different_release_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = root / "settings"
            settings.mkdir()
            plugin_dir = self.release_fixture(root)
            plugin = self.health_plugin(settings)
            plugin.install_health_request_path.write_text(json.dumps(
                self.request_payload(dist_sha256="0" * 64)) + "\n")

            result, run = self.publish_backend(plugin, plugin_dir)

            self.assertFalse(result)
            self.assertIsNone(plugin.install_health_response)
            self.assertFalse(plugin.install_backend_ready_path.exists())
            run.assert_not_called()

    def test_backend_rejects_wrong_bundle_or_non_boolean_frontend_requirement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = root / "settings"
            settings.mkdir()
            plugin_dir = self.release_fixture(root)
            plugin = self.health_plugin(settings)
            invalid_requests = (
                {
                    "frontend_bundle_id":
                        self.FRONTEND_PREFIX + ("f" * 64),
                },
                {"require_frontend": "true"},
            )

            for changes in invalid_requests:
                with self.subTest(changes=changes):
                    plugin.install_health_request_path.write_text(
                        json.dumps(self.request_payload(**changes)) + "\n")
                    result, run = self.publish_backend(plugin, plugin_dir)
                    self.assertFalse(result)
                    self.assertIsNone(plugin.install_health_response)
                    self.assertFalse(
                        plugin.install_backend_ready_path.exists())
                    run.assert_not_called()

    def test_backend_rejects_an_unidentified_loader_pid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = root / "settings"
            settings.mkdir()
            plugin_dir = self.release_fixture(root)
            plugin = self.health_plugin(settings)
            plugin.install_health_request_path.write_text(
                json.dumps(self.request_payload()) + "\n")
            identities = self.identities()
            identities[222] = None

            result, run = self.publish_backend(
                plugin, plugin_dir, identities=identities)

            self.assertFalse(result)
            run.assert_called_once()
            self.assertIsNone(plugin.install_health_response)
            self.assertFalse(plugin.install_backend_ready_path.exists())

    async def test_frontend_response_reuses_verified_backend_challenge(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = root / "settings"
            settings.mkdir()
            plugin_dir = self.release_fixture(root)
            plugin = self.health_plugin(settings)
            request = self.request_payload()
            plugin.install_health_request_path.write_text(
                json.dumps(request) + "\n")
            response = {
                **request,
                "backend": self.identities()[111],
                "loader": self.identities()[222],
                "lifecycle_token": self.LIFECYCLE_TOKEN,
                "ready_at": 1700000000,
            }
            plugin.install_health_response = response

            async def inline_to_thread(function, *arguments):
                return function(*arguments)

            with mock.patch.object(
                    main.asyncio, "to_thread", new=inline_to_thread), \
                    mock.patch.object(
                        main, "__file__", str(plugin_dir / "main.py")), \
                    mock.patch.object(
                        main.time, "time", return_value=1700000001):
                result = await plugin.report_frontend_ready(
                    self.FRONTEND_BUNDLE_ID)

            self.assertTrue(result)
            frontend = json.loads(
                plugin.install_frontend_ready_path.read_text())
            self.assertEqual(
                {key: frontend[key] for key in response}, response)
            self.assertEqual(frontend["frontend_ready_at"], 1700000001)
            self.assertEqual(
                stat.S_IMODE(plugin.install_frontend_ready_path.stat().st_mode),
                0o600,
            )

    async def test_frontend_rejects_wrong_bundle_id_for_same_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = root / "settings"
            settings.mkdir()
            plugin_dir = self.release_fixture(root)
            plugin = self.health_plugin(settings)
            request = self.request_payload()
            plugin.install_health_request_path.write_text(
                json.dumps(request) + "\n")
            plugin.install_health_response = {
                **request,
                "backend": self.identities()[111],
                "loader": self.identities()[222],
            }
            wrong_bundle = self.FRONTEND_PREFIX + ("f" * 64)

            async def inline_to_thread(function, *arguments):
                return function(*arguments)

            with mock.patch.object(
                    main.asyncio, "to_thread", new=inline_to_thread), \
                    mock.patch.object(
                        main, "__file__", str(plugin_dir / "main.py")):
                result = await plugin.report_frontend_ready(wrong_bundle)

            self.assertFalse(result)
            self.assertFalse(plugin.install_frontend_ready_path.exists())

    async def test_frontend_rejects_bundle_mutated_after_backend_response(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = root / "settings"
            settings.mkdir()
            plugin_dir = self.release_fixture(root)
            plugin = self.health_plugin(settings)
            plugin.install_health_request_path.write_text(
                json.dumps(self.request_payload()) + "\n")
            published, _ = self.publish_backend(plugin, plugin_dir)
            self.assertTrue(published)
            (plugin_dir / "dist" / "index.js").write_bytes(
                self.DIST_CONTENT + b"// post-init mutation\n")

            async def inline_to_thread(function, *arguments):
                return function(*arguments)

            with mock.patch.object(
                    main.asyncio, "to_thread", new=inline_to_thread), \
                    mock.patch.object(
                        main, "__file__", str(plugin_dir / "main.py")):
                result = await plugin.report_frontend_ready(
                    self.FRONTEND_BUNDLE_ID)

            self.assertFalse(result)
            self.assertFalse(plugin.install_frontend_ready_path.exists())

    async def test_stale_install_challenge_does_not_reject_backend_startup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = main.Plugin.__new__(main.Plugin)
            plugin.lifecycle_heartbeat_task = None
            plugin.game_watch_task = None
            plugin.runtime_marker = root / "runtime-session.active"
            plugin.legacy_fan_guard_marker = root / "legacy-fan.active"
            plugin.rgb = types.SimpleNamespace(
                reapply_startup=lambda: False)
            plugin._start_plugin_lifecycle_guard = mock.Mock(
                side_effect=RuntimeError("guard unavailable in fixture"))
            plugin._load = mock.Mock()
            plugin._restore_runtime_session = mock.Mock()
            plugin._restore_legacy_system_fan_curve = mock.Mock()
            plugin._publish_backend_install_health = mock.Mock(
                return_value=False)

            async def game_watch():
                await asyncio.Event().wait()

            async def inline_to_thread(function, *arguments):
                return function(*arguments)

            plugin._game_watch_loop = game_watch
            with mock.patch.object(
                    main.asyncio, "to_thread", new=inline_to_thread):
                await plugin._main()

            plugin._publish_backend_install_health.assert_called_once_with()
            self.assertIsNotNone(plugin.game_watch_task)
            self.assertFalse(plugin.game_watch_task.done())
            plugin.game_watch_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await plugin.game_watch_task

    async def test_install_health_exception_is_logged_without_rejecting_startup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = main.Plugin.__new__(main.Plugin)
            plugin.lifecycle_heartbeat_task = None
            plugin.game_watch_task = None
            plugin.runtime_marker = root / "runtime-session.active"
            plugin.legacy_fan_guard_marker = root / "legacy-fan.active"
            plugin.rgb = types.SimpleNamespace(
                reapply_startup=lambda: False)
            plugin._start_plugin_lifecycle_guard = mock.Mock(
                side_effect=RuntimeError("guard unavailable in fixture"))
            plugin._load = mock.Mock()
            plugin._restore_runtime_session = mock.Mock()
            plugin._restore_legacy_system_fan_curve = mock.Mock()
            plugin._publish_backend_install_health = mock.Mock(
                side_effect=ValueError("corrupt install challenge"))

            async def game_watch():
                await asyncio.Event().wait()

            async def inline_to_thread(function, *arguments):
                return function(*arguments)

            plugin._game_watch_loop = game_watch
            with mock.patch.object(
                    main.asyncio, "to_thread", new=inline_to_thread), \
                    mock.patch.object(
                        main.decky.logger, "exception", create=True,
                    ) as log_exception:
                await plugin._main()

            plugin._publish_backend_install_health.assert_called_once_with()
            log_exception.assert_called_once_with(
                "Install health response was withheld; plugin startup continues")
            self.assertIsNotNone(plugin.game_watch_task)
            self.assertFalse(plugin.game_watch_task.done())
            plugin.game_watch_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await plugin.game_watch_task


class ProcessIdentityTests(unittest.TestCase):
    def test_live_identity_contains_pid_reuse_protection(self):
        identity = main._process_identity(os.getpid())

        self.assertEqual(identity["pid"], os.getpid())
        self.assertGreater(identity["start_time_ticks"], 0)
        self.assertGreaterEqual(identity["parent_pid"], 1)

    def test_invalid_or_missing_pid_is_rejected(self):
        self.assertIsNone(main._process_identity(0))
        self.assertIsNone(main._process_identity(999999999))

    def test_guard_identity_lookup_is_bounded_and_diagnostic_only(self):
        unit = "rke-plugin-lifecycle-guard-123-abcdef12"
        for failure in (
                OSError("systemctl unavailable"),
                subprocess.TimeoutExpired(["systemctl"], 3)):
            with self.subTest(failure=type(failure).__name__), \
                 mock.patch.object(main, "_run", side_effect=failure) as run, \
                 mock.patch.object(main.time, "sleep"):
                self.assertIsNone(main._guard_unit_identity(unit))
                self.assertEqual(run.call_count, 3)

        with mock.patch.object(main, "_run") as run:
            self.assertIsNone(main._guard_unit_identity("other.service"))
        run.assert_not_called()


class AutomaticRecoveryFocusTests(unittest.IsolatedAsyncioTestCase):
    BOOT_ID = "11111111-2222-3333-4444-555555555555"

    @staticmethod
    async def inline_to_thread(function, *arguments):
        return function(*arguments)

    def write_request(self, path, *, appid="3214610", requested=None,
                      boot_id=BOOT_ID):
        if requested is None:
            requested = main.time.monotonic()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "version": 1,
            "boot_id": boot_id,
            "appid": appid,
            "requested_monotonic": requested,
            "reason": "stale-heartbeat",
        }) + "\n")

    async def test_fresh_request_for_same_live_game_is_consumed_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = root / "run" / "automatic-recovery-focus.json"
            lock = root / "run" / "focus.lock"
            self.write_request(request)
            plugin = main.Plugin.__new__(main.Plugin)
            plugin._detect_steam_app = mock.Mock(return_value="3214610")

            with mock.patch.object(
                    main, "AUTO_RECOVERY_FOCUS_REQUEST", request), \
                    mock.patch.object(main, "LIFECYCLE_LOCK", lock), \
                    mock.patch.object(main, "_read", return_value=self.BOOT_ID), \
                    mock.patch.object(
                        main.asyncio, "to_thread", new=self.inline_to_thread):
                first = await plugin.consume_automatic_recovery_focus_request()
                second = await plugin.consume_automatic_recovery_focus_request()

            self.assertEqual(first, "3214610")
            self.assertIsNone(second)
            self.assertFalse(request.exists())
            plugin._detect_steam_app.assert_called_once_with()

    async def test_stale_or_different_game_request_never_raises(self):
        now = main.time.monotonic()
        cases = (
            ("stale", now - 100.0, "3214610", self.BOOT_ID),
            ("different-game", now, "999999", self.BOOT_ID),
            ("different-boot", now, "3214610", "old-boot"),
        )
        for name, requested, detected, boot_id in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                request = root / "run" / "automatic-recovery-focus.json"
                lock = root / "run" / "focus.lock"
                self.write_request(
                    request, requested=requested, boot_id=boot_id)
                plugin = main.Plugin.__new__(main.Plugin)
                plugin._detect_steam_app = mock.Mock(return_value=detected)

                with mock.patch.object(
                        main, "AUTO_RECOVERY_FOCUS_REQUEST", request), \
                        mock.patch.object(main, "LIFECYCLE_LOCK", lock), \
                        mock.patch.object(
                            main, "_read", return_value=self.BOOT_ID), \
                        mock.patch.object(
                            main.asyncio, "to_thread", new=self.inline_to_thread):
                    result = await (
                        plugin.consume_automatic_recovery_focus_request())

                self.assertIsNone(result)
                self.assertFalse(request.exists())

    async def test_normal_frontend_mount_without_request_is_noop(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = root / "automatic-recovery-focus.json"
            lock = root / "focus.lock"
            plugin = main.Plugin.__new__(main.Plugin)
            plugin._detect_steam_app = mock.Mock(return_value="3214610")

            with mock.patch.object(
                    main, "AUTO_RECOVERY_FOCUS_REQUEST", request), \
                    mock.patch.object(main, "LIFECYCLE_LOCK", lock), \
                    mock.patch.object(
                        main.asyncio, "to_thread", new=self.inline_to_thread):
                result = await (
                    plugin.consume_automatic_recovery_focus_request())

            self.assertIsNone(result)
            plugin._detect_steam_app.assert_not_called()

    async def test_frontend_navigation_report_accepts_only_bounded_values(self):
        plugin = main.Plugin.__new__(main.Plugin)
        valid = await plugin.report_automatic_recovery_focus_result(
            "3214610", "confirmed")
        invalid = (
            await plugin.report_automatic_recovery_focus_result(
                "0", "confirmed"),
            await plugin.report_automatic_recovery_focus_result(
                "3214610", "invented"),
            await plugin.report_automatic_recovery_focus_result(
                "3214610", {"confirmed": True}),
        )

        self.assertTrue(valid)
        self.assertEqual(invalid, (False, False, False))


class LifecycleCurrentPointerTests(unittest.TestCase):
    def test_old_cleanup_cannot_unlink_a_newer_published_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / "plugin-lifecycle-current.json"
            lock = root / "plugin-lifecycle.lock"
            old = json.dumps({"token": "a" * 32}) + "\n"
            new = json.dumps({"token": "b" * 32}) + "\n"

            with mock.patch.object(main, "LIFECYCLE_CURRENT", current), \
                    mock.patch.object(main, "LIFECYCLE_LOCK", lock):
                main._publish_lifecycle_current(old)
                entered = threading.Event()
                result = []

                def remove_old():
                    entered.set()
                    result.append(main._remove_lifecycle_current("a" * 32))

                # Model the newer publisher winning the shared lock. The old
                # cleaner blocks until the new pointer is fully published,
                # then must observe its different token and leave it intact.
                with main._exclusive_file_lock(lock):
                    worker = threading.Thread(target=remove_old)
                    worker.start()
                    self.assertTrue(entered.wait(1))
                    main._atomic_text(current, new)
                    current.chmod(0o600)
                worker.join(1)

                self.assertFalse(worker.is_alive())
                self.assertEqual(result, [False])
                self.assertEqual(json.loads(current.read_text())["token"], "b" * 32)

    def test_matching_generation_is_removed_under_the_same_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / "plugin-lifecycle-current.json"
            lock = root / "plugin-lifecycle.lock"
            payload = json.dumps({"token": "a" * 32}) + "\n"

            with mock.patch.object(main, "LIFECYCLE_CURRENT", current), \
                    mock.patch.object(main, "LIFECYCLE_LOCK", lock):
                main._publish_lifecycle_current(payload)
                self.assertTrue(main._remove_lifecycle_current("a" * 32))

            self.assertFalse(current.exists())


if __name__ == "__main__":
    unittest.main()
