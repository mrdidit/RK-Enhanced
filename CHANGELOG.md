# Changelog

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
