Beta.11 makes installation observable and recoverable, prevents legacy
ROCKNIX Control plugins from competing for hardware ownership, adds
capability-gated Pocket EVO RGB ABI 3 controls, and reports normalized RK-E
CPU Load.

## Changelog

### Installer safety

- Shows persistent, real installation phases rather than a fabricated progress
  percentage. The same transaction returns after Decky reloads or Quick Access
  is reopened, and every phase is recorded in Utils → Logs.
- Serializes installation and RK-Enhanced mutations, verifies the candidate
  backend and frontend, and rolls RK-Enhanced and Decky back together on
  failure.
- Separates the previous published release from trustworthy last-installed
  history and adds safe cleanup of old RK-Enhanced rollback snapshots.
- Detects both the original ROCKNIX Control and Rocknix Control Enhanced by
  their shared manifest identity. Controls remain blocked until the exact
  approved plugin path and manifest are removed; native `fancontrol.service`
  is restored afterward.
- Places **Remove conflicting plugin** directly on Monitor. Removal is
  permanent and intentionally creates no backup of the conflicting plugin.

### Pocket EVO RGB ABI 3

- Detects the complete runtime ABI instead of assuming support from a product
  name or SoC.
- Keeps Static as the default and adds both-ring, per-stick, and eight-quadrant
  layouts with Static, Breath, RGB Breath, Rainbow, and Reactive effects.
- Adds native colour calibration controls and complete verified readback.
- Unpatched Pocket EVO-S devices safely retain the existing generic Static
  interface; unsupported advanced controls remain hidden.

### Monitoring and interface

- Adds combined, whole-device-normalized **RK-E CPU Load** to Monitor's Runtime
  section without adding another poller.
- Reorganizes Utils, adds **Clean old RK-E backups**, and moves the protected
  system fan editor to the bottom of Fan Curves under the shorter
  **Rocknix Fan Curve** heading.

## Updating

- **From beta.10:** update normally from Utils.
- **From beta.9 or older:** run the full installer once so the compatible Decky
  selection and current transactional updater are installed together.

  ```sh
  curl -fL https://raw.githubusercontent.com/mrdidit/RK-Enhanced/main/install.sh | sh
  ```

## Validation

- Python regressions, TypeScript checks, frontend model tests, POSIX shell
  validation, production build, source map, and frontend integrity all pass.
- The default SSH installer and runtime conflict gate were exercised against a
  real Rocknix Control Enhanced installation on the Pocket FIT Elite. The
  installer blocked without mutation, and RK-Enhanced skipped preset, RGB,
  CPU, GPU, and fan ownership while the conflict remained.

This remains a pre-release. Experimental and device-specific features appear
only when their runtime interfaces validate, but bugs and hardware gaps are
still expected. Reports with logs from
`/storage/homebrew/logs/RK-Enhanced/` are welcome.
