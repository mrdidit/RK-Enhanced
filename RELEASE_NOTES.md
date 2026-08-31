Beta.13 makes the AYN Odin 3's saved Static RGB state survive a full restart
without turning Decky reloads into repeated hardware writes.

## Changelog

### Odin 3 restart persistence

- Persists the complete last successfully applied Odin 3 **RGB** or **Off**
  state: explicit mode, full eight-zone editor state, exact verified cached
  24-channel state, and boot identity.
- Validates the exact Odin 3 HTR3212 provider again on startup and restores the
  saved state once on the first eligible RK-Enhanced startup of a new boot.
- Marks the boot handled before the first channel write. A failed restore
  therefore cannot repeat after a Decky reload; **Save & Apply** remains the
  explicit retry path.
- Writes no channels when the cached channel state already matches. Later
  Decky reloads and same-boot external changes remain untouched.
- Migrates beta.12's saved Odin state automatically to the new explicit-mode
  preference record.
- Treats an exact Odin 3 with a missing or incomplete HTR3212 interface as
  unsupported instead of falling through to an unrelated RGB writer.
- Adds no daemon, polling loop, animation worker, suspend watcher, or
  unload-time RGB mutation.

### Compatibility

- Pocket EVO ABI 3, unpatched Pocket EVO-S, Pocket FIT, and generic
  `analog_sticks_ledcontrol` paths retain their existing behavior.
- Odin 3 support requires the exact `AYN Odin 3` ROCKNIX identity, all 24
  canonical channels, two distinct HTR3212 controllers, and writable
  brightness attributes. Partial or ambiguous layouts fail closed.
- A plain Decky reload in the same boot never reapplies the saved Odin state.
  Only the first eligible RK-Enhanced startup after a new boot may restore it.

## Thanks

Special thanks to the [Armada project](https://github.com/armada-os/armada) and
its contributors. The hardware-tested physical mapping from
[PR #255](https://github.com/armada-os/armada/pull/255) made the safe Odin 3
provider possible. [PR #270](https://github.com/armada-os/armada/pull/270)
provided valuable research into software-generated RGB effects and potential
future improvements.

## Updating

- **From beta.10, beta.11, or beta.12:** update normally from Utils.
- **From beta.9 or older:** use the full installer so RK-Enhanced and a
  compatible Decky Loader are validated together:

  ```sh
  curl -fL https://raw.githubusercontent.com/mrdidit/RK-Enhanced/main/install.sh | sh
  ```

## Validation

- The Python suite ran 290 tests: 289 passed and one Node-dependent host test
  was skipped. These include 30 focused Odin HTR3212 cases for RGB, Off,
  beta.12 migration, failed writes, corrupt state, exact-device validation,
  and same-boot reload behavior.
- TypeScript, frontend model tests, production build, source map, frontend
  integrity (verified separately in the completed Node 20 pipeline), Python
  compilation, POSIX shell validation, and diff checks pass.
- The candidate was hash-verified and Python-compiled after deployment to an
  Odin 3, and live full-reboot validation confirmed restoration of the saved
  RGB state. Same-boot no-repeat behavior is covered by the regression suite.

This remains a pre-release. Bugs and device-specific gaps are still expected;
reports with logs from `/storage/homebrew/logs/RK-Enhanced/` are welcome.
