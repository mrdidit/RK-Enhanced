# Changelog

## v0.2.0-beta.13 — 2026-08-31

### AYN Odin 3 restart persistence

- Persists the complete last successfully applied Odin 3 RGB or Off state and
  restores its exact verified cached 24-channel state once on the first
  eligible RK-Enhanced startup of a new boot.
- Revalidates the exact Odin 3 HTR3212 provider before restoration and rejects
  missing, malformed, inconsistent, or incomplete saved/native state without
  falling through to another RGB provider.
- Tombstones the current boot before the first channel write. A failed restore
  therefore cannot repeat on a Decky reload, while explicit Save & Apply stays
  available as the safe retry path.
- Performs no channel writes when the saved and current cached channel states
  already match and adds no polling loop, daemon, suspend watcher, or
  unload-time RGB mutation.

## v0.2.0-beta.12 — 2026-08-31

### AYN Odin 3 RGB

- Adds a dedicated, exact-device provider for the Odin 3's pair of HTR3212
  stick-ring controllers and complete 24-channel sysfs layout.
- Adds **Off** and **Static RGB** control with both-ring, per-stick, and eight
  physical-quadrant editing using the verified Odin 3 ring order.
- Applies gamma to brightness only, preserving the selected raw RGB ratios.
  The optional FIT/generic mixed-colour correction is not applied to Odin 3.
- Binds every save to a stable complete cached sysfs snapshot, writes only
  changed channels, and checks the full cached state before each write.
  Failures attempt guarded rollback only while the observed state still
  matches, stopping when external divergence is detected.
- Documents the kernel boundary honestly: the LED class exposes cached driver
  brightness, not physical HTR3212 register readback.
- Adds no daemon, polling loop, startup reapply, unload restoration, or unsafe
  fallback for other HTR3212 devices whose physical layouts remain unverified.
