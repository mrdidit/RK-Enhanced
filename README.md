# RK-Enhanced

**RK-Enhanced** stands for **ROCKNIX Kontrol Enhanced**. It is an experimental
Decky Loader plugin for controlling and monitoring ROCKNIX-based ARM handhelds
from Steam's Quick Access Menu.

This project is a reworked successor to
[Rocknix-Control-Enhanced](https://github.com/thefiqs/Rocknix-Control-Enhanced/tree/preset-delete-button),
with ideas and UI work ported from
[NDC-Enhanced](https://github.com/thefiqs/NDC-Enhanced). ROCKNIX and NovaDeck use
different backends, so RK-Enhanced is not a direct copy of NDC-Enhanced.

> [!WARNING]
> RK-Enhanced is early pre-release software. It has known bugs, has only had
> limited hardware testing, and still has plenty of room for improvement. CPU,
> GPU, scheduler and fan settings affect the running system. Keep a recovery
> path available and do not assume every ROCKNIX device exposes identical
> interfaces.

## Current features

- Live battery, signed battery flow, thermal, CPU, GPU, fan, memory and clock
  monitoring with severity-based colours.
- Bypass charging control on supported ROCKNIX kernels, including holding,
  charging and discharging states plus a smoothed remaining-time estimate.
- CPU governor and per-cluster frequency limits.
- GPU governor and frequency limits where ROCKNIX exposes them.
- Kernel or supported `sched_ext` scheduler selection.
- Editable Steam default and reusable performance presets.
- Per-game preset assignment with a configurable Steam fallback preset.
- Combined Save & Apply actions on Performance, Fan Curves and Presets.
- Native ROCKNIX Custom system curve plus independent custom curves in Steam
  and per-game presets.
- Runtime log viewer in the Utils tab.

## Fan-control design

RK-Enhanced does **not** run a competing fan-control loop. ROCKNIX's native
`fancontrol.service` remains responsible for continuously driving the fan.

The system ROCKNIX Custom curve is managed only in Utils. On first setup,
RK-Enhanced imports an existing `/storage/.config/fancontrol.conf`, or creates a
safe curve when none exists. The initial Steam Default preset copies that
system curve.

Steam Default and per-game presets may select Quiet, Moderate, Aggressive, or
Custom. Each preset choosing Custom stores its own independent curve. While
that preset is active, RK-Enhanced writes its curve to `fancontrol.conf` and
reloads native fancontrol. When RK-Enhanced unloads, it restores the separately
saved system ROCKNIX Custom curve. This changes configuration only; ROCKNIX's
native service remains responsible for continuously controlling the fan.

## GPU monitoring

On Qualcomm/Adreno devices exposing KGSL, RK-Enhanced follows MangoHud and reads
the system-wide `gpu_busy_percentage` metric from
`/sys/class/kgsl/kgsl-3d0`. On systems without that metric it falls back to the
active Steam game's unique DRM clients and their `drm-engine-gpu` time. When no
game is running it monitors gamescope instead. Process discovery is cached and
does not repeatedly scan every process and file descriptor on each sample.

## Battery and bypass charging

On supported devices, the Monitor tab controls bypass charging through
`/sys/class/power_supply/battery/charge_behaviour`. RK-Enhanced writes
`inhibit-charge` to enable bypass and `auto` to restore normal charging, then
reads the active bracketed mode back to verify the change.

While bypass is active, the battery bar is blue and signed battery flow reports
whether the battery is holding charge, charging, or supplementing a weak power
supply. When the battery is discharging, RK-Enhanced calculates remaining time
from the charge counter and a smoothed current sample instead of trusting the
kernel estimate after near-zero-current periods. It displays `Calculating…`
until the initial sample window is ready, then updates the estimate every five
seconds. Times use readable forms such as `6h 11m`.

## Known limitations

- This remains early pre-release software; regressions and incomplete workflows
  should be expected.
- Hardware discovery and writable sysfs controls can differ between ROCKNIX
  devices and builds.
- Active-game GPU usage currently targets Qualcomm KGSL and MSM DRM layouts
  tested during development; other drivers may need additional support.
- Decky or SteamWebHelper restarts can occasionally leave old PluginLoader
  workers behind on this ROCKNIX/FEX setup.
- Preset, session restoration and fan-curve workflows need broader real-world
  testing.
- The UI and controller/touch interaction still need refinement.

Bug reports should include the ROCKNIX build, device model, steps to reproduce
and relevant output from the plugin's Utils → Logs view.

## Installation

The bundled installer downloads the latest stable Decky Loader and the newest
RK-Enhanced release, including pre-releases:

```sh
curl -fL https://raw.githubusercontent.com/thefiqs/RK-Enhanced/main/install.sh | sh
```

Run it as `root` on the ROCKNIX device. Review `install.sh` before piping it to a
shell. The installer keeps rollback copies of the previous Decky binary and
RK-Enhanced plugin directory.

For a manual installation, extract the release asset so the final layout is:

```text
/storage/homebrew/plugins/RK-Enhanced/
├── dist/index.js
├── main.py
└── plugin.json
```

## Development

Requirements include Node.js and pnpm:

```sh
pnpm install
python3 -m py_compile main.py
pnpm typecheck
pnpm build
```

The frontend bundle is intentionally emitted as an IIFE. The Decky frontend on
the tested ROCKNIX build evaluates plugin bundles as classic scripts, so an ESM
bundle fails with `Unexpected token: export`.

## License

MIT
