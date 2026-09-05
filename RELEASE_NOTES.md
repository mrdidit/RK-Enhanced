Beta.14 makes RK-Enhanced lighter in normal use, strengthens Steam-session
cleanup, and makes DRM GPU monitoring follow the correct active game or
Gamescope session.

## Changelog

### Lightweight runtime

- Reduces Monitor, game-watcher, frontend, and healthy PluginLoader recovery
  overhead while retaining exact-identity crash recovery.
- Caches boot-stable telemetry discovery, serializes Monitor sampling, avoids
  hidden or irrelevant frontend polling, and narrows fan status reads.
- Reports **RK-E CPU Load** on the same whole-device scale as total CPU load.
- Preserves in-progress preset edits during safe state refreshes and rejects
  stale frontend responses.

### Reliable GPU monitoring

- Selects DRM clients belonging to the exact active Steam AppID first, then
  the measurable Gamescope compositor inside Steam's authoritative scope.
- Rejects `gamescopereaper`, unrelated compositor sessions, and duplicate
  views of the same physical DRM client.
- Binds evidence to exact process generations, DRM devices, client IDs, and
  descriptor lifetimes. Stale or reused sources are rediscovered safely.
- Resets the sampling baseline when ownership changes so a game exit,
  compositor restart, reused PID, or reused descriptor cannot produce a false
  utilization spike.
- Keeps KGSL as the preferred system-wide GPU source where it is available.

### Native state restoration

- Restores native CPU, GPU, scheduler, and protected fan state after a
  confirmed complete Steam exit.
- Debounces transient Steam scope changes, while clean unload, plugin-worker
  death, and verified PluginLoader replacement request immediate restoration.
- Keeps restoration ownership-aware so an independent ROCKNIX or external
  change is not overwritten.

### Compatibility

- No Experimental charging behaviour, helper boundary, or hardware-write
  interface changed in this release.
- KGSL devices retain their existing primary GPU path. The session-selection
  fix applies to devices using DRM engine-accounting fallback.
- Existing fan, preset, charging, and RGB device capability gates are retained.

## Updating

- **From beta.10 or newer:** update normally from Utils.
- **From beta.9 or older:** use the full installer so RK-Enhanced and a
  compatible Decky Loader are validated together:

  ```sh
  curl -fL https://raw.githubusercontent.com/mrdidit/RK-Enhanced/main/install.sh | sh
  ```

## Validation

- The complete Python suite passed 347 tests; one Node-dependent host check was
  skipped in the local environment.
- Python compilation, POSIX shell validation, diff checks, focused selector
  tests, and deterministic stale-discovery races passed.
- The exact backend candidate was hash-verified after deployment. Live checks
  confirmed normalized RK-E load reporting and GPU-selector compatibility on
  the tested ROCKNIX device.
- The tagged Node 20 release pipeline repeats TypeScript checks, frontend model
  tests, the production build and integrity verification, Python and shell
  validation, and release packaging.

This remains a pre-release. Bugs and device-specific gaps are still expected;
reports with logs from `/storage/homebrew/logs/RK-Enhanced/` are welcome.