- This first Odin 3 release is intentionally Static-only. Armada's
  [PR #270](https://github.com/armada-os/armada/pull/270) demonstrates that
  Breathing, Rainbow, Battery-level, and CPU-temperature effects can be
  generated in software; those background effects are not included yet.

Special thanks to the [Armada project](https://github.com/armada-os/armada) and
its contributors. The hardware-tested physical mapping from
[PR #255](https://github.com/armada-os/armada/pull/255) made the safe Odin 3
provider possible, while PR #270 provided valuable research for potential
future effects.

## v0.2.0-beta.11 — 2026-08-30

### Installation safety and visibility

- Adds a persistent, blocking installation view with honest download,
  validation, backup, install, Decky-reload, verification, completion, and
  rollback phases. RK-Enhanced mutations stay disabled during a transaction,
  and progress resumes after Decky reloads or Quick Access is reopened.
- Appends the same phases, source and target releases, Decky version, failures,
  and rollback result to a size-limited installer journal included in Utils
  logs. Both the SSH installer and in-plugin updater use the same record.
- Distinguishes the previous published GitHub release from local installation
  history, and offers a distinct last-installed release when completed local
  cross-version history differs from the previous-published option.
- Detects the exact normalized **ROCKNIX Control** manifest identity shared by
  the original plugin and Rocknix Control Enhanced. Installation blocks by
  default; explicit removal revalidates and permanently deletes only the exact
  non-symlink plugin directory, then restores native `fancontrol.service`.
- Adds the same conflict as a runtime mutation gate, keeping Monitor, Logs,
  Utils, and conflict removal available without allowing competing preset or
  hardware writes.
- Adds **Clean old RK-E backups**, which retains the newest ordinary rollback
  snapshot and excludes symlinks, recovery artifacts, unknown directories,
  other plugins, and Decky's Loader rollback file.

### Interface cleanup

- Places **Remove conflicting plugin** directly on Monitor whenever the runtime
  conflict gate is active, with the permanent-removal warning beside it.
- Removes the PC-side SSH trust-reset hint from Utils.
- Moves the ROCKNIX Custom fan-curve editor to the bottom of Fan Curves.
- Shortens that editor's visible heading to **Rocknix Fan Curve** while keeping
  its protected ROCKNIX Custom purpose clear inside the section.
- Places backup cleanup directly below the downgrade warning and moves the
  Experimental controls block and its explanation to the bottom of Utils.

### Runtime diagnostics

- Adds severity-coloured combined `RK-E CPU Load` to Monitor's Runtime section,
  using the existing serialized telemetry request with no new poller.
- Normalizes the displayed value to the same whole-device scale as Monitor's
  main CPU load, so `7.5%` RK-E load can be compared directly with `15%` total
  load on an eight-core device.
- Combines the RK-Enhanced backend, its exact child/helper process tree, the
  exact PluginLoader lifecycle guard, and the exact runtime-restoration guard;
  cumulative child counters retain short helper work, while per-root process
  generations prevent guard changes from causing false spikes. Decky, other
  plugins, and native ROCKNIX services remain excluded. An unresolved expected
  guard makes the metric explicitly unavailable instead of silently partial.

### Pocket EVO RGB ABI 3

- Adds a dedicated, capability-detected Pocket EVO provider ahead of the
  existing FIT and generic Static providers. The advanced interface is exposed
  only after the complete ABI version 3 contract validates.
- Keeps unpatched Pocket EVO-S devices on their existing generic Static path;
  no product-name or SoC allowlist is introduced.
- Keeps Static as the default native effect and adds both-ring, per-stick, and
  eight-quadrant editing over one complete ordered layout.
- Adds native Breath, RGB Breath, Rainbow, and Reactive controls together with
  driver-owned green/blue calibration, Reset, and Raw actions.
- Serializes potentially slow sysfs transactions in the backend, performs one
  complete write per command, verifies cached native readback, and rejects
  stale or unstable state before mutation.
- Reconciles the complete native state after failed writes, keeps an
  active-but-unsaved result visible and saveable, and uses guarded rollback
  without overwriting a newer external change.
- Preserves dormant Pocket EVO layout/calibration preferences if an unpatched
  kernel temporarily exposes only the generic Static provider.
- Respects ROCKNIX's temporary output gate, never opens the RGB UART, never
  applies the legacy 80% correction to ABI 3, and never restores an EVO
  lighting layout or effect automatically at startup.

## v0.2.0-beta.10 — 2026-08-30

### Compatible Decky installation

- Selects Decky's newest published Loader build, including prereleases, instead
  of silently replacing a compatible installation with an older stable build.
- Fixes missing Decky UI on newer Steam clients that require Decky's renamed
  Steam-initialization API compatibility update.
- Makes a full SSH install require both backend and frontend readiness whenever
  Steam Big Picture is active. When Steam Big Picture is inactive, success
  explicitly says the frontend was not tested.
- Keeps RK-Enhanced and Decky replacement transactional: a failed readiness
  check restores both previous components and their recorded versions.
- Requires beta.9 and earlier installations to use the full SSH installer once;
  subsequent in-plugin updates use the corrected Decky selection automatically.

## v0.2.0-beta.9 — 2026-08-27

### Reliable unattended update verification

- Moves the install-readiness probe from the Quick Access React panel to the
  registered frontend bundle's startup lifecycle, so a Decky reload no longer
  depends on reopening RK-Enhanced within the verification timeout.
- Still requires the exact integrity-stamped frontend bundle to execute and
  complete a successful `getState()` round trip before it can acknowledge the
  nonce-bound candidate backend.
- Cancels the bounded startup probe when Decky dismounts that plugin
  generation, preventing a retired frontend from continuing readiness RPCs.
- Keeps beta.8's install-health protocol and transactional rollback format so
  the beta.8 updater can safely install beta.9.

### Release presentation

- Uses descriptive GitHub release titles and places a visible changelog in the
  release body instead of relying on a buried compare link.

## v0.2.0-beta.8 — 2026-08-27

### Decky compatibility

- Falls back to Decky's legacy UI visibility hook when Loader API v2 is not
  available, so RK-Enhanced can render long enough to recover an older Decky
  installation instead of failing on `useQuickAccessVisible`.
- Makes every new full install and every update initiated by this release fetch
  and validate the latest stable Decky Loader alongside the selected
  RK-Enhanced release.

### Verified installation and rollback

- Treats Decky and RK-Enhanced replacement as one serialized transaction with
  separate transaction and lifecycle-maintenance locks.
- Replaces service-active-only success with a nonce-bound backend/frontend
  handshake tied to release version, boot, exact backend and bundle hashes,
  frontend self-integrity, a ready lifecycle guard, and live
  PluginLoader/backend process generations.
- Requires an update started from the plugin UI to reach the new frontend's
  first hydrated React commit after `getState()` succeeds; a fresh SSH install
  verifies the backend because Steam may not be running.
- Retries initial state hydration for a bounded window so a brief RPC startup
  race does not cause an otherwise healthy update to roll back.
- Revalidates live artifacts and identical process identities across stable
  samples before recording the installed version.
- Restores both the previous plugin and Decky executable on failure, validates
  a protocol-aware restored backend, labels legacy rollback as unverified, and
  preserves unique recovery files if safe rollback cannot complete.
- Handles interruption signals as failures, acquires the lifecycle lock before
  every rollback mutation, and never performs unlocked cleanup of live files.

### Packaging

- Stamps the IIFE bundle with a normalized SHA-256 identity and packages an
  independently verified frontend-integrity manifest.
- Publishes explicit install-health protocol metadata and checks both files in
  CI and on the handheld before replacement begins.

## v0.2.0-beta.7 — 2026-08-26

### Interface

- Moves Thermal limit to the top of Live Performance, directly above CPU load.
- Orders tabs as Monitor, Performance, Fan Curves, Presets, RGB, Utils, and
  Experimental. Capability-gated tabs remain hidden when unavailable.

### RGB compatibility

- Adds a generic static stick-lighting provider discovered from ROCKNIX's
  runtime analogue-stick capability, public helper, and valid persisted state;
  no product or SoC allowlist is used.
- Keeps generic stick lighting independent of `led.color` and the system battery
  indicator, and exposes no animation unless a verified effect interface exists.
- Rejects stale or wrong-provider writes, preserves newer external settings
  during rollback, and clearly warns before a shared-colour save replaces
  unequal right/left ring values.

### PluginLoader lifecycle recovery

- Adds an out-of-cgroup runtime watchdog for each exact RK-Enhanced backend and
  PluginLoader generation; it is deliberately separate from updater failures.
- Binds generation identity to both PID and `/proc` start-time ticks, validates
  same-boot leases and the fixed PluginLoader unit/binary, and revalidates every
  captured identity before a bounded signal.
- Treats active-marker removal as a clean unload and yields to a verified newer
  ready generation instead of restarting an intentional Decky replacement.
- Uses same-boot monotonic heartbeats and a readiness/current handoff so wall
  clock changes and partially started replacement guards cannot trigger a
  false recovery.
- Defers automatic recovery until Steam Big Picture is active and enforces a
  120-second same-boot cooldown to prevent restart loops.
- Captures the running AppID only for automatic recovery and, after the new
  frontend and Steam UI are ready, selects that same still-running game and
  invokes Steam's non-launching gamescope Resume navigation. Manual Decky
  restarts and maintenance never request focus.
- Uses only the captured PluginLoader/RK-Enhanced process trees; it never issues
  a global process-name kill for PluginLoader, FEX, or Python.
- Makes clean-unload runtime restoration an explicit request to the independent
  session guard, with the immediate detached restore retained as a fast path.

### Packaging and maintenance safety

- Compiles, packages, validates, and installs the executable
  `plugin_loader_recovery.py` helper whenever the staged backend references it.
- Replaces unbounded or global installer cleanup with a non-blocking,
  unit-scoped Decky stop and bounded `SIGTERM`/`SIGKILL` escalation.
- Holds the lifecycle recovery lock and maintenance marker across intentional
  Decky stop, file replacement, and bounded startup verification so the runtime
  watchdog cannot restart PluginLoader midway through maintenance.

## v0.2.0-beta.6 — 2026-08-25

### Monitor telemetry

- Distinguishes a valid `0.0 W` battery-power sample from missing telemetry.
- Separates the selected charging policy from instantaneous battery flow, so
  Bypass and Limit remain visible while the battery independently shows watts
  flowing in, flowing out, at zero, or unavailable.
- Shows a valid near-zero sample as **0.0 W**, avoiding the previous
  ambiguous Holding charge and Unavailable presentation.
- Keeps Battery level, Time estimate, and Battery flow as stable rows, with
  compact `W in` / `W out` direction text that fits the Quick Access panel.
- Moves Power & Battery below Clocks and directly above Runtime.

### Utilities

- Shows the current preferred device IPv4 address and interface when Utils is
  visible, without adding a network poller.
- Shows the exact `ssh-keygen -R <Device IP>` command for clearing a stale host
  record on the connecting PC. RK-Enhanced does not delete or regenerate SSH
  keys on the handheld.

### RGB control

- Adds an RGB tab with an internal RGB Control section on devices where the
  required native ROCKNIX lighting interface is discovered at runtime.
- Exposes the native Off, Battery, and RGB LED Color modes while leaving
  ROCKNIX `ledcontrol` in authority.
- Adds Static, Breath, and Rainbow controls only when the known stick-ring
  effect interface is available. Both rings are treated as one shared zone.
- Persists Static through the native `analogsticks.led` setting. RK-Enhanced
  stores its source colour, brightness, optional correction, and animated effect,
  and may reapply an animation at startup only while the native mode remains RGB.
- Adds optional, default-off colour correction for Static and Breath: red is
  unchanged, while green and blue are scaled to 80% only when red is present.
  Rainbow is never corrected.
- Adds no RGB poller, background ownership, unload restoration, or device-name
  gate. Unsupported devices do not show the RGB tab, and partial support exposes
  only controls backed by the detected runtime interface.
- Packages and validates the new `rgb.py` backend in installer, updater, and
  release archives.

## v0.2.0-beta.5 — 2026-08-24

### Incompatible-hardware hotfix

- Keeps Monitor telemetry mounted after the first unsupported or incoherent
  charging-status result instead of rebuilding the page after every status
  refresh.
- Hides native helper paths and raw command diagnostics from unsupported
  Battery policy and Pump profile messages while preserving actionable errors
  on supported devices.

## v0.2.0-beta.4 — 2026-08-24

### Experimental charging safety review

- Makes Experimental charging controls available only as an explicitly
  unlocked, compatible-device feature. They require the canonical native
  ROCKNIX charging helpers and supported runtime capabilities; unlocking the UI
  does not add support to other devices.
- Labels the existing battery-derived charging wattage as **Battery charge power**, without changing its calculation.
- Shortens the Normal pump-profile label to **Qcom Normal** in the Experimental UI.
- Parses the optional atomic USB input-power group from the existing public
  pump-helper status and reports Offline, Transitioning, or measured wattage in
  Experimental Status without a path suffix, another poll, or direct hardware
  read.
- Adds one signed-tenths battery-temperature read to the existing serialized
  charging-status refresh, with Unavailable handling and conservative severity
  colours.
- Polishes Experimental Status with clearer Battery charging wording, semantic
  colours for battery, pump, and USB states, compact pump health, and a Limit
  100 hysteresis explanation shown only when that policy is selected.
- Uses only the canonical `charging_mode` and `kpfe_fast_charge` public helpers;
  RK-Enhanced no longer owns or restores charging sysfs state.
- Requires every displayed Active pump state to have USB online, selected
  PD-PPS, auto charging behaviour, no coordinator error, and both pumps
  online/Good—even when battery status is invalid.
- Gives every Monitor activation one atomic generation/revision lifecycle shared
  by policy status, telemetry, the Bypass cache, and EMA samples. Delayed older
  activations are rejected, status invalidation discards telemetry started under
  the earlier revision, and frontend polling is serialized and response-tagged.
- Stops Monitor and Experimental polling whenever Quick Access is closed, even
  though Decky keeps RK-Enhanced mounted, and restarts with a higher Monitor
  generation when the panel reopens.
- Keeps Battery policy visible when its helper is unsupported or a status RPC
  fails, including while raw telemetry is being reacquired.
- Locks both Experimental selectors until the current session has a successful,
  fresh, valid, non-transitional, coherent battery-and-pump status pair.
- Places request refusals beside the affected selector and documents that
  Limit 100% stops at 100% and resumes at 95%.

## v0.2.0-beta.3 — 2026-08-21

### UI improvements and cleanup

- Enlarges the tab header and shoulder-button hints, and makes tab focus visible.
- Adds consistent focusable section headings and Back to top navigation across
  Monitor, Performance, and Fan Curve.
- Compacts performance and fan controls, removes redundant row separators, and
  places live slider values beside their labels.
- Reorganises Presets into clearer Preset Management and Game Assignment
  sections.
- Opens logs in a large centred modal with controller-friendly scrolling,
  newest-first entries, compact timestamps, and reliable close controls.

## v0.2.0-beta.2 — 2026-08-18

This patch focuses on correct CPU boost handling and recoverable ownership of
runtime hardware controls.

### CPU boost support

- Discovers boost support independently for every CPU policy instead of using
  SoC-specific frequency constants.
- Reads policy or global boost state, `scaling_boost_frequencies`, and the
  policy's `cpuinfo_max_freq` where it exposes an additional boost ceiling.
- Keeps boost clocks out of minimum-frequency choices.
- Adds available boost bins to each policy's maximum-frequency control and
  labels them as **Boost**.
- Shows an informational notice when ROCKNIX CPU boost is enabled.
- Shows a red warning when `performance` and a boost maximum are selected,
  because that governor may request the highest permitted clock continuously.
- Reports the device's effective boost-capable maximum to monitoring rather
  than treating the normal frequency table as the hardware ceiling.
- Fixes the previous SM8750 behaviour where applying a preset wrote the normal
  `4089.6 MHz` maximum back over ROCKNIX's native `4320 MHz` boost ceiling.

Boost remains device- and ROCKNIX-controlled. RK-Enhanced neither enables nor
disables turbo mode; it exposes the frequencies provided by the running kernel.

### Runtime ownership and restoration

- Captures a same-boot native baseline immediately before the first RKE preset
  is applied.
- Tracks CPU governors and minimum/maximum frequencies per policy.
- Tracks GPU governor and minimum/maximum frequencies.
- Tracks the `scx_lavd.service` scheduler state.
- Tracks bypass-charging behaviour when RKE changes it.
- Integrates protected ROCKNIX Custom fan-curve recovery into the same runtime
  session.
- Restores owned controls when Steam exits, the plugin unloads cleanly, Decky
  terminates, or the RKE plugin worker crashes.
- Uses atomic state records and a shared file lock so a detached guard cannot
  race an in-progress hardware write.
- Records RKE's last intended value and restores a control only when its current
  value still matches that value. Later ROCKNIX or manual changes are preserved
  wherever this comparison can identify them.
- Skips stale CPU, GPU, scheduler, and charging values after a reboot while
  still recovering the persistent protected fan curve when required.
- Retries an incomplete crash restoration three times and leaves its session
  record available for startup recovery if restoration cannot finish.
- Detects both PluginLoader termination and a terminated RKE worker process.

The captured baseline is the state immediately before RKE's first apply. This
does not change ROCKNIX's separate behaviour when a Steam override itself is not
restored to the earlier System profile.

### Installation and recovery

- Replaces the fan-only guard with a general runtime restoration guard and a
  standalone restoration helper copied outside the plugin directory for crash
  safety during updates.
- Moves installer backups to `/storage/homebrew/plugin-backups` so Decky cannot
  discover a rollback copy as a second plugin.
- Validates the restoration tools when installing a release.
- Packages the new helper and guard in GitHub release archives.

### Validation

- Adds automated coverage for restoring a native boost ceiling.
- Verifies that controls changed after RKE's write are preserved.
- Covers GPU, charging, and protected fan-curve restoration.
- Covers stale sessions from an earlier boot.
- Covers different boost ceilings across CPU policies and devices.
- Runs Python compilation, unit tests, shell syntax checks, TypeScript checks,
  and the production frontend build in the release workflow.
