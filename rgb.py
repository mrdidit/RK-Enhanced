"""Strict RK-Enhanced boundary for native ROCKNIX RGB controls."""

from contextlib import contextmanager
import fcntl
import json
import os
import re
import threading
from pathlib import Path
from tempfile import NamedTemporaryFile


LED_CONTROL = Path("/usr/bin/ledcontrol")
ANALOG_STICKS_LED_CONTROL = Path("/usr/bin/analog_sticks_ledcontrol")
LED_PATH = Path("/sys/class/leds/konkr:rgb:joysticks")
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
RGB_MODES = ("off", "battery", "rgb")
RGB_EFFECTS = ("static", "breath", "rainbow")
ANIMATED_EFFECTS = ("breath", "rainbow")
PREFERENCES_FILE = "rgb-settings.json"
PREFERENCES_VERSION = 1
DEFAULT_COLOR = (255, 255, 255)
DEFAULT_BRIGHTNESS = 255


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
    """Apply the optional KPFE red-channel compensation exactly once."""
    source = _source_color(color)
    if not isinstance(enabled, bool):
        raise ValueError("colour correction must be true or false")
    red, green, blue = source
    if not enabled or red == 0:
        return source
    # Integer round-to-nearest for x * 0.80. The source channels remain saved
    # separately, so an already corrected output is never corrected again.
    return red, (green * 4 + 2) // 5, (blue * 4 + 2) // 5


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


class RGBController:
    """Validate and serialize access to ROCKNIX's shared-zone RGB ABI.

    ``run`` must accept ``run(argv, check=True)`` and return stdout text.
    ``get_setting`` and ``set_setting`` use ROCKNIX setting names and values.
    The caller normally injects main.py's existing helpers.
    """

    def __init__(self, settings_dir, *, run, get_setting, set_setting,
                 led_control=LED_CONTROL, led_path=LED_PATH,
                 analog_sticks_led_control=ANALOG_STICKS_LED_CONTROL,
                 boot_id_path=BOOT_ID):
        self.settings_dir = Path(settings_dir)
        self.preferences_path = self.settings_dir / PREFERENCES_FILE
        self.lock_path = self.settings_dir / "rgb-control.lock"
        self.run = run
        self.get_setting = get_setting
        self.set_setting = set_setting
        self.led_control = Path(led_control)
        self.analog_sticks_led_control = Path(analog_sticks_led_control)
        self.led_path = Path(led_path)
        self.boot_id_path = Path(boot_id_path)
        self.thread_lock = threading.RLock()

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

    def capabilities(self):
        modes = self._native_modes()
        required = ("brightness", "max_brightness", "multi_intensity")
        # This first provider deliberately recognizes an ABI, not a product
        # name: only the known shared-zone LED class is safe for these writes.
        known_provider = self.led_path.name == "konkr:rgb:joysticks"
        available = bool(
            known_provider and modes == RGB_MODES and
            self.analog_sticks_led_control.is_file() and
            os.access(self.analog_sticks_led_control, os.X_OK) and
            all((self.led_path / name).is_file() for name in required) and
            _read(self.led_path / "max_brightness") == "255"
        )
        effects = ["static"]
        if available and (self.led_path / "effect").is_file():
            effects.extend(("breath", "rainbow"))
        return {
            "available": available,
            "modes": list(RGB_MODES) if available else [],
            "effects": effects if available else [],
            "shared_zone": available,
            "max_brightness": 255 if available else 0,
        }

    def _default_preferences(self):
        return {
            "version": PREFERENCES_VERSION,
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
                "version": PREFERENCES_VERSION,
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
        _atomic_json(self.preferences_path, preferences)

    def _effect_state(self, capabilities):
        if "breath" not in capabilities["effects"]:
            return "static", None, True
        parsed = _parse_effect(_read(self.led_path / "effect"))
        if parsed is None:
            return "static", None, False
        return parsed[0], parsed[1], True

    def _get_state_locked(self, capabilities=None):
        capabilities = capabilities or self.capabilities()
        preferences = self._load_preferences()
        base = {
            "supported": capabilities["available"],
            "valid": False,
            "modes": capabilities["modes"],
            "effects": capabilities["effects"],
            "shared_zone": capabilities["shared_zone"],
            "max_brightness": capabilities["max_brightness"],
            "mode": "unknown",
            "effect": preferences["effect"],
            "color": list(preferences["source_color"]),
            "brightness": preferences["brightness"],
            "correction": preferences["correction"],
            "error": "",
        }
        if not capabilities["available"]:
            return base

        mode = self.get_setting("led.color", "")
        if mode not in RGB_MODES:
            base["error"] = "ROCKNIX LED Color state is unavailable"
            return base

        native = _parse_native_config(self.get_setting("analogsticks.led", ""))
        effect, effect_color, effect_valid = self._effect_state(capabilities)
        base["mode"] = mode
        if mode == "rgb":
            base["effect"] = effect
        elif preferences["effect"] not in capabilities["effects"]:
            base["effect"] = "static"

        if native is not None:
            brightness, native_color, signature = native
            base["brightness"] = brightness
            if signature == preferences["native_signature"]:
                base["color"] = list(preferences["source_color"])
                base["correction"] = preferences["correction"]
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

    def _validated_request(self, request, capabilities):
        if not isinstance(request, dict):
            raise ValueError("RGB request must be an object")
        mode = request.get("mode")
        effect = request.get("effect")
        if mode not in RGB_MODES:
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

    def _preferences(self, source, brightness, correction, effect,
                     animation_active, signature, boot_id=""):
        return {
            "version": PREFERENCES_VERSION,
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

    def set_state(self, request):
        with self.thread_lock, _exclusive_lock(self.lock_path):
            capabilities = self.capabilities()
            if not capabilities["available"]:
                raise RuntimeError("RGB control is unsupported on this device")
            mode, effect, source, brightness, correction = (
                self._validated_request(request, capabilities))

            if mode in ("off", "battery"):
                self.run([str(self.led_control), mode])
                previous = self._load_preferences()
                self._save_preferences(self._preferences(
                    source, brightness, correction, effect, False,
                    previous["native_signature"]))
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
                    source, brightness, correction, effect, False, native))
                raise

            if effect in ANIMATED_EFFECTS:
                try:
                    self._write_effect(effect, output)
                except RuntimeError:
                    # ledcontrol rgb has already left the rings in the safe,
                    # natively persisted Static state. Never arm a failed
                    # animation for startup reapplication.
                    self._save_preferences(self._preferences(
                        source, brightness, correction, effect, False, native))
                    raise

            boot_id = _read(self.boot_id_path) if effect in ANIMATED_EFFECTS else ""
            self._save_preferences(self._preferences(
                source, brightness, correction, effect,
                effect in ANIMATED_EFFECTS, native, boot_id))
            return self._get_state_locked(capabilities)

    def reapply_startup(self):
        """Reapply a persisted animation once per boot, without polling."""
        with self.thread_lock, _exclusive_lock(self.lock_path):
            capabilities = self.capabilities()
            if not capabilities["available"]:
                return False
            preferences = self._load_preferences()
            effect = preferences["effect"]
            if (not preferences["animation_active"] or
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
