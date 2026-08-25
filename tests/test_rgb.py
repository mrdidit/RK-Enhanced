import json
import tempfile
import unittest
from pathlib import Path

import rgb


class RGBControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings_dir = self.root / "settings"
        self.helper = self.root / "ledcontrol"
        self.helper.write_text("#!/bin/sh\nexit 0\n")
        self.helper.chmod(0o755)
        self.analog_helper = self.root / "analog_sticks_ledcontrol"
        self.analog_helper.write_text("#!/bin/sh\nexit 0\n")
        self.analog_helper.chmod(0o755)
        self.led = self.root / "konkr:rgb:joysticks"
        self.led.mkdir()
        (self.led / "brightness").write_text("127\n")
        (self.led / "max_brightness").write_text("255\n")
        (self.led / "multi_intensity").write_text("255 255 255\n")
        (self.led / "effect").write_text("static\n")
        self.boot_id = self.root / "boot_id"
        self.boot_id.write_text("boot-a\n")
        self.values = {
            "led.color": "rgb",
            "analogsticks.led": "127 10 20 30 40 15 25",
        }
        self.reported_modes = "off\nrgb\nbattery\n"
        self.events = []
        self.remove_effect_on_rgb = False
        self.persisted_override = None
        self.fail_rgb_switch = False

    def tearDown(self):
        self.temporary.cleanup()

    def fake_run(self, command, check=True):
        command = tuple(command)
        self.events.append(("run", command, check))
        if command == (str(self.helper), "list"):
            return self.reported_modes
        mode = command[1]
        if mode == "rgb" and self.fail_rgb_switch:
            raise RuntimeError("native LED mode switch failed")
        self.values["led.color"] = mode
        if mode == "rgb":
            if self.remove_effect_on_rgb:
                (self.led / "effect").unlink()
                self.led.chmod(0o555)
            else:
                (self.led / "effect").write_text("static\n")
        return ""

    def get_setting(self, name, default=""):
        return self.values.get(name, default)

    def set_setting(self, name, value):
        self.events.append(("set", name, value))
        self.values[name] = (
            self.persisted_override
            if name == "analogsticks.led" and self.persisted_override is not None
            else value)

    def controller(self, led_path=None, helper=None):
        return rgb.RGBController(
            self.settings_dir,
            run=self.fake_run,
            get_setting=self.get_setting,
            set_setting=self.set_setting,
            led_control=helper or self.helper,
            analog_sticks_led_control=self.analog_helper,
            led_path=led_path or self.led,
            boot_id_path=self.boot_id,
        )

    def request(self, **changes):
        value = {
            "mode": "rgb",
            "effect": "static",
            "color": [255, 255, 255],
            "brightness": 127,
            "correction": False,
        }
        value.update(changes)
        return value

    def test_strict_runtime_capability_detects_known_complete_abi(self):
        capabilities = self.controller().capabilities()

        self.assertEqual(capabilities, {
            "available": True,
            "modes": ["off", "battery", "rgb"],
            "effects": ["static", "breath", "rainbow"],
            "shared_zone": True,
            "max_brightness": 255,
        })

    def test_generic_effect_attribute_is_not_treated_as_known_provider(self):
        generic = self.root / "vendor:rgb:lights"
        generic.mkdir()
        for name in ("brightness", "max_brightness", "multi_intensity", "effect"):
            (generic / name).write_text("255\n" if name == "max_brightness" else "static\n")

        capabilities = self.controller(led_path=generic).capabilities()

        self.assertFalse(capabilities["available"])
        self.assertEqual(capabilities["effects"], [])

    def test_missing_native_mode_or_attribute_hides_support(self):
        self.reported_modes = "off\nrgb\n"
        self.assertFalse(self.controller().capabilities()["available"])

        self.reported_modes = "off\nrgb\nbattery\n"
        (self.led / "multi_intensity").unlink()
        self.assertFalse(self.controller().capabilities()["available"])

    def test_missing_native_analog_helper_hides_support(self):
        self.analog_helper.unlink()

        self.assertFalse(self.controller().capabilities()["available"])

    def test_native_mode_list_accepts_one_line_whitespace_output(self):
        self.reported_modes = "  off   rgb\tbattery  \n"

        capabilities = self.controller().capabilities()

        self.assertTrue(capabilities["available"])
        self.assertEqual(capabilities["modes"], ["off", "battery", "rgb"])

    def test_static_only_provider_does_not_offer_animations(self):
        (self.led / "effect").unlink()

        capabilities = self.controller().capabilities()

        self.assertTrue(capabilities["available"])
        self.assertEqual(capabilities["effects"], ["static"])

    def test_native_seven_value_setting_combines_shared_colour(self):
        state = self.controller().get_state()

        self.assertTrue(state["supported"])
        self.assertTrue(state["valid"])
        self.assertEqual(state["mode"], "rgb")
        self.assertEqual(state["effect"], "static")
        self.assertEqual(state["color"], [40, 20, 30])
        self.assertEqual(state["brightness"], 127)
        self.assertFalse(state["correction"])

    def test_colour_correction_matches_contract(self):
        cases = (
            ((255, 255, 255), True, (255, 204, 204)),
            ((255, 0, 255), True, (255, 0, 204)),
            ((0, 255, 255), True, (0, 255, 255)),
            ((255, 0, 0), True, (255, 0, 0)),
            ((40, 101, 99), False, (40, 101, 99)),
        )
        for source, enabled, expected in cases:
            with self.subTest(source=source, enabled=enabled):
                self.assertEqual(rgb.corrected_color(source, enabled), expected)

    def test_static_persists_corrected_native_fallback_before_ledcontrol(self):
        state = self.controller().set_state(self.request(
            color=[255, 255, 255], brightness=64, correction=True))

        self.assertEqual(self.events, [
            ("run", (str(self.helper), "list"), False),
            ("set", "analogsticks.led", "64 255 204 204 255 204 204"),
            ("run", (str(self.helper), "rgb"), True),
        ])
        self.assertEqual((self.led / "effect").read_text(), "static\n")
        self.assertEqual(state["color"], [255, 255, 255])
        self.assertEqual(state["brightness"], 64)
        self.assertTrue(state["correction"])

    def test_breath_uses_corrected_output_but_returns_source_colour(self):
        state = self.controller().set_state(self.request(
            effect="breath", color=[200, 100, 50], correction=True))

        self.assertEqual(
            self.values["analogsticks.led"],
            "127 200 80 40 200 80 40")
        self.assertEqual(
            (self.led / "effect").read_text(), "breath 200 80 40\n")
        self.assertEqual(state["effect"], "breath")
        self.assertEqual(state["color"], [200, 100, 50])
        self.assertTrue(state["correction"])
        saved = json.loads(
            (self.settings_dir / rgb.PREFERENCES_FILE).read_text())
        self.assertEqual(saved["source_color"], [200, 100, 50])
        self.assertTrue(saved["animation_active"])
        self.assertEqual(saved["last_applied_boot_id"], "boot-a")

    def test_rainbow_ignores_colour_correction(self):
        state = self.controller().set_state(self.request(
            effect="rainbow", color=[200, 100, 50], correction=True))

        self.assertEqual(
            self.values["analogsticks.led"],
            "127 200 100 50 200 100 50")
        self.assertEqual((self.led / "effect").read_text(), "rainbow\n")
        self.assertEqual(state["color"], [200, 100, 50])

    def test_animation_failure_leaves_static_and_is_not_armed_for_startup(self):
        self.remove_effect_on_rgb = True

        with self.assertRaisesRegex(RuntimeError, "unable to apply"):
            self.controller().set_state(self.request(effect="breath"))

        self.assertEqual(
            self.values["analogsticks.led"],
            "127 255 255 255 255 255 255")
        self.assertEqual(self.values["led.color"], "rgb")
        saved = json.loads(
            (self.settings_dir / rgb.PREFERENCES_FILE).read_text())
        self.assertFalse(saved["animation_active"])
        self.assertEqual(saved["effect"], "breath")

    def test_persistence_mismatch_never_switches_native_rgb_mode(self):
        self.values["led.color"] = "battery"
        self.persisted_override = "64 1 2 3 1 2 3"

        with self.assertRaisesRegex(RuntimeError, "did not persist"):
            self.controller().set_state(self.request(
                color=[255, 255, 255], brightness=64, correction=True))

        self.assertEqual(self.values["led.color"], "battery")
        self.assertFalse(any(
            event[0] == "run" and event[1] == (str(self.helper), "rgb")
            for event in self.events))
        self.assertFalse(
            (self.settings_dir / rgb.PREFERENCES_FILE).exists())

    def test_failed_native_rgb_switch_preserves_source_metadata_safely(self):
        self.values["led.color"] = "off"
        self.fail_rgb_switch = True

        with self.assertRaisesRegex(RuntimeError, "mode switch failed"):
            self.controller().set_state(self.request(
                effect="breath", color=[200, 100, 50], brightness=64,
                correction=True))

        self.assertEqual(self.values["led.color"], "off")
        self.assertEqual(
            self.values["analogsticks.led"],
            "64 200 80 40 200 80 40")
        saved = json.loads(
            (self.settings_dir / rgb.PREFERENCES_FILE).read_text())
        self.assertEqual(saved["source_color"], [200, 100, 50])
        self.assertTrue(saved["correction"])
        self.assertEqual(saved["native_signature"], self.values["analogsticks.led"])
        self.assertFalse(saved["animation_active"])
        self.assertEqual(saved["last_applied_boot_id"], "")

        # The corrected native output must still map back to the original
        # source colour in the UI after the failed activation.
        state = self.controller().get_state()
        self.assertTrue(state["valid"])
        self.assertEqual(state["mode"], "off")
        self.assertEqual(state["color"], [200, 100, 50])
        self.assertTrue(state["correction"])

    def test_off_and_battery_use_only_native_ledcontrol(self):
        controller = self.controller()
        controller.set_state(self.request(mode="off", effect="breath"))
        controller.set_state(self.request(mode="battery", effect="breath"))

        mutations = [event for event in self.events
                     if event[0] == "set" or event[1][1] != "list"]
        self.assertEqual(mutations, [
            ("run", (str(self.helper), "off"), True),
            ("run", (str(self.helper), "battery"), True),
        ])
        saved = json.loads(
            (self.settings_dir / rgb.PREFERENCES_FILE).read_text())
        self.assertFalse(saved["animation_active"])

    def test_unsupported_or_invalid_requests_never_mutate_hardware(self):
        unsupported = self.controller(helper=self.root / "missing")
        with self.assertRaisesRegex(RuntimeError, "unsupported"):
            unsupported.set_state(self.request())
        self.assertEqual(self.events, [])

        controller = self.controller()
        invalid = (
            self.request(mode="invalid"),
            self.request(effect="scan"),
            self.request(color=[1, 2]),
            self.request(color=[1, -1, 2]),
            self.request(brightness=True),
            self.request(brightness=256),
            self.request(correction=1),
        )
        for request in invalid:
            self.events.clear()
            with self.assertRaises(ValueError):
                controller.set_state(request)
            self.assertFalse(any(event[0] == "set" for event in self.events))
            self.assertFalse(any(
                event[0] == "run" and event[1][1] != "list"
                for event in self.events))

    def test_animation_is_reapplied_once_on_a_new_boot(self):
        controller = self.controller()
        controller.set_state(self.request(
            effect="breath", color=[100, 50, 25], correction=True))
        # A same-boot Decky restart must not fight a later native Static
        # choice; the MCU retains an animation without process ownership.
        (self.led / "effect").write_text("static\n")
        self.assertFalse(controller.reapply_startup())
        self.assertEqual((self.led / "effect").read_text(), "static\n")

        self.boot_id.write_text("boot-b\n")
        self.events.clear()

        self.assertTrue(controller.reapply_startup())
        self.assertEqual(
            (self.led / "effect").read_text(), "breath 100 40 20\n")
        self.assertFalse(controller.reapply_startup())
        self.assertFalse(any(event[0] == "set" for event in self.events))
        self.assertFalse(any(
            event[0] == "run" and event[1][1] != "list"
            for event in self.events))

    def test_startup_reapply_respects_native_mode_and_external_colour(self):
        controller = self.controller()
        controller.set_state(self.request(effect="rainbow"))
        self.boot_id.write_text("boot-b\n")
        (self.led / "effect").write_text("static\n")

        self.values["led.color"] = "battery"
        self.assertFalse(controller.reapply_startup())
        self.values["led.color"] = "rgb"
        self.values["analogsticks.led"] = "127 1 2 3 1 2 3"
        self.assertFalse(controller.reapply_startup())
        self.assertEqual((self.led / "effect").read_text(), "static\n")

    def test_malformed_native_state_is_visible_but_not_fabricated(self):
        self.values["analogsticks.led"] = "not a valid setting"

        state = self.controller().get_state()

        self.assertTrue(state["supported"])
        self.assertFalse(state["valid"])
        self.assertEqual(state["mode"], "rgb")
        self.assertEqual(state["error"], "ROCKNIX RGB state is unavailable")

    def test_off_and_battery_validity_ignores_rgb_effect_and_colour(self):
        self.values["analogsticks.led"] = "malformed"
        (self.led / "effect").write_text("unknown animation\n")
        controller = self.controller()

        for mode in ("off", "battery"):
            with self.subTest(mode=mode):
                self.values["led.color"] = mode
                state = controller.get_state()
                self.assertTrue(state["supported"])
                self.assertTrue(state["valid"])
                self.assertEqual(state["mode"], mode)
                self.assertEqual(state["error"], "")


if __name__ == "__main__":
    unittest.main()
