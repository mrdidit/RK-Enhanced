import json
import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InstallerProgressContractTests(unittest.TestCase):
    def setUp(self):
        self.installer = (ROOT / "install.sh").read_text()
        self.updater = (ROOT / "updater.sh").read_text()

    def test_shells_parse(self):
        for name in ("install.sh", "updater.sh"):
            subprocess.run(
                ["sh", "-n", str(ROOT / name)],
                check=True,
                capture_output=True,
                text=True,
            )

    def test_progress_is_atomic_and_generation_bound(self):
        required = (
            'INSTALL_PROGRESS_FILE="${SETTINGS_DIR}/install-progress.json"',
            '"${SETTINGS_DIR}/install-progress.XXXXXX"',
            'chmod 600 "${rke_progress_temporary}"',
            'mv "${rke_progress_temporary}" "${INSTALL_PROGRESS_FILE}"',
            "generation: $generation",
            "transaction_id: $transaction_id",
            "source_version: $source_version",
            "target_version: $target_version",
            "decky_version: $decky_version",
            "phase: $phase",
            "message: $message",
            "outcome: $outcome",
            "writer: {pid: $writer_pid",
        )
        for source in (self.installer, self.updater):
            for value in required:
                self.assertIn(value, source)

    def test_journal_is_bounded_and_shared(self):
        for source in (self.installer, self.updater):
            self.assertIn(
                'INSTALL_JOURNAL_FILE="${LOG_DIR}/installer.log"', source
            )
            self.assertIn("INSTALL_JOURNAL_LIMIT=524288", source)
            self.assertIn('"${INSTALL_JOURNAL_FILE}.1"', source)
            self.assertIn('>> "${INSTALL_JOURNAL_FILE}"', source)

    def test_legacy_control_detection_is_manifest_based_and_fails_closed(self):
        for source in (self.installer, self.updater):
            self.assertIn('"rocknix control"', source)
            self.assertIn('[ ! -L "${rke_conflict_path}" ]', source)
            self.assertIn('[ ! -L "${rke_conflict_manifest}" ]', source)
            self.assertIn('""|.|..|*/*) return 1', source)
            self.assertIn("fancontrol.service", source)
            self.assertNotIn("pkill", source)
            self.assertNotIn("killall", source)

    def test_conflict_removal_is_always_explicit(self):
        self.assertIn("--remove-conflicting-rocknix-control", self.installer)
        self.assertIn(
            "RKE_REMOVE_CONFLICTING_ROCKNIX_CONTROL", self.installer
        )
        self.assertIn(
            "remove-conflicting-rocknix-control", self.updater
        )
        self.assertIn("--remove-rocknix-control", self.updater)
        self.assertIn('operation_kind="remove-conflict"', self.updater)
        self.assertIn(
            "removal is permanent and creates no backup", self.updater
        )

    def test_full_install_removal_is_bound_to_approved_paths_and_identity(self):
        for source in (self.installer, self.updater):
            self.assertIn("fingerprint_legacy_rocknix_control()", source)
            self.assertIn("stat -c '%d:%i:%Z'", source)
            self.assertIn("stat -c '%d:%i:%Z:%s'", source)
            self.assertIn("rke_conflict_manifest_digest", source)
            self.assertIn(
                'cmp -s "${legacy_conflict_fingerprint}"', source
            )
            self.assertIn(
                '"${legacy_conflicts}" "${legacy_conflict_fingerprint}"',
                source,
            )
        self.assertNotIn(
            'scan_legacy_rocknix_control "${rke_conflict_current}"',
            self.installer,
        )
        self.assertNotIn(
            'scan_legacy_rocknix_control "${rke_conflict_current}"',
            self.updater,
        )

    def test_successful_backups_receive_trusted_creation_markers(self):
        for source in (self.installer, self.updater):
            self.assertIn(".rke-backup-created.json", source)
            self.assertIn("'{protocol: 1, created_at: $created_at", source)
            self.assertIn('chmod 600 "${', source)

    def test_actual_previous_release_is_recorded_only_across_versions(self):
        for source, target in (
            (self.installer, "${rke_version}"),
            (self.updater, "${version}"),
        ):
            self.assertIn(
                'LAST_INSTALLED_VERSION_FILE="${SETTINGS_DIR}/last-installed-version.txt"',
                source,
            )
            self.assertIn(
                f'[ "${{progress_source_version}}" != "{target}" ]', source
            )
            self.assertIn(
                'printf \'%s\\n\' "${progress_source_version}"', source
            )

    def test_standalone_removal_revalidates_then_restores_services(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            storage = root / "storage"
            run_root = root / "run"
            bin_dir = root / "bin"
            plugins = storage / "homebrew/plugins"
            settings = storage / "homebrew/settings/RK-Enhanced"
            conflict = plugins / "legacy control"
            for directory in (bin_dir, settings, conflict):
                directory.mkdir(parents=True, exist_ok=True)
            (settings / "installed-version.txt").write_text("v0.2.0-beta.10\n")
            (conflict / "plugin.json").write_text(
                json.dumps({"name": "  ROCKNIX Control  ", "version": "0.1.2"})
            )
            plugin_state = root / "plugin.state"
            fan_state = root / "fan.state"
            plugin_state.write_text("active\n")
            fan_state.write_text("inactive\n")
            systemctl = bin_dir / "systemctl"
            systemctl.write_text(
                "#!/bin/sh\n"
                "plugin_state=$MOCK_PLUGIN_STATE\n"
                "fan_state=$MOCK_FAN_STATE\n"
                "command=$1\n"
                "shift\n"
                "case $command in\n"
                "  show) cat \"$plugin_state\" ;;\n"
                "  stop|kill)\n"
                "    printf 'inactive\\n' > \"$plugin_state\"\n"
                "    if [ -n \"${MOCK_SWAP_TARGET:-}\" ] && "
                "       [ -d \"${MOCK_SWAP_SOURCE:-}\" ]; then\n"
                "      rm -rf \"$MOCK_SWAP_TARGET\"\n"
                "      mv \"$MOCK_SWAP_SOURCE\" \"$MOCK_SWAP_TARGET\"\n"
                "    fi\n"
                "    ;;\n"
                "  start)\n"
                "    unit=$1\n"
                "    case $unit in\n"
                "      fancontrol.service) printf 'active\\n' > \"$fan_state\" ;;\n"
                "      *) printf 'active\\n' > \"$plugin_state\" ;;\n"
                "    esac\n"
                "    ;;\n"
                "  is-active)\n"
                "    [ \"${1:-}\" = --quiet ] && shift\n"
                "    case ${1:-} in\n"
                "      fancontrol.service) state=$(cat \"$fan_state\") ;;\n"
                "      *) state=$(cat \"$plugin_state\") ;;\n"
                "    esac\n"
                "    [ \"$state\" = active ]\n"
                "    ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n"
            )
            systemctl.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{bin_dir}:{environment['PATH']}",
                    "RKE_STORAGE_ROOT": str(storage),
                    "RKE_RUN_ROOT": str(run_root),
                    "RKE_PROC_ROOT": "/proc",
                    "MOCK_PLUGIN_STATE": str(plugin_state),
                    "MOCK_FAN_STATE": str(fan_state),
                }
            )
            def approval_for(path):
                directory_status = path.lstat()
                manifest = path / "plugin.json"
                manifest_status = manifest.lstat()
                manifest_digest = hashlib.sha256(
                    manifest.read_bytes()).hexdigest()
                fingerprint = (
                    f"{path}\t"
                    f"{directory_status.st_dev}:{directory_status.st_ino}:"
                    f"{int(directory_status.st_ctime)}\t"
                    f"{manifest_status.st_dev}:{manifest_status.st_ino}:"
                    f"{int(manifest_status.st_ctime)}:{manifest_status.st_size}\t"
                    f"{manifest_digest}\n"
                )
                return hashlib.sha256(fingerprint.encode()).hexdigest()

            approval = approval_for(conflict)
            completed = subprocess.run(
                [
                    "sh",
                    str(ROOT / "updater.sh"),
                    "--remove-rocknix-control",
                    str(conflict),
                    approval,
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(conflict.exists())
            self.assertEqual(plugin_state.read_text().strip(), "active")
            self.assertEqual(fan_state.read_text().strip(), "active")
            progress = json.loads((settings / "install-progress.json").read_text())
            self.assertEqual(progress["kind"], "remove-conflict")
            self.assertEqual(progress["outcome"], "succeeded")
            self.assertFalse(progress["active"])
            self.assertTrue(progress["terminal"])
            journal = (
                storage / "homebrew/logs/RK-Enhanced/installer.log"
            ).read_text()
            self.assertIn("no backup", journal)
            self.assertIn("native fancontrol is active", journal)

            real_conflict = plugins / "real-conflict"
            real_conflict.mkdir()
            (real_conflict / "plugin.json").write_text(
                json.dumps({"name": "ROCKNIX Control", "version": "0.1.2"})
            )
            linked_conflict = plugins / "linked-conflict"
            linked_conflict.symlink_to(real_conflict, target_is_directory=True)
            refused = subprocess.run(
                [
                    "sh",
                    str(ROOT / "updater.sh"),
                    "--remove-rocknix-control",
                    str(linked_conflict),
                    "0" * 64,
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertTrue(linked_conflict.is_symlink())
            self.assertTrue(real_conflict.is_dir())
            refused_progress = json.loads(
                (settings / "install-progress.json").read_text()
            )
            self.assertEqual(refused_progress["outcome"], "blocked")
            self.assertTrue(refused_progress["terminal"])
            self.assertGreater(refused_progress["generation"], progress["generation"])

            race_target = plugins / "race-control"
            race_target.mkdir()
            (race_target / "plugin.json").write_text(json.dumps({
                "name": "ROCKNIX Control", "version": "0.1.2",
            }))
            race_approval = approval_for(race_target)
            replacement = root / "replacement-control"
            replacement.mkdir()
            (replacement / "plugin.json").write_text(json.dumps({
                "name": "ROCKNIX Control", "version": "0.1.2",
            }))
            race_environment = environment.copy()
            race_environment.update({
                "MOCK_SWAP_TARGET": str(race_target),
                "MOCK_SWAP_SOURCE": str(replacement),
            })
            raced = subprocess.run(
                [
                    "sh",
                    str(ROOT / "updater.sh"),
                    "--remove-rocknix-control",
                    str(race_target),
                    race_approval,
                ],
                check=False,
                capture_output=True,
                text=True,
                env=race_environment,
            )
            self.assertNotEqual(raced.returncode, 0)
            self.assertTrue(race_target.is_dir())
            self.assertFalse(replacement.exists())
            raced_progress = json.loads(
                (settings / "install-progress.json").read_text()
            )
            self.assertEqual(raced_progress["outcome"], "failed")
            self.assertIn("changed", raced_progress["message"].lower())


if __name__ == "__main__":
    unittest.main()
