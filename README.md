# RK-Enhanced

**RK-Enhanced — ROCKNIX Kontrol Enhanced** is a Decky Loader plugin providing
performance controls, automatic per-game presets, fan-curve management, and
live hardware monitoring for ROCKNIX-based ARM handhelds.

RK-Enhanced is built on the foundation established by Seilent's original
[ROCKNIX Control](https://github.com/seilent/rocknix-control). Its ROCKNIX
integration and Decky control concepts made this project possible.

Detailed changes for each published build are recorded in
[CHANGELOG.md](CHANGELOG.md).

The project later evolved through
[Rocknix-Control-Enhanced](https://github.com/mrdidit/Rocknix-Control-Enhanced/tree/preset-delete-button),
with additional interface concepts and improvements inspired by
[NDC-Enhanced](https://github.com/mrdidit/NDC-Enhanced).

ROCKNIX and NovaDeck expose different services, settings, and hardware
interfaces. RK-Enhanced therefore uses a ROCKNIX-specific backend rather than
directly copying NDC-Enhanced.

> [!WARNING]
> RK-Enhanced is beta software. Hardware controls affect the running system,
> and support can vary between devices and ROCKNIX builds. Keep a recovery
> path available and expect unfinished areas.

## What RK-Enhanced does

### Live monitoring

The Monitor tab provides an at-a-glance view of:

- Battery level and estimated remaining or charging time
- Battery charge power, battery draw, and bypass state
- CPU and GPU load
- Representative CPU temperature
- CPU hotspot temperature
- GPU temperature
- Fan speed
- Used memory
- Live CPU cluster clocks
- GPU clock and governor
- CPU scheduler
- CPU queue status
- Active thermal limits

Temperature and load indicators use severity-based colours so unusual
conditions can be recognised quickly.

Battery estimates use smoothed measurements where kernel-provided values are
unreliable. Longer estimates are shortened to forms such as `10h+` to preserve
the Quick Access Menu layout.

### Performance controls

Performance settings can be stored independently in every preset:

- CPU governor
- Minimum and maximum frequency for each CPU cluster
- Device-discovered CPU boost ceilings when ROCKNIX turbo mode is enabled
- GPU governor
- GPU maximum frequency
- Kernel or supported `sched_ext` scheduler

Save & Apply controls are available at convenient points throughout the
Performance tab.

Boost clocks are discovered independently for every CPU policy. They are valid
maximum limits, never minimum choices, and are labelled **Boost** in the UI.
An adaptive governor such as `schedutil` treats the selected value as an upper
limit and requests boost only when needed. The `performance` governor may hold
an allowed boost clock continuously, so RK-Enhanced displays a red warning for
that combination.

### Fan curves

Every RK-E preset contains an independent, editable fan curve.

RK-Enhanced does not run a competing fan-control loop. ROCKNIX's native
`fancontrol.service` continues to control the hardware.

When ROCKNIX cooling is already set to **Custom**, RK-Enhanced:

1. Protects the system ROCKNIX Custom curve.
2. Writes the active RK-E preset curve to `fancontrol.conf`.
3. Reloads native fancontrol.
4. Restores the protected curve when Steam exits or Decky stops unexpectedly.

RK-Enhanced never changes the active ROCKNIX cooling-profile selection.

For RK-E fan curves to operate, configure either:

- **ROCKNIX Settings → Cooling Profile → Custom**, or
- **Per-System Advanced Configuration → Steam → Cooling Profile → Custom**

Steam's **Default** option also works when the System cooling profile is
Custom.

When another cooling profile is active, the Fan Curves tab displays a red
warning and disables its Save & Apply controls. Performance settings remain
available.

### ROCKNIX Custom curve

ROCKNIX provides one native Custom curve through:

```text
/storage/.config/fancontrol.conf
```

RK-Enhanced adds a graphical editor for this curve under:

```text
Utils → Edit ROCKNIX Custom fan curve
```

If `fancontrol.conf` does not exist, Utils can create it with a safe initial
curve. The file can then be inspected and modified graphically through
RK-Enhanced; a terminal or external file manager is not required.

Curve configuration and curve activation are deliberately separate. RK-Enhanced
provides the graphical configuration, while the active cooling profile is
selected through ROCKNIX Settings or Steam's Per-System Advanced Configuration.

During first setup, the ROCKNIX Custom curve is copied into **RK-E Default**.
After that initial copy, both curves are completely independent:

- **ROCKNIX Custom** is the protected system curve.
- **RK-E Default** is the standard Steam preset.
- Additional presets each contain their own fan curve.

Editing ROCKNIX Custom does not silently overwrite RK-E Default or other
presets.

### Presets and automatic game switching

The preset system contains:

- **RK-E Default**
- Additional named presets

A new preset copies the complete currently selected preset, including:

- CPU configuration
- GPU configuration
- Scheduler
- Fan curve

Any preset can be selected as the Steam default.

While a game is running, the Presets tab provides a dedicated **Game preset**
dropdown. Selecting an entry assigns and immediately applies that preset.

A lightweight backend watcher monitors Steam independently of the Decky panel:

```text
Steam idle
    ↓
RK-E Default or selected Steam default
    ↓
Game starts
    ↓
Assigned game preset
    ↓
Game exits
    ↓
Steam default restored
```

The watcher reads the small Steam systemd process scope, avoids global process
scanning, and debounces launch and exit transitions. Preset switching therefore
continues while the RK-Enhanced panel is closed.

### Utilities

The Utils tab contains:

- Runtime logs
- ROCKNIX Custom fan-curve editor
- Installed and latest release discovery
- Update to the latest published release
- Reinstall the current release
- Downgrade to the previous published release
- Hidden experimental controls

Release discovery includes GitHub pre-releases.

Updates are installed by a detached updater that:

1. Downloads and validates the selected release.
2. Creates a rollback copy.
3. Replaces the plugin.
4. Reloads Decky.
5. Leaves Steam and running games untouched.

Downgrading preserves the safer current updater so reinstalling a newer release
cannot restore obsolete Steam lifecycle behaviour.

Downgrades are intended for recovery only. Releases predating the public
charging-helper boundary may directly write, capture, or restore charging state.
Before downgrading, use the current Experimental tab to select Battery policy
Normal and Pump profile Qualcomm/Normal, confirm both fresh statuses, then hide
the experimental controls. Reboot immediately after the downgrade before using
charging controls. Preserving the current updater makes it possible to return
to a newer release, but does not make an older plugin's charging behaviour safe.

### Experimental charging controls

Charging controls are currently hidden behind the experimental unlock in Utils.
On supported KPFE builds, the Experimental tab provides two independent control
families:

- Battery policy: Normal, Bypass, or Limit 50–100%
- Pump profile: Qualcomm/Normal, Slow 25 W, or Fast 36 W

**Limit 100%** stops charging at 100% and resumes at 95%. The five-point gap is
intentional hysteresis, preventing repeated stop/start cycling at the endpoint.

RK-Enhanced never writes charging sysfs controls directly. Battery policy is
owned exclusively through `/usr/bin/charging_mode`, and the dual-pump profile is
owned exclusively through `/usr/bin/kpfe_fast_charge`. Slow and Fast require a
new risk confirmation for every enable request.

On newer compatible helpers, Experimental Status also reports **USB input
power** from the optional atomic status fields returned by
`/usr/bin/kpfe_fast_charge status`. Qualcomm and dual-pump measurements are
shown as wattage only; Offline and Transitioning are shown
without a fabricated zero. Missing, malformed, stale, unavailable, or
incoherent telemetry shows Unavailable or Stale and never retains an earlier
wattage. This is charging-path input power, distinct from Monitor's
battery-derived **Battery charge power**. RK-Enhanced does not probe USB or pump
sysfs as a fallback.

The same serialized status refresh reads the generic
`/sys/class/power_supply/battery/temp` attribute once and shows **Battery
temperature** in degrees Celsius. Missing or malformed samples show Unavailable.
Temperature colours are green below 35°C, yellow from 35°C, orange from 45°C,
and red from 50°C; sub-zero readings are also red. These conservative bands are
informational and do not replace the helper or coordinator safety limits.

Experimental Status uses semantic colours for quick recognition: healthy or
active states are green, inactive states are blue, transitions and unknown
states are yellow or orange, and errors are red. **Battery charging** reports
whether charging is Allowed or Paused; it does not claim that current is flowing.

The Limit 100 hysteresis explanation is shown only while Limit 100 is selected.
Both selectors remain locked until a current, coherent status refresh succeeds.
Either selector is disabled when either helper status is invalid, stale, or
transitional. Hiding the tab, unloading RK-Enhanced, or crashing Decky does not
issue a charging command or restore an earlier charging mode; the canonical
ROCKNIX helpers and coordinator own that lifecycle.

Bypass behaviour still depends on the device and power supply. Sufficient power
may hold the battery level, while a weaker source can allow partial discharge or
continued slow charging.

## Hardware support

Development and live testing currently focus on Qualcomm-based ROCKNIX
handhelds, including:

- Snapdragon SM8650
- Snapdragon SM8750

RK-Enhanced discovers CPU policies, GPU interfaces, thermal zones, fan
controls, and battery features at runtime. Controls unavailable on a device
should be omitted or reported as unavailable.

Broader hardware validation is still required.

## GPU monitoring

On Qualcomm/Adreno devices exposing KGSL, RK-Enhanced reads:

```text
/sys/class/kgsl/kgsl-3d0/gpu_busy_percentage
```

This provides system-wide GPU activity and closely follows the value used by
MangoApp.

When KGSL activity is unavailable, RK-Enhanced can fall back to DRM client
engine accounting associated with the active Steam application or gamescope.
Process and file-descriptor discovery is cached to avoid expensive repeated
scans.

## Temperature monitoring

Thermal-zone discovery is designed to work across different Qualcomm layouts.

Current representation:

- **CPU temperature:** average of available CPU package sensors
- **CPU hotspot:** hottest valid CPU or CPU-package sensor
- **GPU temperature:** primary GPU package sensor, with a fallback to the
  hottest GPU sensor

Colour thresholds:

- Green: below 50°C
- Yellow: 50–69°C
- Orange: 70–84°C
- Red: 85°C and above

The representative CPU value is intended to describe overall package
conditions, while CPU hotspot highlights the hottest observed area.

## Design challenges

### ROCKNIX has one Custom fan slot

ROCKNIX always reads the same `fancontrol.conf` file. Multiple RK-E curves
therefore require controlled temporary replacement rather than separate native
fan slots. The restoration guard exists to keep this process recoverable.

### ROCKNIX cooling overrides are global

An explicit Steam cooling override can be copied into the global ROCKNIX
setting. After Steam exits, ROCKNIX may leave that value active instead of
restoring the earlier System profile.

For example:

```text
System: Quiet
Steam: Aggressive
```

may leave the System profile at Aggressive afterward. RK-Enhanced deliberately
does not alter or restore ROCKNIX profile selections.

### Decky runs through FEX

On the tested ARM ROCKNIX environment, Decky and plugin workers run through
FEX. This introduces several complications:

- System tools can inherit incompatible private libraries.
- Graceful PluginLoader shutdown can occasionally hang.
- Old child processes may survive a failed restart.
- Frontend bundles must be classic scripts rather than ES modules.

RK-Enhanced sanitises child-process library variables and emits its frontend as
an IIFE bundle to remain compatible with this environment.

### Hardware interfaces vary

Thermal names, CPU clusters, GPU drivers, battery controls, fan paths, and
writable sysfs entries can differ between devices and ROCKNIX versions.
Runtime discovery reduces hard-coded assumptions, but cannot replace testing
across real hardware.

### Monitoring must remain lightweight

Telemetry polling can itself affect performance if implemented carelessly.
RK-Enhanced caches expensive discovery, avoids duplicate pollers, starts
Monitor polling only while Quick Access is open on the Monitor tab, and
serializes those requests. Experimental status polling likewise stops when its
tab or Quick Access is hidden. Each response is bound to the current Monitor
activation and charging revision, so late results are discarded after a panel
close, tab change, or status failure.

The automatic game watcher reads only Steam's small process cgroup rather than
repeatedly scanning the whole system.

### Runtime restoration

Before the first preset is applied in a Steam session, RK-Enhanced captures the
native CPU, GPU, and scheduler state. It records only controls RKE
actually changes and restores their native values when Steam exits, the plugin
unloads, or Decky/plugin workers terminate unexpectedly.

Restoration is ownership-aware: a value is restored only while it still equals
the last value written by RK-Enhanced. If ROCKNIX or another tool changes that
control afterward, RK-Enhanced leaves the newer value intact. Runtime values
are not carried across a reboot; the protected persistent ROCKNIX Custom fan
curve is still recovered when necessary.

## Current limitations

- Hardware coverage remains limited.
- Qualcomm KGSL and MSM DRM receive the strongest GPU-monitoring support.
- Thermal sensor naming may require additional device-specific handling.
- Utils can create `/storage/.config/fancontrol.conf` when it is missing and
  graphically edit the ROCKNIX Custom curve afterward. Activation still occurs
  through ROCKNIX or Steam cooling-profile settings.
- Native ROCKNIX profile overrides may remain active after Steam exits.
- Crash recovery and ownership-aware restoration need broader long-duration
  testing across devices.
- Touch, controller navigation, and compact-screen layouts still need
  refinement.
- Experimental charging controls are not ready for general exposure.
- Preset import, export, and sharing are not yet available.
- TDP control is not currently implemented.

## Roadmap

### Beta stabilisation

- Stress-test launch, exit, crash, sleep, resume, and reboot transitions
- Validate restoration after forced PluginLoader termination
- Expand SM8650 and SM8750 device testing
- Improve detection of unsupported and partially supported controls
- Add clearer diagnostics when sysfs writes fail
- Refine controller and touch navigation
- Reduce remaining layout inconsistencies
- Review every preset migration path

### Hardware expansion

- Add more Qualcomm GPU layouts
- Improve non-KGSL GPU monitoring
- Build a reusable device-capability registry
- Validate additional battery and charging interfaces
- Improve thermal-zone classification
- Investigate safe power and TDP controls where hardware support exists

### Preset improvements

- Import and export presets
- Duplicate presets explicitly
- Display the effective preset and reason for selection
- Add assignment summaries
- Add optional preset reset points
- Improve conflict handling when settings change outside RK-Enhanced

### Diagnostics and recovery

- Export a compact diagnostic report
- Include device, ROCKNIX build, sensor discovery, and recent logs
- Add restoration self-tests
- Detect stale configuration sessions at boot
- Improve recovery guidance when Decky or Steam becomes unavailable

### Longer-term direction

- Broader ARM handheld support
- More device-specific safeguards
- Optional performance recommendations based on detected capabilities
- Stable preset schema with backward-compatible migrations
- A polished 1.0 release after sufficient hardware coverage and recovery
  testing

## Installation

Run the installer as `root` on the ROCKNIX device:

```sh
curl -fL https://raw.githubusercontent.com/mrdidit/RK-Enhanced/main/install.sh | sh
```

The installer retrieves the latest published RK-Enhanced release, including
pre-releases, and installs Decky Loader when required. Review `install.sh`
before piping it into a shell.

Manual layout:

```text
/storage/homebrew/plugins/RK-Enhanced/
├── dist/index.js
├── charging.py
├── main.py
├── runtime-restore.py
├── runtime-restore-guard.sh
├── updater.sh
└── plugin.json
```

## Development

Requirements:

- Node.js
- pnpm
- Python 3

Validation and build:

```sh
pnpm install
python3 -m py_compile main.py charging.py runtime-restore.py
python3 -m unittest discover -s tests -v
pnpm typecheck
pnpm build
```

The frontend is emitted as an IIFE because the tested Decky/FEX environment
evaluates plugin bundles as classic scripts. An ESM bundle fails with:

```text
Unexpected token: export
```

## Reporting problems

Useful reports include:

- Device model and SoC
- ROCKNIX build
- RK-Enhanced version
- Exact reproduction steps
- Expected and observed behaviour
- Relevant output from Utils → Logs

## Origins and acknowledgements

RK-Enhanced would not exist without **Seilent's original
[ROCKNIX Control](https://github.com/seilent/rocknix-control)**.

That project established the core idea of bringing ROCKNIX hardware controls
into Decky's Quick Access Menu and provided the foundation from which
Rocknix-Control-Enhanced and RK-Enhanced developed.

Further development draws from:

- [ROCKNIX Control](https://github.com/seilent/rocknix-control) by Seilent — the
  original foundation
- [Rocknix-Control-Enhanced](https://github.com/mrdidit/Rocknix-Control-Enhanced/tree/preset-delete-button)
  — the earlier enhanced fork
- [NDC-Enhanced](https://github.com/mrdidit/NDC-Enhanced) — interface and
  workflow inspiration
- The ROCKNIX project and its device-specific services, quirks, and hardware
  support

RK-Enhanced continues that work with deep respect for the projects and
contributors that made it possible.

## Development collaboration

RK-Enhanced is developed through hands-on hardware testing, direct ROCKNIX
inspection, and iterative coding collaboration with **Codex**.

Development has used **GPT-5.6 Sol**, with reasoning effort scaled from
**Light through Ultra** according to task complexity. Hardware behaviour and
final decisions are validated against real devices rather than accepted from
generated output alone.

## License

MIT

## Screenshots

### Utils

![RK-Enhanced Utils](docs/screenshots/utils.jpg)

### Presets

![RK-Enhanced presets](docs/screenshots/presets.jpg)

### Fan curves and live fan output

![RK-Enhanced fan curve live output](docs/screenshots/fan-curves-live.jpg)

### Fan-curve editor

![RK-Enhanced fan-curve editor](docs/screenshots/fan-curves.jpg)

### GPU performance controls

![RK-Enhanced GPU performance controls](docs/screenshots/performance-gpu.jpg)

### CPU performance controls

![RK-Enhanced CPU performance controls](docs/screenshots/performance-cpu.jpg)

### Monitor clocks and runtime

![RK-Enhanced clocks and runtime monitoring](docs/screenshots/monitor-runtime.jpg)

### Live performance monitor

![RK-Enhanced live performance monitor](docs/screenshots/monitor-performance.jpg)
