"""Strict RK-Enhanced boundary for native ROCKNIX RGB controls."""

from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import threading
from pathlib import Path
from tempfile import NamedTemporaryFile


LED_CONTROL = Path("/usr/bin/ledcontrol")
ANALOG_STICKS_LED_CONTROL = Path("/usr/bin/analog_sticks_ledcontrol")
LED_PATH = Path("/sys/class/leds/konkr:rgb:joysticks")
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
EVO_LEDS_ROOT = Path("/sys/class/leds")
RGB_MODES = ("off", "battery", "rgb")
RGB_EFFECTS = ("static", "breath", "rainbow")
ANIMATED_EFFECTS = ("breath", "rainbow")
PREFERENCES_FILE = "rgb-settings.json"
HTR_PREFERENCES_FILE = "rgb-htr3212-settings.json"
PREFERENCES_VERSION = 3
LEGACY_PREFERENCES_VERSION = 2
DEFAULT_COLOR = (255, 255, 255)
DEFAULT_BRIGHTNESS = 255
PROVIDER_SYSFS_EFFECTS = "sysfs-effects"
PROVIDER_ANALOG_STATIC = "analog-static"
PROVIDER_POCKET_EVO_V3 = "pocket-evo-v3"
PROVIDER_HTR3212_STATIC = "htr3212-static"
PROVIDER_NONE = "none"
ANALOG_STICKS_CAPABILITY = "DEVICE_ANALOG_STICKS_LED_CONTROL"
QUIRK_DEVICE_CAPABILITY = "QUIRK_DEVICE"
HTR3212_ODIN3_QUIRK = "AYN Odin 3"
EVO_ZONE_INDEX = (
    "left-270", "left-0", "left-90", "left-180",
    "right-270", "right-0", "right-90", "right-180",
)
EVO_EFFECTS = ("static", "breath", "rgb-breath", "rainbow", "reactive")
EVO_ATTRIBUTES = (
    "abi_version", "zone_index", "zone_layout", "available_effects",
    "effect", "calibration", "enabled",
)
EVO_WRITABLE_ATTRIBUTES = ("zone_layout", "effect", "calibration", "enabled")
EVO_LAYOUT_MODES = ("both", "per-stick", "quadrants")
EVO_DEFAULT_CALIBRATION = (15, 20)
EVO_RAW_CALIBRATION = (100, 100)
HTR3212_DRIVER = "htr3212"
HTR3212_ZONE_INDEX = (
    "left-upper-left", "left-upper-right",
    "left-lower-right", "left-lower-left",
    "right-upper-left", "right-upper-right",
    "right-lower-right", "right-lower-left",
)
# Hardware-verified Odin 3 ring order. The exported channel colours are
# authoritative; the device-tree symbol names behind them are misleading.
HTR3212_ZONE_CHANNELS = (
    ("l", 4), ("l", 1), ("l", 2), ("l", 3),
    ("r", 1), ("r", 2), ("r", 3), ("r", 4),
)
HTR3212_PREFERENCE_VERSION = 3


@contextmanager
def _exclusive_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _read(path, default=""):
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeError):
        return default


def _bounded_integer(value, name, maximum=255):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not 0 <= value <= maximum:
        raise ValueError(f"{name} must be between 0 and {maximum}")
    return value


