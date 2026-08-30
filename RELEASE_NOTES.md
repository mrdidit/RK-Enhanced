Beta.10 fixes installation failures caused by replacing a compatible Decky
prerelease with an older stable Loader. It is a focused installer hotfix; the
ongoing Pocket EVO RGB and runtime-diagnostics work is not included.

## Changelog

- Selects Decky's newest non-draft published release containing `PluginLoader`,
  including compatibility prereleases.
- Validates the Decky download, RK-Enhanced package, and integrity-stamped
  frontend before replacing the installed components.
- Requires matching backend and frontend readiness when Steam Big Picture is
  active. The RK-Enhanced panel does not need to be open.
- Performs backend-only verification when Steam Big Picture is inactive and
  explicitly says that the frontend was not tested.
- Retains transactional backup and rollback of RK-Enhanced, Decky, and their
  recorded versions when required readiness is not reached.

## Updating

- **From beta.9 or older:** run the full installer once. The updater that starts
  an in-plugin update comes from the currently installed release and does not
  yet use beta.10's prerelease-aware Decky selection before replacement.

  ```sh
  curl -fL https://raw.githubusercontent.com/mrdidit/RK-Enhanced/main/install.sh | sh
  ```

After beta.10 is installed, future Update, Reinstall, and Downgrade actions use
the corrected Decky selection automatically.

## Validation

- The Python regression suite, TypeScript checks, POSIX shell validation,
  frontend integrity, source-map verification, packaging, and rollback
  contracts pass.
- The full installer was exercised on a Pocket EVO with Steam active and Decky
  `v3.2.8-pre1`; exact backend/frontend readiness passed without opening the
  RK-Enhanced panel.

This remains a pre-release. Bugs and device-specific gaps are still expected;
reports with logs from `/storage/homebrew/logs/RK-Enhanced/` are welcome.