def _source_color(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("RGB colour must contain exactly three channels")
    return tuple(
        _bounded_integer(channel, f"RGB channel {index + 1}")
        for index, channel in enumerate(value)
    )


def corrected_color(color, enabled):
    """Apply the optional red-channel compensation exactly once."""
    source = _source_color(color)
    if not isinstance(enabled, bool):
        raise ValueError("colour correction must be true or false")
    red, green, blue = source
    if not enabled or red == 0:
        return source
    # Integer round-to-nearest for x * 0.80. The source channels remain saved
    # separately, so an already corrected output is never corrected again.
    return red, (green * 4 + 2) // 5, (blue * 4 + 2) // 5


def _htr3212_pwm(channel, brightness):
    """Apply perceptual correction to the level, preserving RGB ratios."""
    channel = _bounded_integer(channel, "RGB channel")
    brightness = _bounded_integer(brightness, "zone brightness")
    level = math.pow(brightness / 255, 2.2)
    return int(round(channel * level))


def _htr3212_zone_output(zone):
    color = _source_color(zone["color"])
    brightness = _bounded_integer(zone["brightness"], "zone brightness")
    return tuple(_htr3212_pwm(channel, brightness) for channel in color)


def _parse_native_config(raw):
    values = raw.split()
    if len(values) != 7 or any(not re.fullmatch(r"\d+", value) for value in values):
        return None
    parsed = [int(value) for value in values]
    if any(value > 255 for value in parsed):
        return None
    brightness = parsed[0]
    right, left = parsed[1:4], parsed[4:7]
    color = tuple(max(right[index], left[index]) for index in range(3))
    signature = " ".join(str(value) for value in parsed)
    return brightness, color, signature


def _native_config(brightness, color):
    red, green, blue = color
    return f"{brightness} {red} {green} {blue} {red} {green} {blue}"


def _state_revision(provider, *values):
    """Return an opaque revision for the exact native state shown to the UI."""
    material = "\0".join((provider, *(str(value) for value in values)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _parse_effect(raw):
    fields = raw.split()
    if fields == ["static"]:
        return "static", None
    if fields == ["rainbow"]:
        return "rainbow", None
    if (len(fields) == 4 and fields[0] == "breath" and
            all(re.fullmatch(r"\d+", value) for value in fields[1:])):
        color = tuple(int(value) for value in fields[1:])
        if all(value <= 255 for value in color):
            return "breath", color
    return None


def _decimal_values(raw, count, *, maximum=255):
    values = raw.split()
    if (len(values) != count or
            any(re.fullmatch(r"[0-9]+", value) is None for value in values)):
        return None
    parsed = tuple(int(value) for value in values)
    if any(value > maximum for value in parsed):
        return None
    return parsed


def _parse_evo_effect(raw):
    """Parse exactly one Pocket EVO ABI 3 effect command."""
    fields = raw.split()
    if fields == ["static"]:
        return {"effect": "static"}
    if fields == ["rainbow"]:
        return {"effect": "rainbow"}
    grammars = {
        "breath": 4,
        "rgb-breath": 1,
        "reactive": 7,
    }
    if not fields or fields[0] not in grammars:
        return None
    values = _decimal_values(" ".join(fields[1:]), grammars[fields[0]])
    if values is None:
        return None
    if fields[0] == "breath":
        brightness, red, green, blue = values
        return {
            "effect": "breath",
            "brightness": brightness,
            "color": (red, green, blue),
        }
    if fields[0] == "rgb-breath":
        return {"effect": "rgb-breath", "brightness": values[0]}
    brightness, *colors = values
    return {
        "effect": "reactive",
        "brightness": brightness,
        "idle_color": tuple(colors[:3]),
        "active_color": tuple(colors[3:]),
    }


def _evo_effect_command(lighting):
    effect = lighting["effect"]
    if effect == "static":
        return "static"
    if effect == "rainbow":
        return "rainbow"
    if effect == "breath":
        return " ".join((
            "breath", str(lighting["brightness"]),
            *(str(value) for value in lighting["color"]),
        ))
    if effect == "rgb-breath":
        return f"rgb-breath {lighting['brightness']}"
    if effect == "reactive":
        return " ".join((
            "reactive", str(lighting["brightness"]),
            *(str(value) for value in lighting["idle_color"]),
            *(str(value) for value in lighting["active_color"]),
        ))
    raise ValueError("unsupported Pocket EVO RGB effect")


def _evo_layout_values(zones):
    values = []
    for zone in zones:
        values.extend((*zone["color"], zone["brightness"]))
    return tuple(values)


def _layout_mode(zones):
    values = tuple((tuple(zone["color"]), zone["brightness"]) for zone in zones)
    if len(set(values)) == 1:
        return "both"
    if len(set(values[:4])) == 1 and len(set(values[4:])) == 1:
        return "per-stick"
    return "quadrants"


class RGBController:
    """Validate and serialize access to ROCKNIX's shared-zone RGB ABI.

    ``run`` must accept ``run(argv, check=True)`` and return stdout text.
    ``get_setting`` and ``set_setting`` use ROCKNIX setting names and values.
    The caller normally injects main.py's existing helpers.
    """

    def __init__(self, settings_dir, *, run, get_setting, set_setting,
                 get_runtime_capability=None,
                 led_control=LED_CONTROL, led_path=LED_PATH,
                 analog_sticks_led_control=ANALOG_STICKS_LED_CONTROL,
                 boot_id_path=BOOT_ID, evo_leds_root=EVO_LEDS_ROOT,
                 htr_leds_root=EVO_LEDS_ROOT):
        self.settings_dir = Path(settings_dir)
        self.preferences_path = self.settings_dir / PREFERENCES_FILE
        self.htr_preferences_path = (
            self.settings_dir / HTR_PREFERENCES_FILE)
        self.lock_path = self.settings_dir / "rgb-control.lock"
        self.run = run
        self.get_setting = get_setting
        self.set_setting = set_setting
        self.get_runtime_capability = (
            get_runtime_capability or (lambda _name: ""))
        self.led_control = Path(led_control)
        self.analog_sticks_led_control = Path(analog_sticks_led_control)
        self.led_path = Path(led_path)
        self.boot_id_path = Path(boot_id_path)
        self.evo_leds_root = Path(evo_leds_root)
        self.htr_leds_root = Path(htr_leds_root)
        self.thread_lock = threading.RLock()

    @staticmethod
    def _readable_attribute(path):
        try:
            mode = Path(path).stat().st_mode
        except OSError:
            return False
        return bool(
            stat.S_ISREG(mode) and
            mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH) and
            os.access(path, os.R_OK)
        )

    @staticmethod
    def _writable_attribute(path):
        try:
            mode = Path(path).stat().st_mode
        except OSError:
            return False
        return bool(
            stat.S_ISREG(mode) and
            mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) and
            os.access(path, os.W_OK)
        )

    def _discover_evo(self):
        """Return ``(status, path, effects, error)`` for ABI 3 discovery.

        An entirely absent ABI is a normal fallback case (notably an
        unpatched Pocket EVO-S). Once an ABI-looking path exists, however, a
        malformed or partial interface fails closed rather than falling into
        the generic helper provider and discarding its per-zone state.
        """
        pattern = "rgb:joystick-backlight-*/device/rgb"
        try:
            matches = list(self.evo_leds_root.glob(pattern))
        except OSError:
            matches = []
        if not matches:
            return "absent", None, (), ""

        canonical = set()
        try:
            for match in matches:
                canonical.add(match.resolve(strict=True))
        except OSError:
            return "invalid", None, (), "Pocket EVO RGB interface is incomplete"
        if len(canonical) != 1:
            return "invalid", None, (), "Pocket EVO RGB interface is ambiguous"
        path = next(iter(canonical))
        if not path.is_dir():
            return "invalid", None, (), "Pocket EVO RGB interface is unavailable"
        if not all(self._readable_attribute(path / name)
                   for name in EVO_ATTRIBUTES):
            return "invalid", path, (), "Pocket EVO RGB ABI 3 attributes are incomplete"
        if not all(self._writable_attribute(path / name)
                   for name in EVO_WRITABLE_ATTRIBUTES):
            return "invalid", path, (), "Pocket EVO RGB controls are not writable"
        try:
            abi_version = (path / "abi_version").read_text().strip()
            zone_index = tuple((path / "zone_index").read_text().split())
            available = tuple((path / "available_effects").read_text().split())
        except (OSError, UnicodeError):
            return "invalid", path, (), "Pocket EVO RGB ABI 3 is unreadable"
        if abi_version != "3":
            return "invalid", path, (), "Unsupported Pocket EVO RGB ABI"
        if zone_index != EVO_ZONE_INDEX:
            return "invalid", path, (), "Pocket EVO RGB zone order is unsupported"
        if not set(EVO_EFFECTS).issubset(available):
            return "invalid", path, (), "Pocket EVO RGB effects are incomplete"
        # Unknown future tokens are deliberately ignored. Their presence does
        # not extend the request parser or writer.
        effects = tuple(effect for effect in EVO_EFFECTS if effect in available)
        return "valid", path, effects, ""

    def _discover_htr3212(self):
        """Discover the hardware-mapped Odin 3 discrete 24-channel ABI.

        Other handhelds expose the same controller with unverified physical
        layouts. They deliberately remain unsupported until their mapping is
        measured on hardware rather than guessed from device-tree labels.
        """
        try:
            quirk = self.get_runtime_capability(QUIRK_DEVICE_CAPABILITY)
        except Exception:
            return "absent", (), ""
        if quirk != HTR3212_ODIN3_QUIRK:
            return "absent", (), ""

        channels = []
        for side, zone in HTR3212_ZONE_CHANNELS:
            channels.append(tuple(
                self.htr_leds_root / f"{side}:{color}{zone}"
                for color in ("r", "g", "b")
            ))
        flat = tuple(path for zone in channels for path in zone)
        present = tuple(path.exists() for path in flat)
        if not any(present):
            # The exact Odin identity has already matched. A missing ABI is a
            # failed exact-provider validation, not permission to fall through
            # to an unrelated generic RGB writer.
            return "invalid", (), "Odin 3 RGB channel set is unavailable"
        if not all(present):
            return "invalid", (), "Odin 3 RGB channel set is incomplete"

        controller_by_side = {}
        resolved_leds = set()
        try:
            for (side, _zone), zone_paths in zip(
                    HTR3212_ZONE_CHANNELS, channels):
                for path in zone_paths:
                    resolved_led = path.resolve(strict=True)
                    if resolved_led in resolved_leds:
                        return (
                            "invalid", (),
                            "Odin 3 RGB channel mapping is ambiguous",
                        )
                    resolved_leds.add(resolved_led)
                    brightness = path / "brightness"
                    maximum = path / "max_brightness"
                    if (not self._readable_attribute(brightness) or
                            not self._writable_attribute(brightness) or
                            not self._readable_attribute(maximum) or
                            _read(maximum) != "255"):
                        return (
                            "invalid", (),
                            "Odin 3 RGB channels are not safely writable",
                        )
                    device = (path / "device").resolve(strict=True)
                    driver = (device / "driver").resolve(strict=True)
                    if driver.name != HTR3212_DRIVER:
                        return (
                            "invalid", (),
                            "Odin 3 RGB channels use an unexpected driver",
                        )
                    previous = controller_by_side.setdefault(side, device)
                    if previous != device:
                        return (
                            "invalid", (),
                            "Odin 3 RGB channel ownership is ambiguous",
                        )
        except (OSError, RuntimeError):
            return "invalid", (), "Odin 3 RGB interface is incomplete"
        if (set(controller_by_side) != {"l", "r"} or
                controller_by_side["l"] == controller_by_side["r"]):
            return "invalid", (), "Odin 3 RGB controllers are ambiguous"
        return "valid", tuple(channels), ""

    def _native_modes(self):
        if not self.led_control.is_file() or not os.access(self.led_control, os.X_OK):
            return ()
        try:
            output = self.run([str(self.led_control), "list"], check=False)
        except Exception:
            return ()
        if not isinstance(output, str):
            return ()
        reported = {token.lower() for token in output.split()}
        return tuple(mode for mode in RGB_MODES if mode in reported)

    def _sysfs_effects_available(self):
        required = ("brightness", "max_brightness", "multi_intensity")
        # This first provider deliberately recognizes an ABI, not a product
        # name: only the known shared-zone LED class is safe for these writes.
        known_provider = self.led_path.name == "konkr:rgb:joysticks"
        attributes_available = bool(
            known_provider and
            self.analog_sticks_led_control.is_file() and
            os.access(self.analog_sticks_led_control, os.X_OK) and
            all((self.led_path / name).is_file() for name in required) and
            _read(self.led_path / "max_brightness") == "255"
        )
        # Avoid even the existing read-only ``ledcontrol list`` probe when the
        # strict sysfs ABI is absent. The generic provider is detected solely
        # from its ROCKNIX capability flag, public helper and saved setting.
        return attributes_available and self._native_modes() == RGB_MODES

    def _analog_static_available(self):
        try:
            enabled = (
                self.get_runtime_capability(ANALOG_STICKS_CAPABILITY) == "true")
        except Exception:
            return False
        return bool(
            enabled and
            self.analog_sticks_led_control.is_file() and
            os.access(self.analog_sticks_led_control, os.X_OK) and
            _parse_native_config(
                self.get_setting("analogsticks.led", "")) is not None
        )

    def capabilities(self):
        evo_status, _evo_path, evo_effects, evo_error = self._discover_evo()
        if evo_status == "valid":
            return self._provider_capabilities(
                PROVIDER_POCKET_EVO_V3,
                evo_effects=evo_effects,
            )
        if evo_status == "invalid":
            return {
                "available": False,
                "provider": PROVIDER_POCKET_EVO_V3,
                "modes": [],
                "effects": [],
                "shared_zone": False,
                "max_brightness": 0,
                "error": evo_error,
            }
        htr_status, _htr_channels, htr_error = self._discover_htr3212()
        if htr_status == "valid":
            return self._provider_capabilities(PROVIDER_HTR3212_STATIC)
        if htr_status == "invalid":
            return {
                "available": False,
                "provider": PROVIDER_HTR3212_STATIC,
                "modes": [],
                "effects": [],
                "shared_zone": False,
                "max_brightness": 0,
                "error": htr_error,
            }
        if self._sysfs_effects_available():
            provider = PROVIDER_SYSFS_EFFECTS
        elif self._analog_static_available():
            provider = PROVIDER_ANALOG_STATIC
        else:
            return {
                "available": False,
                "provider": PROVIDER_NONE,
                "modes": [],
                "effects": [],
                "shared_zone": False,
                "max_brightness": 0,
            }

        return self._provider_capabilities(provider)

    def _provider_capabilities(self, provider, *, evo_effects=()):
        if provider == PROVIDER_POCKET_EVO_V3:
            return {
                "available": True,
                "provider": provider,
                "modes": ["off", "rgb"],
                "effects": list(evo_effects),
                "shared_zone": False,
                "max_brightness": 255,
                "zone_ids": list(EVO_ZONE_INDEX),
                "layout_modes": list(EVO_LAYOUT_MODES),
                "error": "",
            }
        if provider == PROVIDER_HTR3212_STATIC:
            return {
                "available": True,
                "provider": provider,
                "modes": ["off", "rgb"],
                "effects": ["static"],
                "shared_zone": False,
                "max_brightness": 255,
                "zone_ids": list(HTR3212_ZONE_INDEX),
                "layout_modes": list(EVO_LAYOUT_MODES),
                "error": "",
            }
        modes = (
            list(RGB_MODES)
            if provider == PROVIDER_SYSFS_EFFECTS else ["off", "rgb"])
        effects = ["static"]
        if (provider == PROVIDER_SYSFS_EFFECTS and
                (self.led_path / "effect").is_file()):
            effects.extend(("breath", "rainbow"))
        return {
            "available": True,
            "provider": provider,
            "modes": modes,
            "effects": effects,
            "shared_zone": True,
            "max_brightness": 255,
        }

    def _default_preferences(self):
        return {
            "version": LEGACY_PREFERENCES_VERSION,
            "provider": PROVIDER_NONE,
            "source_color": list(DEFAULT_COLOR),
            "brightness": DEFAULT_BRIGHTNESS,
            "correction": False,
            "effect": "static",
            "animation_active": False,
            "native_signature": "",
            "last_applied_boot_id": "",
        }

    def _load_preferences(self):
        defaults = self._default_preferences()
        try:
            candidate = json.loads(self.preferences_path.read_text())
            if not isinstance(candidate, dict):
                return defaults
            # ABI 3 keeps its provider record at the top level. If a device
            # temporarily falls back to the legacy provider, its independent
            # preference record lives alongside (rather than replacing) the
            # dormant EVO editor/calibration state.
            if (candidate.get("version") == PREFERENCES_VERSION and
                    candidate.get("provider") == PROVIDER_POCKET_EVO_V3):
                candidate = candidate.get("legacy_preferences")
                if not isinstance(candidate, dict):
                    return defaults
            # Version-1 preferences predate multiple providers and therefore
            # belong only to the original strict sysfs-effects ABI.
            version = candidate.get("version")
            if isinstance(version, bool) or version not in (1, 2):
                raise ValueError
            if "provider" in candidate:
                if version != 2:
                    raise ValueError
                provider = candidate["provider"]
            else:
                if version != 1:
                    raise ValueError
                provider = PROVIDER_SYSFS_EFFECTS
            if provider not in (PROVIDER_SYSFS_EFFECTS, PROVIDER_ANALOG_STATIC):
                raise ValueError
            color = _source_color(candidate.get("source_color"))
            brightness = _bounded_integer(
                candidate.get("brightness"), "RGB brightness")
            correction = candidate.get("correction")
            if not isinstance(correction, bool):
                raise ValueError
            effect = candidate.get("effect")
            if effect not in RGB_EFFECTS:
                raise ValueError
            animation_active = candidate.get("animation_active")
            if not isinstance(animation_active, bool):
                raise ValueError
            native_signature = candidate.get("native_signature", "")
            last_boot = candidate.get("last_applied_boot_id", "")
            if not isinstance(native_signature, str) or not isinstance(last_boot, str):
                raise ValueError
            return {
                "version": LEGACY_PREFERENCES_VERSION,
                "provider": provider,
                "source_color": list(color),
                "brightness": brightness,
                "correction": correction,
                "effect": effect,
                "animation_active": animation_active,
                "native_signature": native_signature,
                "last_applied_boot_id": last_boot,
            }
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            return defaults

    def _save_preferences(self, preferences):
        value = dict(preferences)
        try:
            existing = json.loads(self.preferences_path.read_text())
        except (OSError, UnicodeError, TypeError, json.JSONDecodeError):
            existing = None

        saving_evo = (
            value.get("version") == PREFERENCES_VERSION and
            value.get("provider") == PROVIDER_POCKET_EVO_V3)
        existing_evo = (
            isinstance(existing, dict) and
            existing.get("version") == PREFERENCES_VERSION and
            existing.get("provider") == PROVIDER_POCKET_EVO_V3)
        if saving_evo:
            legacy = (
                existing.get("legacy_preferences")
                if existing_evo else existing)
            legacy_version = (
                legacy.get("version") if isinstance(legacy, dict) else None)
            if (isinstance(legacy, dict) and
                    not isinstance(legacy_version, bool) and
                    legacy_version in (1, 2)):
                # Preserve the original legacy object exactly; its own loader
                # remains responsible for validating it before presentation.
                value["legacy_preferences"] = legacy
        elif (existing_evo and
                not isinstance(value.get("version"), bool) and
                value.get("version") in (1, 2)):
            combined = dict(existing)
            combined["legacy_preferences"] = value
            value = combined
        _atomic_json(self.preferences_path, value)

    @staticmethod
    def _default_evo_lighting():
        return {
            "effect": "static",
            "layout_mode": "both",
            "zones": [
                {
                    "id": zone_id,
                    "color": list(DEFAULT_COLOR),
                    "brightness": DEFAULT_BRIGHTNESS,
                }
                for zone_id in EVO_ZONE_INDEX
            ],
            "color": list(DEFAULT_COLOR),
            "brightness": DEFAULT_BRIGHTNESS,
            "idle_color": list(DEFAULT_COLOR),
            "active_color": [0, 0, 255],
        }

    def _parse_evo_lighting_preference(self, value):
        try:
            return self._validated_evo_lighting(value, EVO_EFFECTS)
        except (TypeError, ValueError):
            return None

    def _default_evo_preferences(self):
        return {
            "version": PREFERENCES_VERSION,
            "provider": PROVIDER_POCKET_EVO_V3,
            "lighting": None,
            "native_lighting_revision": "",
            "resume_lighting": None,
            "calibration_override": None,
            "last_calibration_boot_id": "",
        }

    def _load_evo_preferences(self):
        defaults = self._default_evo_preferences()
        try:
            candidate = json.loads(self.preferences_path.read_text())
            if (not isinstance(candidate, dict) or
                    candidate.get("version") != PREFERENCES_VERSION or
                    candidate.get("provider") != PROVIDER_POCKET_EVO_V3):
                return defaults
            lighting = candidate.get("lighting")
            if lighting is not None:
                lighting = self._parse_evo_lighting_preference(lighting)
                if lighting is None:
                    raise ValueError
            resume = candidate.get("resume_lighting")
            if resume is not None:
                resume = self._parse_evo_lighting_preference(resume)
                if resume is None:
                    raise ValueError
            native_lighting_revision = candidate.get(
                "native_lighting_revision", "")
            if not isinstance(native_lighting_revision, str):
                raise ValueError
            calibration_override = candidate.get("calibration_override")
            if calibration_override is not None:
                if not isinstance(calibration_override, dict):
                    raise ValueError
                calibration_override = {
                    "green_percent": _bounded_integer(
                        calibration_override.get("green_percent"),
                        "green calibration", 100),
                    "blue_percent": _bounded_integer(
                        calibration_override.get("blue_percent"),
                        "blue calibration", 100),
                }
            last_boot = candidate.get("last_calibration_boot_id", "")
            if not isinstance(last_boot, str):
                raise ValueError
            return {
                "version": PREFERENCES_VERSION,
                "provider": PROVIDER_POCKET_EVO_V3,
                "lighting": lighting,
                "native_lighting_revision": native_lighting_revision,
                "resume_lighting": resume,
                "calibration_override": calibration_override,
                "last_calibration_boot_id": last_boot,
            }
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            return defaults

    @staticmethod
    def _default_htr3212_lighting():
        return {
            "effect": "static",
            "layout_mode": "both",
            "zones": [
                {
                    "id": zone_id,
                    "color": list(DEFAULT_COLOR),
                    "brightness": DEFAULT_BRIGHTNESS,
                }
                for zone_id in HTR3212_ZONE_INDEX
            ],
            # Keep the common zoned-lighting envelope so the frontend can use
            # one editor. These fields are dormant while Static is the only
            # effect offered by this provider.
            "color": list(DEFAULT_COLOR),
            "brightness": DEFAULT_BRIGHTNESS,
            "idle_color": list(DEFAULT_COLOR),
            "active_color": [0, 0, 255],
        }

    def _validated_htr3212_lighting(self, value):
        if not isinstance(value, dict):
            raise ValueError("Odin 3 RGB lighting must be an object")
        if value.get("effect") != "static":
            raise ValueError("Odin 3 RGB currently supports Static only")
        layout_mode = value.get("layout_mode")
        if layout_mode not in EVO_LAYOUT_MODES:
            raise ValueError("invalid Odin 3 RGB editing layout")
        source_zones = value.get("zones")
        if not isinstance(source_zones, list) or len(source_zones) != 8:
            raise ValueError("Odin 3 RGB layout must contain eight zones")
        zones = []
        for expected_id, source in zip(HTR3212_ZONE_INDEX, source_zones):
            if not isinstance(source, dict) or source.get("id") != expected_id:
                raise ValueError("Odin 3 RGB zones are out of order")
            zones.append({
                "id": expected_id,
                "color": list(_source_color(source.get("color"))),
                "brightness": _bounded_integer(
                    source.get("brightness"), "zone brightness"),
            })
        return {
            "effect": "static",
            "layout_mode": layout_mode,
            "zones": zones,
            "color": list(_source_color(value.get("color"))),
            "brightness": _bounded_integer(
                value.get("brightness"), "RGB brightness"),
            "idle_color": list(_source_color(value.get("idle_color"))),
            "active_color": list(_source_color(value.get("active_color"))),
        }

    def _default_htr3212_preferences(self):
        return {
            "version": HTR3212_PREFERENCE_VERSION,
            "provider": PROVIDER_HTR3212_STATIC,
            "mode": None,
            "lighting": None,
            "resume_lighting": None,
            "native_signature": "",
            "last_applied_boot_id": "",
        }

    def _load_htr3212_preferences(self):
        defaults = self._default_htr3212_preferences()
        try:
            candidate = json.loads(self.htr_preferences_path.read_text())
            version = candidate.get("version") if isinstance(candidate, dict) else None
            if (not isinstance(candidate, dict) or
                    version not in (1, 2, HTR3212_PREFERENCE_VERSION) or
                    candidate.get("provider") != PROVIDER_HTR3212_STATIC):
                return defaults
            # The short-lived v1 test build offered EVO-style software colour
            # correction. Preserve only v1 metadata known to describe raw RGB;
            # corrected source metadata cannot truthfully represent the cached
            # PWM values after that control is removed.
            if version == 1 and candidate.get("correction") is not False:
                return defaults
            lighting = candidate.get("lighting")
            if lighting is not None:
                lighting = self._validated_htr3212_lighting(lighting)
            resume = candidate.get("resume_lighting")
            if resume is not None:
                resume = self._validated_htr3212_lighting(resume)
            native_signature = candidate.get("native_signature")
            last_boot = candidate.get("last_applied_boot_id")
            if (not isinstance(native_signature, str) or
                    not isinstance(last_boot, str)):
                raise ValueError
            native_snapshot = self._htr3212_snapshot_from_signature(
                native_signature)
            if native_signature and native_snapshot is None:
                raise ValueError
            mode = candidate.get("mode")
            if version in (1, 2):
                mode = (
                    None if native_snapshot is None else
                    "rgb" if any(value for zone in native_snapshot
                                 for value in zone) else
                    "off"
                )
            if mode not in (None, "off", "rgb"):
                raise ValueError
            return {
                "version": HTR3212_PREFERENCE_VERSION,
                "provider": PROVIDER_HTR3212_STATIC,
                "mode": mode,
                "lighting": lighting,
                "resume_lighting": resume,
                "native_signature": native_signature,
                "last_applied_boot_id": last_boot,
            }
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            return defaults

    def _save_htr3212_preferences(self, preferences):
        _atomic_json(self.htr_preferences_path, preferences)

    @staticmethod
    def _htr3212_signature(snapshot):
        return " ".join(
            str(value) for zone in snapshot for value in zone)

    @staticmethod
    def _htr3212_snapshot_from_signature(signature):
        if not isinstance(signature, str):
            return None
        tokens = signature.split()
        if (len(tokens) != len(HTR3212_ZONE_INDEX) * 3 or
                any(re.fullmatch(r"[0-9]+", token) is None
                    for token in tokens)):
            return None
        values = tuple(int(token) for token in tokens)
        if any(value > 255 for value in values):
            return None
        return tuple(
            values[index:index + 3]
            for index in range(0, len(values), 3)
        )

    @staticmethod
    def _htr3212_revision(snapshot):
        return _state_revision(
            PROVIDER_HTR3212_STATIC,
            RGBController._htr3212_signature(snapshot),
        )

    @staticmethod
    def _read_htr3212_snapshot_once(channels):
        zones = []
        try:
            for zone_paths in channels:
                values = []
                for path in zone_paths:
                    raw = (path / "brightness").read_text().strip()
                    if re.fullmatch(r"[0-9]+", raw) is None:
                        return None
                    value = int(raw)
                    if value > 255:
                        return None
                    values.append(value)
                zones.append(tuple(values))
        except (OSError, UnicodeError):
            return None
        return tuple(zones)

    def _stable_htr3212_snapshot(self, channels):
        first = self._read_htr3212_snapshot_once(channels)
        second = self._read_htr3212_snapshot_once(channels)
        if first is None or second is None or first != second:
            return None
        return second

    def _htr3212_lighting_from_snapshot(self, snapshot):
        lighting = self._default_htr3212_lighting()
        zones = []
        for zone_id, output in zip(HTR3212_ZONE_INDEX, snapshot):
            # Native PWM cannot uniquely recover separate source colour and
            # perceptual level. The canonical lossless representation keeps
            # the exact PWM triplet as colour at full brightness.
            brightness = 255 if any(output) else 0
            color = list(output) if brightness else list(DEFAULT_COLOR)
            zones.append({
                "id": zone_id,
                "color": color,
                "brightness": brightness,
            })
        lighting.update({
            "layout_mode": _layout_mode(zones),
            "zones": zones,
        })
        return lighting

    @staticmethod
    def _htr3212_snapshot_from_lighting(lighting):
        return tuple(
            _htr3212_zone_output(zone)
            for zone in lighting["zones"]
        )

    @staticmethod
    def _evo_snapshot_revision(snapshot):
        return _state_revision(
            PROVIDER_POCKET_EVO_V3,
            " ".join(str(value) for value in snapshot["zone_layout"]),
            _evo_effect_command(snapshot["effect"]),
            " ".join(str(value) for value in snapshot["calibration"]),
            snapshot["enabled"],
        )

    @staticmethod
    def _evo_snapshot_lighting_revision(snapshot):
        """Identify both selected effect and its cached Static layout."""
        return _state_revision(
            PROVIDER_POCKET_EVO_V3,
            " ".join(str(value) for value in snapshot["zone_layout"]),
            _evo_effect_command(snapshot["effect"]),
        )

    def _read_evo_snapshot_once(self, path):
        try:
            layout = _decimal_values((path / "zone_layout").read_text(), 32)
            effect = _parse_evo_effect((path / "effect").read_text())
            calibration = _decimal_values(
                (path / "calibration").read_text(), 2, maximum=100)
            enabled = _decimal_values(
                (path / "enabled").read_text(), 1, maximum=1)
        except (OSError, UnicodeError):
            return None
        if None in (layout, effect, calibration, enabled):
            return None
        return {
            "zone_layout": layout,
            "effect": effect,
            "calibration": calibration,
            "enabled": enabled[0],
        }

    def _stable_evo_snapshot(self, path):
        first = self._read_evo_snapshot_once(path)
        second = self._read_evo_snapshot_once(path)
        if first is None or second is None or first != second:
            return None
        return second

    @staticmethod
    def _zones_from_layout(layout):
        return [
            {
                "id": zone_id,
                "color": list(layout[index:index + 3]),
                "brightness": layout[index + 3],
            }
            for zone_id, index in zip(EVO_ZONE_INDEX, range(0, 32, 4))
        ]

    def _lighting_from_snapshot(self, snapshot):
        zones = self._zones_from_layout(snapshot["zone_layout"])
        effect = snapshot["effect"]
        lighting = self._default_evo_lighting()
        lighting.update({
            "effect": effect["effect"],
            "layout_mode": _layout_mode(zones),
            "zones": zones,
        })
        for name in ("color", "brightness", "idle_color", "active_color"):
            if name in effect:
                value = effect[name]
                lighting[name] = list(value) if isinstance(value, tuple) else value
        return lighting

    @staticmethod
    def _evo_calibration_value(pair):
        return {
            "green_percent": pair[0],
            "blue_percent": pair[1],
        }

    def _effect_state(self, capabilities, raw_effect=None):
        if "breath" not in capabilities["effects"]:
            return "static", None, True
        parsed = _parse_effect(
            _read(self.led_path / "effect")
            if raw_effect is None else raw_effect)
        if parsed is None:
            return "static", None, False
        return parsed[0], parsed[1], True

    def _get_state_locked(self, capabilities=None):
        capabilities = capabilities or self.capabilities()
        if capabilities["provider"] == PROVIDER_HTR3212_STATIC:
            preferences = self._load_htr3212_preferences()
            base = {
                "supported": capabilities["available"],
                "valid": False,
                "provider": PROVIDER_HTR3212_STATIC,
                "revision": "",
                "modes": capabilities["modes"],
                "effects": capabilities["effects"],
                "shared_zone": False,
                "max_brightness": capabilities["max_brightness"],
                "mode": "unknown",
                "lighting": self._default_htr3212_lighting(),
                "resume_lighting": None,
                "error": capabilities.get("error", ""),
            }
            if not capabilities["available"]:
                return base
            status, channels, error = self._discover_htr3212()
            if status != "valid":
                base["supported"] = False
                base["error"] = error or "Odin 3 RGB interface disappeared"
                return base
            snapshot = self._stable_htr3212_snapshot(channels)
            if snapshot is None:
                base["error"] = "Odin 3 RGB state is unstable or malformed"
                return base
            signature = self._htr3212_signature(snapshot)
            mode = (
                "off" if all(value == 0 for zone in snapshot for value in zone)
                else "rgb")
            lighting = self._htr3212_lighting_from_snapshot(snapshot)
            resume = None
            if preferences["native_signature"] == signature:
                if mode == "rgb" and preferences["lighting"] is not None:
                    lighting = preferences["lighting"]
                if mode == "off":
                    resume = (
                        preferences["resume_lighting"] or
                        preferences["lighting"])
            base.update({
                "valid": True,
                "revision": self._htr3212_revision(snapshot),
                "mode": mode,
                "lighting": lighting,
                "resume_lighting": resume,
                "error": "",
            })
            return base
        if capabilities["provider"] == PROVIDER_POCKET_EVO_V3:
            preferences = self._load_evo_preferences()
            base = {
                "supported": capabilities["available"],
                "valid": False,
                "provider": PROVIDER_POCKET_EVO_V3,
                "revision": "",
                "modes": capabilities["modes"],
                "effects": capabilities["effects"],
                "shared_zone": False,
                "max_brightness": capabilities["max_brightness"],
                "mode": "unknown",
                "temporarily_gated": False,
                "lighting": self._default_evo_lighting(),
                "resume_lighting": None,
                "calibration": {
                    "green_percent": EVO_DEFAULT_CALIBRATION[0],
                    "blue_percent": EVO_DEFAULT_CALIBRATION[1],
                },
                "calibration_override": preferences["calibration_override"],
                "error": capabilities.get("error", ""),
            }
            if not capabilities["available"]:
                return base
            evo_status, evo_path, _effects, evo_error = self._discover_evo()
            if evo_status != "valid":
                base["supported"] = False
                base["error"] = evo_error or "Pocket EVO RGB interface disappeared"
                return base
            snapshot = self._stable_evo_snapshot(evo_path)
            if snapshot is None:
                base["error"] = "Pocket EVO RGB state is unstable or malformed"
                return base
            lighting = self._lighting_from_snapshot(snapshot)
            saved_lighting = preferences["lighting"]
            if (saved_lighting is not None and
                    preferences["native_lighting_revision"] and
                    preferences["native_lighting_revision"] ==
                    self._evo_snapshot_lighting_revision(snapshot)):
                # The exact native lighting command still matches RKE's saved
                # request. Preserve editor-only metadata such as Static scope,
                # cached zones under an advanced effect, and inactive effect
                # colours. A different native command remains authoritative.
                lighting = self._validated_evo_lighting(
                    saved_lighting, EVO_EFFECTS)
            is_off = (
                lighting["effect"] == "static" and
                all(value == 0 for value in snapshot["zone_layout"])
            )
            base.update({
                "valid": True,
                "revision": self._evo_snapshot_revision(snapshot),
                "mode": "off" if is_off else "rgb",
                "temporarily_gated": snapshot["enabled"] == 0,
                "lighting": lighting,
                "resume_lighting": (
                    preferences["resume_lighting"] if is_off else None),
                "calibration": self._evo_calibration_value(
                    snapshot["calibration"]),
                "error": "",
            })
            return base

        preferences = self._load_preferences()
        preferences_current = preferences["provider"] == capabilities["provider"]
        visible_preferences = (
            preferences if preferences_current else self._default_preferences())
        base = {
            "supported": capabilities["available"],
            "valid": False,
            "provider": capabilities["provider"],
            "zones_differ": False,
            "revision": "",
            "modes": capabilities["modes"],
            "effects": capabilities["effects"],
            "shared_zone": capabilities["shared_zone"],
            "max_brightness": capabilities["max_brightness"],
            "mode": "unknown",
            "effect": visible_preferences["effect"],
            "color": list(visible_preferences["source_color"]),
            "brightness": visible_preferences["brightness"],
            "correction": visible_preferences["correction"],
            "error": capabilities.get("error", ""),
        }
        if not capabilities["available"]:
            return base

        if capabilities["provider"] == PROVIDER_ANALOG_STATIC:
            native_raw = self.get_setting("analogsticks.led", "")
            native = _parse_native_config(native_raw)
            base["revision"] = _state_revision(
                PROVIDER_ANALOG_STATIC, native_raw)
            # Capability detection already required this exact state to be
            # valid. Recheck it so a concurrent external edit is never
            # replaced with fabricated values.
            if native is None:
                base["error"] = "ROCKNIX analogue-stick RGB state is unavailable"
                return base
            native_brightness, _native_color, signature = native
            fields = [int(value) for value in signature.split()]
            right_color = tuple(fields[1:4])
            left_color = tuple(fields[4:7])
            base["mode"] = "rgb" if native_brightness > 0 else "off"
            base["effect"] = "static"
            base["zones_differ"] = right_color != left_color
            if native_brightness > 0:
                base["brightness"] = native_brightness
            elif visible_preferences["brightness"] <= 0:
                base["brightness"] = DEFAULT_BRIGHTNESS
            if (preferences_current and
                    signature == preferences["native_signature"]):
                base["color"] = list(visible_preferences["source_color"])
                base["correction"] = visible_preferences["correction"]
            else:
                # The generic public helper treats the right-hand RGB fields
                # as authoritative. Do not invent a colour by combining two
                # unequal zones; an explicit Save intentionally unifies them.
                base["color"] = list(right_color)
                base["correction"] = False
            base["valid"] = True
            return base

        mode = self.get_setting("led.color", "")
        native_raw = self.get_setting("analogsticks.led", "")
        effect_raw = _read(self.led_path / "effect")
        base["revision"] = _state_revision(
            PROVIDER_SYSFS_EFFECTS, mode, native_raw, effect_raw)
        if mode not in RGB_MODES:
            base["error"] = "ROCKNIX LED Color state is unavailable"
            return base

        native = _parse_native_config(native_raw)
        effect, effect_color, effect_valid = self._effect_state(
            capabilities, effect_raw)
        base["mode"] = mode
        if mode == "rgb":
            base["effect"] = effect
        elif visible_preferences["effect"] not in capabilities["effects"]:
            base["effect"] = "static"

        if native is not None:
            brightness, native_color, signature = native
            base["brightness"] = brightness
            if (preferences_current and
                    signature == preferences["native_signature"]):
                base["color"] = list(visible_preferences["source_color"])
                base["correction"] = visible_preferences["correction"]
            else:
                # A ROCKNIX-side edit is authoritative. Present its effective
                # colour and disable correction rather than pretending the
                # older RKE source colour still describes the hardware.
                base["color"] = list(effect_color or native_color)
                base["correction"] = False

        # Off and Battery are complete native modes in their own right. Their
        # validity must not depend on an RGB-only effect attribute or saved
        # analogue-stick colour. RGB requires both pieces to be readable.
        base["valid"] = bool(
            mode != "rgb" or (effect_valid and native is not None))
        if not base["valid"]:
            base["error"] = (
                "ROCKNIX RGB state is unavailable" if mode == "rgb"
                else "RGB effect state is unavailable")
        return base

    def get_state(self):
        with self.thread_lock, _exclusive_lock(self.lock_path):
            return self._get_state_locked()

    def _validated_evo_lighting(self, value, available_effects):
        if not isinstance(value, dict):
            raise ValueError("Pocket EVO lighting must be an object")
        effect = value.get("effect")
        if effect not in EVO_EFFECTS or effect not in available_effects:
            raise ValueError("unsupported Pocket EVO RGB effect")
        layout_mode = value.get("layout_mode")
        if layout_mode not in EVO_LAYOUT_MODES:
            raise ValueError("invalid Pocket EVO Static editing layout")
        source_zones = value.get("zones")
        if not isinstance(source_zones, list) or len(source_zones) != 8:
            raise ValueError("Pocket EVO Static layout must contain eight zones")
        zones = []
        for expected_id, source in zip(EVO_ZONE_INDEX, source_zones):
            if not isinstance(source, dict) or source.get("id") != expected_id:
                raise ValueError("Pocket EVO Static zones are out of order")
            zones.append({
                "id": expected_id,
                "color": list(_source_color(source.get("color"))),
                "brightness": _bounded_integer(
                    source.get("brightness"), "zone brightness"),
            })
        # The provider carries a complete editor draft, including values not
        # used by the selected effect. Only the selected effect's exact native
        # grammar is emitted.
        color = list(_source_color(value.get("color")))
        brightness = _bounded_integer(
            value.get("brightness"), "RGB brightness")
        idle_color = list(_source_color(value.get("idle_color")))
        active_color = list(_source_color(value.get("active_color")))
        return {
            "effect": effect,
            "layout_mode": layout_mode,
            "zones": zones,
            "color": color,
            "brightness": brightness,
            "idle_color": idle_color,
            "active_color": active_color,
        }

    def _validated_evo_request(self, request, capabilities, current):
        if not isinstance(request, dict):
            raise ValueError("RGB request must be an object")
        if request.get("provider") != PROVIDER_POCKET_EVO_V3:
            raise ValueError("RGB provider changed; refresh before applying")
        if request.get("revision") != current["revision"]:
            raise ValueError("RGB state changed; refresh before applying")
        mode = request.get("mode")
        if mode not in ("off", "rgb"):
            raise ValueError("Pocket EVO RGB mode must be Off or RGB")
        lighting = self._validated_evo_lighting(
            request.get("lighting"), capabilities["effects"])
        return mode, lighting

    @staticmethod
    def _write_evo_attribute(path, value):
        """Emit one validated ABI command through exactly one ``os.write``."""
        payload = (value + "\n").encode("ascii")
        descriptor = None
        try:
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CLOEXEC | os.O_TRUNC)
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise OSError("short Pocket EVO RGB sysfs write")
        except OSError as reason:
            if reason.errno == errno.EBUSY:
                raise RuntimeError(
                    "Pocket EVO RGB transport is suspended; retry after resume") from reason
            if reason.errno in (errno.ENOENT, errno.ENODEV):
                raise RuntimeError(
                    "Pocket EVO RGB interface disappeared; refresh and retry") from reason
            raise RuntimeError(
                f"unable to apply Pocket EVO RGB state: {reason}") from reason
        except UnicodeError as reason:
            raise RuntimeError(
                f"unable to apply Pocket EVO RGB state: {reason}") from reason
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _evo_selected_native(snapshot):
        effect = snapshot["effect"]
        if effect["effect"] == "static":
            return "zone_layout", " ".join(
                str(value) for value in snapshot["zone_layout"])
        return "effect", _evo_effect_command(effect)

    @staticmethod
    def _evo_lighting_native(lighting):
        if lighting["effect"] == "static":
            return "zone_layout", " ".join(
                str(value) for value in _evo_layout_values(lighting["zones"]))
        return "effect", _evo_effect_command(lighting)

    def _evo_snapshot_matches_command(self, snapshot, attribute, command):
        if snapshot is None:
            return False
        selected_attribute, selected_command = self._evo_selected_native(snapshot)
        return selected_attribute == attribute and selected_command == command

    @staticmethod
    def _evo_expected_snapshot(previous, attribute, command):
        """Return the exact native state one successful lighting write owns."""
        expected = {
            "zone_layout": previous["zone_layout"],
            "effect": previous["effect"],
            "calibration": previous["calibration"],
            "enabled": previous["enabled"],
        }
        if attribute == "zone_layout":
            layout = _decimal_values(command, 32)
            if layout is None:
                raise ValueError("invalid Pocket EVO Static layout command")
            expected["zone_layout"] = layout
            expected["effect"] = {"effect": "static"}
            return expected
        if attribute == "effect":
            effect = _parse_evo_effect(command)
            if effect is None or effect["effect"] == "static":
                raise ValueError("invalid Pocket EVO effect command")
            expected["effect"] = effect
            return expected
        raise ValueError("invalid Pocket EVO lighting attribute")

    def _evo_preferences_after_lighting(
            self, previous, lighting, mode, native_snapshot):
        result = dict(previous)
        result.update({
            "version": PREFERENCES_VERSION,
            "provider": PROVIDER_POCKET_EVO_V3,
            "lighting": lighting,
            "native_lighting_revision":
                self._evo_snapshot_lighting_revision(native_snapshot),
        })
        if mode == "rgb":
            result["resume_lighting"] = lighting
        return result

    def _guarded_evo_lighting_rollback(
            self, path, applied_snapshot, previous_snapshot):
        current = self._stable_evo_snapshot(path)
        if current != applied_snapshot:
            return False

        previous_layout = " ".join(
            str(value) for value in previous_snapshot["zone_layout"])
        try:
            # ``zone_layout`` is cached even while an advanced effect is
            # selected. Restore it first so a failed advanced -> Static Save
            # cannot leak the rejected Static draft the next time another
            # client selects Static.
            if current["zone_layout"] != previous_snapshot["zone_layout"]:
                self._write_evo_attribute(
                    path / "zone_layout", previous_layout)
                current = self._stable_evo_snapshot(path)
                if (current is None or
                        current["zone_layout"] !=
                        previous_snapshot["zone_layout"] or
                        current["effect"] != {"effect": "static"} or
                        current["calibration"] !=
                        previous_snapshot["calibration"] or
                        current["enabled"] != previous_snapshot["enabled"]):
                    return False

            attribute, command = self._evo_selected_native(previous_snapshot)
            if not self._evo_snapshot_matches_command(
                    current, attribute, command):
                self._write_evo_attribute(path / attribute, command)
        except RuntimeError:
            return False
        restored = self._stable_evo_snapshot(path)
        return restored == previous_snapshot

    def _set_evo_state_locked(self, request, capabilities, current):
        mode, requested_lighting = self._validated_evo_request(
            request, capabilities, current)
        evo_status, path, _effects, evo_error = self._discover_evo()
        if evo_status != "valid":
            raise RuntimeError(
                evo_error or "Pocket EVO RGB interface disappeared")
        previous_snapshot = self._stable_evo_snapshot(path)
        if (previous_snapshot is None or
                self._evo_snapshot_revision(previous_snapshot) !=
                current["revision"]):
            raise ValueError("RGB state changed; refresh before applying")
        preferences = self._load_evo_preferences()

        if mode == "off":
            # Preserve the complete validated editor draft for explicit On.
            # Under an advanced effect the native ``zone_layout`` is hidden
            # cache and can be all-zero after an earlier Off cycle; the
            # revision-bound request retains the intended Static zones and
            # any edits the user made before selecting Off.
            if current["mode"] != "off":
                preferences["resume_lighting"] = requested_lighting
            applied_lighting = self._default_evo_lighting()
            applied_lighting["zones"] = [
                {"id": zone_id, "color": [0, 0, 0], "brightness": 0}
                for zone_id in EVO_ZONE_INDEX
            ]
            applied_lighting["layout_mode"] = "both"
            attribute = "zone_layout"
            command = " ".join("0" for _ in range(32))
        else:
            applied_lighting = requested_lighting
            # A plain On request made from the native all-zero Off state uses
            # the saved last non-off state. An edited nonzero draft remains
            # authoritative and is applied directly.
            if (current["mode"] == "off" and
                    requested_lighting["effect"] == "static" and
                    all(value == 0 for value in
                        _evo_layout_values(requested_lighting["zones"])) and
                    preferences["resume_lighting"] is not None):
                applied_lighting = preferences["resume_lighting"]
            attribute, command = self._evo_lighting_native(applied_lighting)
        expected_snapshot = self._evo_expected_snapshot(
            previous_snapshot, attribute, command)

        if self._evo_snapshot_matches_command(
                previous_snapshot, attribute, command):
            new_preferences = self._evo_preferences_after_lighting(
                preferences, applied_lighting, mode, previous_snapshot)
            self._save_preferences(new_preferences)
            return self._get_state_locked(capabilities)

        try:
            self._write_evo_attribute(path / attribute, command)
        except RuntimeError:
            # A transport error can occur after some LEDs visibly changed.
            # Reconcile every ABI component before releasing RKE's queue.
            self._stable_evo_snapshot(path)
            raise
        applied_snapshot = self._stable_evo_snapshot(path)
        if applied_snapshot != expected_snapshot:
            # Reconcile before returning an error. Do not claim the requested
            # state or persist it when any native component differs. This
            # also detects another client changing the hidden Static layout
            # and then re-selecting the same advanced effect before readback.
            self._stable_evo_snapshot(path)
            raise RuntimeError("Pocket EVO RGB native readback did not match")
        new_preferences = self._evo_preferences_after_lighting(
            preferences, applied_lighting, mode, applied_snapshot)
        try:
            self._save_preferences(new_preferences)
        except Exception:
            self._guarded_evo_lighting_rollback(
                path, applied_snapshot, previous_snapshot)
            raise
        return self._get_state_locked(capabilities)

    def _validated_htr3212_request(self, request, current):
        if not isinstance(request, dict):
            raise ValueError("RGB request must be an object")
        if request.get("provider") != PROVIDER_HTR3212_STATIC:
            raise ValueError("RGB provider changed; refresh before applying")
        if request.get("revision") != current["revision"]:
            raise ValueError("RGB state changed; refresh before applying")
        mode = request.get("mode")
        if mode not in ("off", "rgb"):
            raise ValueError("Odin 3 RGB mode must be Off or RGB")
        lighting = self._validated_htr3212_lighting(request.get("lighting"))
        return mode, lighting

    @staticmethod
    def _write_htr3212_brightness(path, value):
        payload = f"{_bounded_integer(value, 'HTR3212 PWM')}\n".encode("ascii")
        descriptor = None
        try:
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CLOEXEC | os.O_TRUNC)
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise OSError("short Odin 3 RGB sysfs write")
        except OSError as reason:
            if reason.errno == errno.EBUSY:
                raise RuntimeError(
                    "Odin 3 RGB transport is suspended; retry after resume") from reason
            if reason.errno in (errno.ENOENT, errno.ENODEV):
                raise RuntimeError(
                    "Odin 3 RGB interface disappeared; refresh and retry") from reason
            raise RuntimeError(
                f"unable to apply Odin 3 RGB state: {reason}") from reason
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _guarded_htr3212_rollback(
            self, channels, owned_snapshot, previous_snapshot):
        expected = owned_snapshot
        if self._stable_htr3212_snapshot(channels) != expected:
            return False
        expected_mutable = [list(zone) for zone in expected]
        try:
            for zone_index, zone_paths in enumerate(channels):
                for color_index, path in enumerate(zone_paths):
                    previous = previous_snapshot[zone_index][color_index]
                    if expected_mutable[zone_index][color_index] == previous:
                        continue
                    if (self._stable_htr3212_snapshot(channels) !=
                            tuple(tuple(zone) for zone in expected_mutable)):
                        return False
                    self._write_htr3212_brightness(
                        path / "brightness", previous)
                    expected_mutable[zone_index][color_index] = previous
                    if _read(path / "brightness") != str(previous):
                        return False
        except (RuntimeError, OSError, UnicodeError):
            return False
        return self._stable_htr3212_snapshot(channels) == previous_snapshot

    def _apply_htr3212_snapshot(
            self, channels, previous_snapshot, target_snapshot):
        expected = [list(zone) for zone in previous_snapshot]
        last_before = previous_snapshot
        writes_started = False
        try:
            for zone_index, zone_paths in enumerate(channels):
                for color_index, path in enumerate(zone_paths):
                    target = target_snapshot[zone_index][color_index]
                    if expected[zone_index][color_index] == target:
                        continue
                    last_before = tuple(tuple(zone) for zone in expected)
                    # Sysfs exposes 24 independent attributes, not one atomic
                    # layout command.  Recheck the complete cached state before
                    # every changed channel. Divergence observed between writes
                    # aborts the operation before RK-Enhanced advances farther.
                    # The LED class offers no compare-and-swap operation, so the
                    # final check/write pair itself cannot be atomic.
                    if self._stable_htr3212_snapshot(channels) != last_before:
                        if not writes_started:
                            raise ValueError(
                                "RGB state changed; refresh before applying")
                        raise RuntimeError(
                            "Odin 3 RGB state changed during apply; "
                            "refresh and retry")
                    expected[zone_index][color_index] = target
                    # The sysfs call may have changed the driver's cached value
                    # even when userspace receives a later error, so consider
                    # the transaction uncertain from this point onward.
                    writes_started = True
                    self._write_htr3212_brightness(
                        path / "brightness", target)
                    if _read(path / "brightness") != str(target):
                        raise RuntimeError(
                            "Odin 3 RGB cached channel state did not match")
        except Exception:
            owned = self._stable_htr3212_snapshot(channels)
            expected_snapshot = tuple(tuple(zone) for zone in expected)
            if owned in (last_before, expected_snapshot):
                self._guarded_htr3212_rollback(
                    channels, owned, previous_snapshot)
            raise
        applied = self._stable_htr3212_snapshot(channels)
        if applied != target_snapshot:
            raise RuntimeError("Odin 3 RGB cached native state did not match")
        return applied

    def _set_htr3212_state_locked(self, request, capabilities, current):
        mode, lighting = self._validated_htr3212_request(
            request, current)
        status, channels, error = self._discover_htr3212()
        if status != "valid":
            raise RuntimeError(error or "Odin 3 RGB interface disappeared")
        # ``brightness`` is the Linux LED class driver's cached sysfs state;
        # this HTR3212 driver does not expose physical register readback.
        previous_snapshot = self._stable_htr3212_snapshot(channels)
        if (previous_snapshot is None or
                self._htr3212_revision(previous_snapshot) !=
                current["revision"]):
            raise ValueError("RGB state changed; refresh before applying")
        preferences = self._load_htr3212_preferences()
        if (mode == "rgb" and current["mode"] == "off" and
                all(zone["brightness"] == 0 for zone in lighting["zones"]) and
                preferences["resume_lighting"] is not None):
            lighting = preferences["resume_lighting"]
        target_snapshot = (
            tuple((0, 0, 0) for _zone in HTR3212_ZONE_INDEX)
            if mode == "off" else
            self._htr3212_snapshot_from_lighting(lighting)
        )
        if target_snapshot == previous_snapshot:
            applied_snapshot = previous_snapshot
        else:
            applied_snapshot = self._apply_htr3212_snapshot(
                channels, previous_snapshot, target_snapshot)

        preferences.update({
            "version": HTR3212_PREFERENCE_VERSION,
            "provider": PROVIDER_HTR3212_STATIC,
            "mode": mode,
            "lighting": lighting,
            "resume_lighting": lighting,
            "native_signature": self._htr3212_signature(applied_snapshot),
            "last_applied_boot_id": _read(self.boot_id_path),
        })
        try:
            self._save_htr3212_preferences(preferences)
        except Exception:
            if applied_snapshot != previous_snapshot:
                self._guarded_htr3212_rollback(
                    channels, applied_snapshot, previous_snapshot)
            raise
        return self._get_state_locked(capabilities)

    def _validated_request(self, request, capabilities, current_revision):
        if not isinstance(request, dict):
            raise ValueError("RGB request must be an object")
        if request.get("provider") != capabilities["provider"]:
            raise ValueError("RGB provider changed; refresh before applying")
        if request.get("revision") != current_revision:
            raise ValueError("RGB state changed; refresh before applying")
        mode = request.get("mode")
        effect = request.get("effect")
        if mode not in RGB_MODES or mode not in capabilities["modes"]:
            raise ValueError("LED Color must be Off, Battery, or RGB")
        if effect not in RGB_EFFECTS or effect not in capabilities["effects"]:
            raise ValueError("unsupported RGB effect")
        color = _source_color(request.get("color"))
        brightness = _bounded_integer(
            request.get("brightness"), "RGB brightness",
            capabilities["max_brightness"])
        correction = request.get("correction")
        if not isinstance(correction, bool):
            raise ValueError("colour correction must be true or false")
        return mode, effect, color, brightness, correction

    def _preferences(self, provider, source, brightness, correction, effect,
                     animation_active, signature, boot_id=""):
        return {
            "version": LEGACY_PREFERENCES_VERSION,
            "provider": provider,
            "source_color": list(source),
            "brightness": brightness,
            "correction": correction,
            "effect": effect,
            "animation_active": animation_active,
            "native_signature": signature,
            "last_applied_boot_id": boot_id,
        }

    def _write_effect(self, effect, output_color):
        path = self.led_path / "effect"
        if effect == "breath":
            red, green, blue = output_color
            value = f"breath {red} {green} {blue}\n"
        elif effect == "rainbow":
            value = "rainbow\n"
        else:
            raise ValueError("only animated effects may be written directly")
        try:
            path.write_text(value)
        except OSError as reason:
            raise RuntimeError("unable to apply the RGB animation") from reason

    def _restore_analog_static(self, previous_native, failed_native, *, reapply):
        """Best-effort rollback without clobbering a newer external write."""
        try:
            current = _parse_native_config(
                self.get_setting("analogsticks.led", ""))
            if current is None:
                return False
            current_native = current[2]
            if current_native == failed_native:
                self.set_setting("analogsticks.led", previous_native)
                restored = _parse_native_config(
                    self.get_setting("analogsticks.led", ""))
                if restored is None or restored[2] != previous_native:
                    return False
                target = previous_native
            elif current_native == previous_native:
                target = previous_native
            else:
                # ROCKNIX or another controller won the race. Preserve its
                # valid setting; if hardware may have changed, align it with
                # that newer value rather than restoring RKE's old snapshot.
                target = current_native
            if reapply:
                latest = _parse_native_config(
                    self.get_setting("analogsticks.led", ""))
                if latest is None:
                    return False
                # Narrow the final read/apply window and always prefer a newer
                # valid external value over either RKE snapshot.
                target = latest[2]
                self.run([
                    str(self.analog_sticks_led_control),
                    *target.split(),
                ])
            return True
        except Exception:
            return False

    def set_state(self, request):
        with self.thread_lock, _exclusive_lock(self.lock_path):
            capabilities = self.capabilities()
            if not capabilities["available"]:
                raise RuntimeError("RGB control is unsupported on this device")
            current = self._get_state_locked(capabilities)
            if not current["valid"]:
                raise RuntimeError(current["error"] or "RGB state is unavailable")
            if capabilities["provider"] == PROVIDER_HTR3212_STATIC:
                return self._set_htr3212_state_locked(
                    request, capabilities, current)
            if capabilities["provider"] == PROVIDER_POCKET_EVO_V3:
                return self._set_evo_state_locked(
                    request, capabilities, current)
            mode, effect, source, brightness, correction = (
                self._validated_request(
                    request, capabilities, current["revision"]))

            if capabilities["provider"] == PROVIDER_ANALOG_STATIC:
                previous_raw = self.get_setting("analogsticks.led", "")
                previous_native_state = _parse_native_config(previous_raw)
                if (previous_native_state is None or
                        _state_revision(
                            PROVIDER_ANALOG_STATIC, previous_raw) !=
                        current["revision"]):
                    raise ValueError("RGB state changed; refresh before applying")
                previous_native = previous_native_state[2]
                previous = self._load_preferences()
                previous_brightness = (
                    previous["brightness"]
                    if (previous["provider"] == PROVIDER_ANALOG_STATIC and
                        previous["brightness"] > 0)
                    else DEFAULT_BRIGHTNESS)
                if mode == "off":
                    applied_brightness = 0
                    remembered_brightness = (
                        brightness if brightness > 0 else
                        previous_brightness)
                else:
                    applied_brightness = (
                        brightness if brightness > 0 else
                        previous_brightness)
                    remembered_brightness = applied_brightness
                output = corrected_color(source, correction)
                native = _native_config(applied_brightness, output)
                try:
                    self.set_setting("analogsticks.led", native)
                except Exception:
                    self._restore_analog_static(
                        previous_native, native, reapply=False)
                    raise
                persisted = _parse_native_config(
                    self.get_setting("analogsticks.led", ""))
                if persisted is None or persisted[2] != native:
                    self._restore_analog_static(
                        previous_native, native, reapply=False)
                    raise RuntimeError(
                        "ROCKNIX did not persist the requested RGB colour")
                # The public helper consumes the exact seven values that were
                # reread from ROCKNIX's persisted setting. It has no probe or
                # mode command, and generic devices never use ``ledcontrol``.
                try:
                    self.run([
                        str(self.analog_sticks_led_control),
                        *persisted[2].split(),
                    ])
                    self._save_preferences(self._preferences(
                        PROVIDER_ANALOG_STATIC, source, remembered_brightness,
                        correction, "static", False, native))
                except Exception:
                    self._restore_analog_static(
                        previous_native, native, reapply=True)
                    raise
                return self._get_state_locked(capabilities)

            if mode in ("off", "battery"):
                self.run([str(self.led_control), mode])
                previous = self._load_preferences()
                previous_signature = (
                    previous["native_signature"]
                    if previous["provider"] == PROVIDER_SYSFS_EFFECTS else
                    (_parse_native_config(
                        self.get_setting("analogsticks.led", "")) or
                     (None, None, ""))[2])
                self._save_preferences(self._preferences(
                    PROVIDER_SYSFS_EFFECTS, source, brightness, correction,
                    effect, False,
                    previous_signature))
                return self._get_state_locked(capabilities)

            # Rainbow is intentionally unaffected by colour correction. Its
            # native Static fallback therefore receives the source colour.
            output = corrected_color(
                source, correction and effect in ("static", "breath"))
            native = _native_config(brightness, output)
            self.set_setting("analogsticks.led", native)
            persisted = _parse_native_config(
                self.get_setting("analogsticks.led", ""))
            if persisted is None or persisted[2] != native:
                # Never switch ROCKNIX into RGB mode unless its native Static
                # fallback was persisted exactly as requested.
                raise RuntimeError(
                    "ROCKNIX did not persist the requested RGB colour")
            try:
                self.run([str(self.led_control), "rgb"])
            except Exception:
                # Persistence succeeded but the native mode switch did not.
                # Retain the uncorrected source metadata so the corrected
                # native output is never mistaken for the user's source on a
                # later read. A failed activation must not arm an animation.
                self._save_preferences(self._preferences(
                    PROVIDER_SYSFS_EFFECTS, source, brightness, correction,
                    effect, False, native))
                raise

            if effect in ANIMATED_EFFECTS:
                try:
                    self._write_effect(effect, output)
                except RuntimeError:
                    # ledcontrol rgb has already left the rings in the safe,
                    # natively persisted Static state. Never arm a failed
                    # animation for startup reapplication.
                    self._save_preferences(self._preferences(
                        PROVIDER_SYSFS_EFFECTS, source, brightness, correction,
                        effect, False, native))
                    raise

            boot_id = _read(self.boot_id_path) if effect in ANIMATED_EFFECTS else ""
            self._save_preferences(self._preferences(
                PROVIDER_SYSFS_EFFECTS, source, brightness, correction, effect,
                effect in ANIMATED_EFFECTS, native, boot_id))
            return self._get_state_locked(capabilities)

    def _guarded_evo_calibration_rollback(
            self, path, applied_snapshot, previous_pair):
        current = self._stable_evo_snapshot(path)
        if current != applied_snapshot:
            return False
        command = f"{previous_pair[0]} {previous_pair[1]}"
        try:
            self._write_evo_attribute(path / "calibration", command)
        except RuntimeError:
            return False
        restored = self._stable_evo_snapshot(path)
        return bool(
            restored is not None and
            restored["calibration"] == previous_pair and
            restored["zone_layout"] == applied_snapshot["zone_layout"] and
            restored["effect"] == applied_snapshot["effect"] and
            restored["enabled"] == applied_snapshot["enabled"]
        )

    def set_calibration(self, request):
        """Save, reset, or select raw ABI 3 native colour calibration."""
        with self.thread_lock, _exclusive_lock(self.lock_path):
            capabilities = self.capabilities()
            if (not capabilities["available"] or
                    capabilities["provider"] != PROVIDER_POCKET_EVO_V3):
                raise RuntimeError(
                    "Pocket EVO RGB calibration is unsupported on this device")
            current = self._get_state_locked(capabilities)
            if not current["valid"]:
                raise RuntimeError(current["error"] or "RGB state is unavailable")
            if not isinstance(request, dict):
                raise ValueError("calibration request must be an object")
            if request.get("provider") != PROVIDER_POCKET_EVO_V3:
                raise ValueError("RGB provider changed; refresh before applying")
            if request.get("revision") != current["revision"]:
                raise ValueError("RGB state changed; refresh before applying")
            action = request.get("action")
            if action == "save":
                target = (
                    _bounded_integer(
                        request.get("green_percent"), "green calibration", 100),
                    _bounded_integer(
                        request.get("blue_percent"), "blue calibration", 100),
                )
                override = self._evo_calibration_value(target)
            elif action == "reset":
                target = EVO_DEFAULT_CALIBRATION
                override = None
            elif action == "raw":
                target = EVO_RAW_CALIBRATION
                override = self._evo_calibration_value(target)
            else:
                raise ValueError("calibration action must be Save, Reset, or Raw")

            evo_status, path, _effects, evo_error = self._discover_evo()
            if evo_status != "valid":
                raise RuntimeError(
                    evo_error or "Pocket EVO RGB interface disappeared")
            previous_snapshot = self._stable_evo_snapshot(path)
            if (previous_snapshot is None or
                    self._evo_snapshot_revision(previous_snapshot) !=
                    current["revision"]):
                raise ValueError("RGB state changed; refresh before applying")
            previous_pair = previous_snapshot["calibration"]
            command = f"{target[0]} {target[1]}"
            try:
                self._write_evo_attribute(path / "calibration", command)
            except RuntimeError:
                self._stable_evo_snapshot(path)
                raise
            applied_snapshot = self._stable_evo_snapshot(path)
            if (applied_snapshot is None or
                    applied_snapshot["calibration"] != target):
                self._stable_evo_snapshot(path)
                raise RuntimeError(
                    "Pocket EVO RGB calibration readback did not match")

            preferences = self._load_evo_preferences()
            native_lighting = self._lighting_from_snapshot(applied_snapshot)
            if preferences["lighting"] is None:
                preferences["lighting"] = native_lighting
                preferences["native_lighting_revision"] = (
                    self._evo_snapshot_lighting_revision(applied_snapshot))
            if (current["mode"] != "off" and
                    preferences["resume_lighting"] is None):
                preferences["resume_lighting"] = native_lighting
            preferences["calibration_override"] = override
            preferences["last_calibration_boot_id"] = (
                _read(self.boot_id_path) if override is not None else "")
            try:
                self._save_preferences(preferences)
            except Exception:
                self._guarded_evo_calibration_rollback(
                    path, applied_snapshot, previous_pair)
                raise
            return self._get_state_locked(capabilities)

    def reapply_startup(self):
        """Restore eligible persisted RGB state once per boot, without polling."""
        with self.thread_lock, _exclusive_lock(self.lock_path):
            evo_status, evo_path, _evo_effects, _evo_error = self._discover_evo()
            if evo_status == "invalid":
                return False
            if evo_status == "valid":
                preferences = self._load_evo_preferences()
                override = preferences["calibration_override"]
                if override is None:
                    return False
                boot_id = _read(self.boot_id_path)
                if (not boot_id or
                        boot_id == preferences["last_calibration_boot_id"]):
                    return False
                previous_snapshot = self._stable_evo_snapshot(evo_path)
                if previous_snapshot is None:
                    return False
                target = (
                    override["green_percent"],
                    override["blue_percent"],
                )
                # Tombstone this boot before touching the volatile native
                # value. If an external writer wins between our write and
                # verification, a later Decky restart must not retry and
                # overwrite that now-authoritative calibration.
                preferences["last_calibration_boot_id"] = boot_id
                self._save_preferences(preferences)
                self._write_evo_attribute(
                    evo_path / "calibration", f"{target[0]} {target[1]}")
                applied_snapshot = self._stable_evo_snapshot(evo_path)
                if (applied_snapshot is None or
                        applied_snapshot["calibration"] != target):
                    return False
                # The caller can distinguish this from legacy animation
                # restoration and log the operation accurately.
                return "calibration"
            htr_status, htr_channels, _htr_error = self._discover_htr3212()
            if htr_status == "invalid":
                return False
            if htr_status == "valid":
                preferences = self._load_htr3212_preferences()
                mode = preferences["mode"]
                lighting = preferences["lighting"]
                boot_id = _read(self.boot_id_path)
                if (mode not in ("off", "rgb") or lighting is None or
                        not boot_id or
                        boot_id == preferences["last_applied_boot_id"]):
                    return False
                target = self._htr3212_snapshot_from_signature(
                    preferences["native_signature"])
                if target is None:
                    return False
                # Off is an exact all-zero output while RGB must remain exactly
                # reproducible from the complete validated editor state.
                # Keeping the mode explicit prevents resume lighting from ever
                # being mistaken for the last successfully applied state.
                if ((mode == "off" and
                     any(value for zone in target for value in zone)) or
                        (mode == "rgb" and
                         target != self._htr3212_snapshot_from_lighting(
                             lighting))):
                    return False
                current = self._stable_htr3212_snapshot(htr_channels)
                if current is None:
                    return False
                # Mark this boot handled before touching any volatile channel.
                # A Decky reload must never turn a transient write failure into
                # an automatic rewrite loop. Save & Apply remains the explicit
                # retry path and records the current boot again on success.
                preferences["last_applied_boot_id"] = boot_id
                self._save_htr3212_preferences(preferences)
                if current == target:
                    return False
                self._apply_htr3212_snapshot(
                    htr_channels, current, target)
                return PROVIDER_HTR3212_STATIC
            # Generic static devices require explicit Save only. Return before
            # their capability flag or persisted seven-field state is read.
            if not self._sysfs_effects_available():
                return False
            capabilities = self._provider_capabilities(PROVIDER_SYSFS_EFFECTS)
            preferences = self._load_preferences()
            effect = preferences["effect"]
            if (preferences["provider"] != PROVIDER_SYSFS_EFFECTS or
                    not preferences["animation_active"] or
                    effect not in ANIMATED_EFFECTS or
                    effect not in capabilities["effects"] or
                    self.get_setting("led.color", "") != "rgb"):
                return False
            boot_id = _read(self.boot_id_path)
            if not boot_id or boot_id == preferences["last_applied_boot_id"]:
                return False
            native = _parse_native_config(
                self.get_setting("analogsticks.led", ""))
            if native is None or native[2] != preferences["native_signature"]:
                # An external ROCKNIX edit wins over stale RKE animation
                # metadata. Do not rewrite either the setting or hardware.
                return False
            output = corrected_color(
                preferences["source_color"],
                preferences["correction"] and effect == "breath")
            self._write_effect(effect, output)
            preferences["last_applied_boot_id"] = boot_id
            self._save_preferences(preferences)
            return True
