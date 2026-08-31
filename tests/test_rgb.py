import errno
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        self.setting_reads = []
        self.remove_effect_on_rgb = False
        self.persisted_override = None
        self.fail_persist = False
        self.fail_rgb_switch = False
        self.fail_analog_helper = False
        self.analog_failure_replacement = None

    def tearDown(self):
        self.temporary.cleanup()

    def fake_run(self, command, check=True):
        command = tuple(command)
        self.events.append(("run", command, check))
        if command == (str(self.helper), "list"):
            return self.reported_modes
        if command and command[0] == str(self.analog_helper):
            if self.fail_analog_helper:
                if self.analog_failure_replacement is not None:
                    self.values["analogsticks.led"] = (
                        self.analog_failure_replacement)
                raise RuntimeError("analogue-stick helper failed")
            return ""
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
        self.setting_reads.append(name)
        return self.values.get(name, default)

    def set_setting(self, name, value):
        self.events.append(("set", name, value))
        if self.fail_persist:
            raise RuntimeError("setting persistence failed")
        self.values[name] = (
            self.persisted_override
            if name == "analogsticks.led" and self.persisted_override is not None
            else value)

    def controller(self, led_path=None, helper=None, runtime_flag=""):
        return rgb.RGBController(
            self.settings_dir,
            run=self.fake_run,
            get_setting=self.get_setting,
            set_setting=self.set_setting,
            get_runtime_capability=lambda name: (
                runtime_flag
                if name == rgb.ANALOG_STICKS_CAPABILITY else ""),
            led_control=helper or self.helper,
            analog_sticks_led_control=self.analog_helper,
            led_path=led_path or self.led,
            boot_id_path=self.boot_id,
        )

    def generic_controller(self, runtime_flag="true"):
        return self.controller(
            led_path=self.root / "generic-analogue-stick-provider",
            runtime_flag=runtime_flag,
        )

    def request(self, **changes):
        provider = changes.pop("provider", "sysfs-effects")
        if provider == "analog-static":
            revision = rgb._state_revision(
                provider, self.values.get("analogsticks.led", ""))
        else:
            revision = rgb._state_revision(
                provider,
                self.values.get("led.color", ""),
                self.values.get("analogsticks.led", ""),
                rgb._read(self.led / "effect"),
            )
        value = {
            "provider": provider,
            "revision": revision,
            "mode": "rgb",
            "effect": "static",
            "color": [255, 255, 255],
            "brightness": 127,
            "correction": False,
        }
        value.update(changes)
        return value

    def generic_request(self, **changes):
        changes.setdefault("provider", "analog-static")
        return self.request(**changes)

    def test_strict_runtime_capability_detects_known_complete_abi(self):
        capabilities = self.controller().capabilities()

        self.assertEqual(capabilities, {
            "available": True,
            "provider": "sysfs-effects",
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

    def test_strict_sysfs_provider_wins_when_generic_flag_is_also_true(self):
        capabilities = self.controller(runtime_flag="true").capabilities()

        self.assertEqual(capabilities["provider"], "sysfs-effects")
        self.assertEqual(capabilities["effects"], ["static", "breath", "rainbow"])

    def test_generic_static_provider_uses_only_runtime_capability_and_saved_state(self):
        self.values["led.color"] = "battery"
        self.values["analogsticks.led"] = "255 255 25 0 0 0 0"

        capabilities = self.generic_controller().capabilities()
        state = self.generic_controller().get_state()

        self.assertEqual(capabilities, {
            "available": True,
            "provider": "analog-static",
            "modes": ["off", "rgb"],
            "effects": ["static"],
            "shared_zone": True,
            "max_brightness": 255,
        })
        self.assertTrue(state["supported"])
        self.assertTrue(state["valid"])
        self.assertEqual(state["provider"], "analog-static")
        self.assertEqual(state["mode"], "rgb")
        self.assertEqual(state["effect"], "static")
        self.assertEqual(state["color"], [255, 25, 0])
        self.assertEqual(state["brightness"], 255)
        self.assertFalse(state["correction"])
        # Detection and status are read-only: neither public helper is used.
        self.assertEqual(self.events, [])

    def test_generic_provider_rejects_missing_or_nonexact_capability(self):
        for reported in ("", "false", "True", "1"):
            with self.subTest(reported=reported):
                capabilities = self.generic_controller(reported).capabilities()
                self.assertFalse(capabilities["available"])
                self.assertEqual(capabilities["provider"], "none")
                self.assertEqual(capabilities["modes"], [])
                self.assertEqual(self.events, [])

    def test_generic_provider_requires_executable_helper_and_valid_seven_fields(self):
        self.analog_helper.chmod(0o644)
        self.assertFalse(self.generic_controller().capabilities()["available"])
        self.analog_helper.chmod(0o755)

        invalid = (
            "", "255 1 2 3 4 5", "255 1 2 3 4 5 6 7",
            "255 1 2 3 4 5 -1", "255 1 2 3 4 5 256", "one 1 2 3 4 5 6",
        )
        for value in invalid:
            with self.subTest(value=value):
                self.values["analogsticks.led"] = value
                capabilities = self.generic_controller().capabilities()
                self.assertFalse(capabilities["available"])
                self.assertEqual(capabilities["provider"], "none")
        self.assertEqual(self.events, [])

    def test_generic_save_persists_then_invokes_public_helper_with_exact_values(self):
        self.values["led.color"] = "battery"
        controller = self.generic_controller()

        state = controller.set_state(self.generic_request(
            color=[100, 50, 25], brightness=64, correction=True))

        expected = "64 100 40 20 100 40 20"
        self.assertEqual(self.events, [
            ("set", "analogsticks.led", expected),
            ("run", (
                str(self.analog_helper), "64", "100", "40", "20",
                "100", "40", "20",
            ), True),
        ])
        self.assertEqual(self.values["analogsticks.led"], expected)
        self.assertEqual(self.values["led.color"], "battery")
        self.assertEqual(state["provider"], "analog-static")
        self.assertEqual(state["mode"], "rgb")
        self.assertEqual(state["color"], [100, 50, 25])
        self.assertEqual(state["brightness"], 64)
        self.assertTrue(state["correction"])
        self.assertFalse(state["zones_differ"])
        self.assertEqual(len(state["revision"]), 64)
        saved = json.loads(
            (self.settings_dir / rgb.PREFERENCES_FILE).read_text())
        self.assertEqual(saved["version"], 2)
        self.assertEqual(saved["provider"], "analog-static")

    def test_generic_off_and_on_restore_last_nonzero_brightness(self):
        controller = self.generic_controller()
        controller.set_state(self.generic_request(
            color=[20, 30, 40], brightness=42, correction=False))
        self.events.clear()

        off = controller.set_state(self.generic_request(
            mode="off", color=[20, 30, 40], brightness=0))

        self.assertEqual(self.values["analogsticks.led"], "0 20 30 40 20 30 40")
        self.assertEqual(off["mode"], "off")
        self.assertEqual(off["brightness"], 42)
        self.assertEqual(self.events[-1], (
            "run", (
                str(self.analog_helper), "0", "20", "30", "40",
                "20", "30", "40",
            ), True))
        self.events.clear()

        enabled = controller.set_state(self.generic_request(
            mode="rgb", color=[20, 30, 40], brightness=0))

        self.assertEqual(
            self.values["analogsticks.led"], "42 20 30 40 20 30 40")
        self.assertEqual(enabled["mode"], "rgb")
        self.assertEqual(enabled["brightness"], 42)
        self.assertEqual(self.values["led.color"], "rgb")

    def test_generic_persistence_mismatch_or_failure_never_calls_helper(self):
        self.persisted_override = "64 1 2 3 1 2 3"
        with self.assertRaisesRegex(RuntimeError, "did not persist"):
            self.generic_controller().set_state(self.generic_request(
                color=[100, 50, 25], brightness=64, correction=True))
        self.assertEqual(self.events, [
            ("set", "analogsticks.led", "64 100 40 20 100 40 20"),
        ])
        self.assertFalse(any(event[0] == "run" for event in self.events))
        self.assertFalse((self.settings_dir / rgb.PREFERENCES_FILE).exists())

        self.values["analogsticks.led"] = "127 10 20 30 40 15 25"
        self.persisted_override = None
        self.fail_persist = True
        self.events.clear()
        with self.assertRaisesRegex(RuntimeError, "persistence failed"):
            self.generic_controller().set_state(self.generic_request())
        self.assertEqual(len(self.events), 1)
        self.assertTrue(all(event[0] == "set" for event in self.events))
        self.assertFalse(any(event[0] == "run" for event in self.events))

    def test_generic_helper_failure_does_not_save_source_metadata(self):
        self.fail_analog_helper = True

        with self.assertRaisesRegex(RuntimeError, "helper failed"):
            self.generic_controller().set_state(self.generic_request(
                color=[100, 50, 25], brightness=64, correction=True))

        self.assertEqual(
            self.values["analogsticks.led"], "127 10 20 30 40 15 25")
        self.assertFalse((self.settings_dir / rgb.PREFERENCES_FILE).exists())
        self.assertEqual(self.values["led.color"], "rgb")
        self.assertEqual([event[0] for event in self.events], [
            "set", "run", "set", "run",
        ])

    def test_generic_preferences_failure_rolls_back_setting_and_hardware(self):
        controller = self.generic_controller()

        def fail_preferences(_preferences):
            raise OSError("preferences unavailable")

        controller._save_preferences = fail_preferences
        with self.assertRaisesRegex(OSError, "preferences unavailable"):
            controller.set_state(self.generic_request(
                color=[100, 50, 25], brightness=64, correction=True))

        self.assertEqual(
            self.values["analogsticks.led"], "127 10 20 30 40 15 25")
        self.assertEqual(self.events, [
            ("set", "analogsticks.led", "64 100 40 20 100 40 20"),
            ("run", (
                str(self.analog_helper), "64", "100", "40", "20",
                "100", "40", "20",
            ), True),
            ("set", "analogsticks.led", "127 10 20 30 40 15 25"),
            ("run", (
                str(self.analog_helper), "127", "10", "20", "30",
                "40", "15", "25",
            ), True),
        ])

    def test_generic_failure_preserves_and_reapplies_newer_external_value(self):
        third_party = "200 9 8 7 6 5 4"
        self.fail_analog_helper = True
        self.analog_failure_replacement = third_party

        with self.assertRaisesRegex(RuntimeError, "helper failed"):
            self.generic_controller().set_state(self.generic_request(
                color=[100, 50, 25], brightness=64, correction=True))

        self.assertEqual(self.values["analogsticks.led"], third_party)
        self.assertEqual([event[0] for event in self.events], [
            "set", "run", "run",
        ])
        self.assertEqual(self.events[-1], (
            "run", (
                str(self.analog_helper), "200", "9", "8", "7",
                "6", "5", "4",
            ), True))

    def test_generic_provider_never_offers_or_reapplies_animations(self):
        controller = self.generic_controller()
        self.assertEqual(controller.capabilities()["effects"], ["static"])
        self.events.clear()

        for effect in ("breath", "rainbow"):
            with self.subTest(effect=effect):
                with self.assertRaisesRegex(ValueError, "unsupported RGB effect"):
                    controller.set_state(self.generic_request(effect=effect))
        self.setting_reads.clear()
        self.assertFalse(controller.reapply_startup())
        self.assertEqual(self.events, [])
        self.assertEqual(self.setting_reads, [])

    def test_generic_read_uses_right_colour_and_reports_unequal_zones(self):
        self.values["analogsticks.led"] = "255 10 20 30 200 210 220"
        controller = self.generic_controller()

        state = controller.get_state()

        self.assertTrue(state["valid"])
        self.assertEqual(state["color"], [10, 20, 30])
        self.assertTrue(state["zones_differ"])
        self.assertEqual(self.events, [])

        applied = controller.set_state(self.generic_request(
            color=state["color"], brightness=state["brightness"]))

        self.assertEqual(
            self.values["analogsticks.led"], "255 10 20 30 10 20 30")
        self.assertFalse(applied["zones_differ"])

    def test_stale_or_missing_provider_is_rejected_before_any_mutation(self):
        cases = (
            (self.controller(), self.request(provider="analog-static")),
            (self.generic_controller(), self.generic_request(provider="sysfs-effects")),
            (self.generic_controller(), {
                key: value for key, value in self.generic_request().items()
                if key != "provider"
            }),
        )
        for controller, request in cases:
            with self.subTest(provider=request.get("provider")):
                self.events.clear()
                with self.assertRaisesRegex(ValueError, "provider changed"):
                    controller.set_state(request)
                self.assertFalse(any(event[0] == "set" for event in self.events))
                self.assertFalse(any(
                    event[0] == "run" and event[1][1:] != ("list",)
                    for event in self.events))

    def test_stale_native_revision_is_rejected_before_any_mutation(self):
        controller = self.generic_controller()
        request = self.generic_request(color=[1, 2, 3])
        self.values["analogsticks.led"] = "127 9 8 7 9 8 7"
        self.events.clear()

        with self.assertRaisesRegex(ValueError, "state changed"):
            controller.set_state(request)

        self.assertEqual(self.values["analogsticks.led"], "127 9 8 7 9 8 7")
        self.assertEqual(self.events, [])

    def test_legacy_untagged_preferences_belong_only_to_sysfs_provider(self):
        self.settings_dir.mkdir(parents=True)
        signature = self.values["analogsticks.led"]
        (self.settings_dir / rgb.PREFERENCES_FILE).write_text(json.dumps({
            "version": 1,
            "source_color": [1, 2, 3],
            "brightness": 99,
            "correction": True,
            "effect": "static",
            "animation_active": False,
            "native_signature": signature,
            "last_applied_boot_id": "",
        }))

        generic = self.generic_controller().get_state()
        strict = self.controller().get_state()

        self.assertEqual(generic["color"], [10, 20, 30])
        self.assertFalse(generic["correction"])
        self.assertEqual(strict["color"], [1, 2, 3])
        self.assertTrue(strict["correction"])

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


class HtrTestController(rgb.RGBController):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.htr_writes = []
        self.fail_on_write = None
        self.failure_mutation = None

    def _write_htr3212_brightness(self, path, value):
        path = Path(path)
        self.htr_writes.append((path.parent.name, value))
        if (self.fail_on_write is not None and
                len(self.htr_writes) == self.fail_on_write):
            if self.failure_mutation is not None:
                self.failure_mutation()
            raise RuntimeError("injected HTR3212 write failure")
        path.write_text(f"{value}\n")


class Htr3212RGBControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings_dir = self.root / "settings"
        self.leds = self.root / "class-leds"
        self.leds.mkdir()
        self.devices = self.root / "devices"
        self.drivers = self.root / "drivers"
        self.driver = self.drivers / "htr3212"
        self.driver.mkdir(parents=True)
        self.controllers = {
            "l": self.devices / "1-003c",
            "r": self.devices / "0-003c",
        }
        for controller in self.controllers.values():
            (controller / "leds").mkdir(parents=True)
            (controller / "driver").symlink_to(self.driver, target_is_directory=True)
        for side in ("l", "r"):
            for color in ("r", "g", "b"):
                for zone in range(1, 5):
                    name = f"{side}:{color}{zone}"
                    backing = self.controllers[side] / "leds" / name
                    backing.mkdir()
                    (backing / "brightness").write_text("0\n")
                    (backing / "brightness").chmod(0o644)
                    (backing / "max_brightness").write_text("255\n")
                    (backing / "max_brightness").chmod(0o444)
                    (backing / "device").symlink_to(
                        self.controllers[side], target_is_directory=True)
                    (self.leds / name).symlink_to(backing, target_is_directory=True)
        self.evo_root = self.root / "evo-leds"
        self.evo_root.mkdir()
        self.boot_id = self.root / "boot_id"
        self.boot_id.write_text("boot-a\n")
        self.helper = self.root / "analog_sticks_ledcontrol"
        self.helper.write_text("#!/bin/sh\nexit 0\n")
        self.helper.chmod(0o755)
        self.led_control = self.root / "ledcontrol"
        self.led_control.write_text("#!/bin/sh\nexit 0\n")
        self.led_control.chmod(0o755)
        self.values = {
            "led.color": "rgb",
            "analogsticks.led": "255 1 2 3 1 2 3",
        }
        self.events = []

    def tearDown(self):
        self.temporary.cleanup()

    def controller(self, *, quirk=rgb.HTR3212_ODIN3_QUIRK):
        return HtrTestController(
            self.settings_dir,
            run=lambda command, check=True: self.events.append(
                ("run", tuple(command), check)) or "off battery rgb",
            get_setting=lambda name, default="": self.values.get(name, default),
            set_setting=lambda name, value: self.events.append(
                ("set", name, value)),
            get_runtime_capability=lambda name: (
                quirk if name == rgb.QUIRK_DEVICE_CAPABILITY else
                "true" if name == rgb.ANALOG_STICKS_CAPABILITY else ""),
            led_control=self.led_control,
            analog_sticks_led_control=self.helper,
            led_path=self.root / "missing-shared-provider",
            boot_id_path=self.boot_id,
            evo_leds_root=self.evo_root,
            htr_leds_root=self.leds,
        )

    @staticmethod
    def request(state, *, mode=None, lighting=None):
        return {
            "provider": "htr3212-static",
            "revision": state["revision"],
            "mode": mode or state["mode"],
            "lighting": lighting or state["lighting"],
        }

    def brightness(self, name):
        return int((self.leds / name / "brightness").read_text())

    def write_htr_snapshot(self, snapshot):
        for (side, zone), values in zip(
                rgb.HTR3212_ZONE_CHANNELS, snapshot):
            for color, value in zip(("r", "g", "b"), values):
                (self.leds / f"{side}:{color}{zone}" / "brightness").write_text(
                    f"{value}\n")

    def htr_snapshot(self):
        return tuple(
            tuple(self.brightness(f"{side}:{color}{zone}")
                  for color in ("r", "g", "b"))
            for side, zone in rgb.HTR3212_ZONE_CHANNELS
        )

    def saved_active_htr_state(self, controller):
        state = controller.get_state()
        lighting = state["lighting"]
        lighting["layout_mode"] = "quadrants"
        for index, zone in enumerate(lighting["zones"]):
            zone["color"] = [20 + index, 40 + index, 60 + index]
            zone["brightness"] = 255
        applied = controller.set_state(self.request(
            state, mode="rgb", lighting=lighting))
        return applied, controller._htr3212_snapshot_from_lighting(lighting)

    def test_exact_odin3_abi_is_read_only_and_wins_over_generic(self):
        controller = self.controller()

        capabilities = controller.capabilities()
        state = controller.get_state()

        self.assertEqual(capabilities, {
            "available": True,
            "provider": "htr3212-static",
            "modes": ["off", "rgb"],
            "effects": ["static"],
            "shared_zone": False,
            "max_brightness": 255,
            "zone_ids": list(rgb.HTR3212_ZONE_INDEX),
            "layout_modes": ["both", "per-stick", "quadrants"],
            "error": "",
        })
        self.assertTrue(state["supported"])
        self.assertTrue(state["valid"])
        self.assertEqual(state["mode"], "off")
        self.assertEqual(state["provider"], "htr3212-static")
        self.assertEqual(state["effects"], ["static"])
        self.assertEqual([zone["id"] for zone in state["lighting"]["zones"]],
                         list(rgb.HTR3212_ZONE_INDEX))
        self.assertEqual(controller.htr_writes, [])
        self.assertEqual(self.events, [])
        json.dumps(state)

    def test_unverified_device_or_partial_odin_abi_is_not_used(self):
        self.assertEqual(
            self.controller(quirk="AYN Thor").capabilities()["provider"],
            "analog-static")

        (self.leds / "l:r1").unlink()
        capabilities = self.controller().capabilities()
        self.assertEqual(capabilities["provider"], "htr3212-static")
        self.assertFalse(capabilities["available"])
        self.assertIn("incomplete", capabilities["error"])

    def test_exact_odin_with_absent_htr_abi_never_falls_through(self):
        for path in tuple(self.leds.iterdir()):
            path.unlink()
        controller = self.controller()
        controller._sysfs_effects_available = mock.Mock(return_value=True)
        controller._load_preferences = mock.Mock(return_value={
            "provider": rgb.PROVIDER_SYSFS_EFFECTS,
            "animation_active": True,
            "effect": "rainbow",
            "last_applied_boot_id": "boot-old",
        })
        controller._write_effect = mock.Mock()

        capabilities = controller.capabilities()
        self.assertEqual(capabilities["provider"], rgb.PROVIDER_HTR3212_STATIC)
        self.assertFalse(capabilities["available"])
        self.assertIn("unavailable", capabilities["error"])
        self.assertFalse(controller.reapply_startup())
        controller._sysfs_effects_available.assert_not_called()
        controller._load_preferences.assert_not_called()
        controller._write_effect.assert_not_called()
        self.assertEqual(controller.htr_writes, [])

    def test_duplicate_channel_alias_and_wrong_driver_fail_closed(self):
        original = self.leds / "l:r1"
        duplicate_target = (self.leds / "l:g1").resolve()
        original.unlink()
        original.symlink_to(duplicate_target, target_is_directory=True)
        capabilities = self.controller().capabilities()
        self.assertFalse(capabilities["available"])
        self.assertIn("ambiguous", capabilities["error"])

        original.unlink()
        original.symlink_to(
            self.controllers["l"] / "leds" / "l:r1",
            target_is_directory=True)
        wrong = self.drivers / "wrong-driver"
        wrong.mkdir()
        (self.controllers["l"] / "driver").unlink()
        (self.controllers["l"] / "driver").symlink_to(
            wrong, target_is_directory=True)
        capabilities = self.controller().capabilities()
        self.assertFalse(capabilities["available"])
        self.assertIn("unexpected driver", capabilities["error"])

    def test_gamma_corrects_level_only(self):
        self.assertEqual(rgb._htr3212_pwm(128, 255), 128)
        self.assertEqual(rgb._htr3212_pwm(255, 128), 56)
        self.assertEqual(rgb._htr3212_pwm(100, 128), 22)

    def test_revision_covers_every_native_channel(self):
        controller = self.controller()
        baseline = controller.get_state()["revision"]
        for name in ("l:r4", "l:g1", "r:b4"):
            with self.subTest(name=name):
                path = self.leds / name / "brightness"
                path.write_text("1\n")
                changed = controller.get_state()["revision"]
                self.assertNotEqual(changed, baseline)
                self.assertEqual(len(changed), 64)
                path.write_text("0\n")

    def test_state_requires_two_identical_complete_snapshots(self):
        controller = self.controller()
        original = controller._read_htr3212_snapshot_once
        calls = 0

        def changing(channels):
            nonlocal calls
            snapshot = [list(zone) for zone in original(channels)]
            calls += 1
            if calls % 2 == 0:
                snapshot[0][0] = 1
            return tuple(tuple(zone) for zone in snapshot)

        controller._read_htr3212_snapshot_once = changing
        state = controller.get_state()
        self.assertTrue(state["supported"])
        self.assertFalse(state["valid"])
        self.assertIn("unstable", state["error"])
        self.assertEqual(controller.htr_writes, [])

    def test_static_apply_uses_hardware_mapped_quadrants(self):
        controller = self.controller()
        state = controller.get_state()
        lighting = state["lighting"]
        lighting["layout_mode"] = "quadrants"
        for index, zone in enumerate(lighting["zones"]):
            zone["color"] = [10 + index, 30 + index, 50 + index]
            zone["brightness"] = 255

        applied = controller.set_state(self.request(state, mode="rgb", lighting=lighting))

        expected_nodes = (
            "l:r4", "l:r1", "l:r2", "l:r3",
            "r:r1", "r:r2", "r:r3", "r:r4",
        )
        for index, red_node in enumerate(expected_nodes):
            prefix, red = red_node.split(":")
            zone_number = red[1:]
            self.assertEqual(self.brightness(red_node), 10 + index)
            self.assertEqual(self.brightness(f"{prefix}:g{zone_number}"), 30 + index)
            self.assertEqual(self.brightness(f"{prefix}:b{zone_number}"), 50 + index)
        self.assertEqual(applied["lighting"]["layout_mode"], "quadrants")
        self.assertEqual(applied["lighting"]["zones"], lighting["zones"])
        self.assertEqual(len(controller.htr_writes), 24)
        self.assertEqual(self.events, [])

    def test_gamma_preserves_selected_rgb_ratios(self):
        controller = self.controller()
        state = controller.get_state()
        lighting = state["lighting"]
        for zone in lighting["zones"]:
            zone["color"] = [100, 50, 25]
            zone["brightness"] = 128

        applied = controller.set_state(self.request(
            state, mode="rgb", lighting=lighting))

        self.assertEqual((
            self.brightness("l:r4"), self.brightness("l:g4"),
            self.brightness("l:b4")), (22, 11, 5))
        self.assertEqual(applied["lighting"]["zones"][0]["color"],
                         [100, 50, 25])
        again = controller.set_state(self.request(applied))
        self.assertEqual(again["lighting"]["zones"][0]["color"],
                         [100, 50, 25])
        self.assertEqual(len(controller.htr_writes), 24)
        self.assertNotIn("correction", applied)
        self.assertNotIn(
            "correction",
            json.loads(controller.htr_preferences_path.read_text()))

    def test_v1_preferences_migrate_only_when_correction_was_off(self):
        controller = self.controller()
        lighting = controller._default_htr3212_lighting()
        lighting["layout_mode"] = "per-stick"
        candidate = {
            "version": 1,
            "provider": "htr3212-static",
            "lighting": lighting,
            "resume_lighting": lighting,
            "correction": False,
            "native_signature": " ".join("0" for _index in range(24)),
            "last_applied_boot_id": "boot-a",
        }
        controller.htr_preferences_path.parent.mkdir(parents=True)
        controller.htr_preferences_path.write_text(json.dumps(candidate))

        migrated = controller.get_state()
        self.assertEqual(
            migrated["resume_lighting"]["layout_mode"], "per-stick")
        self.assertEqual(
            controller._load_htr3212_preferences()["mode"], "off")
        self.assertNotIn("correction", migrated)

        candidate["correction"] = True
        controller.htr_preferences_path.write_text(json.dumps(candidate))
        rejected = controller.get_state()
        self.assertIsNone(rejected["resume_lighting"])
        self.assertNotIn("correction", rejected)

    def test_v2_active_preferences_restore_and_migrate_on_new_boot(self):
        controller = self.controller()
        _active, target = self.saved_active_htr_state(controller)
        legacy = json.loads(controller.htr_preferences_path.read_text())
        legacy["version"] = 2
        legacy.pop("mode")
        controller.htr_preferences_path.write_text(json.dumps(legacy))
        self.write_htr_snapshot(tuple((0, 0, 0) for _zone in target))
        self.boot_id.write_text("boot-b\n")
        restarted = self.controller()

        self.assertEqual(
            restarted.reapply_startup(), rgb.PROVIDER_HTR3212_STATIC)
        self.assertEqual(self.htr_snapshot(), target)
        migrated = json.loads(restarted.htr_preferences_path.read_text())
        self.assertEqual(migrated["version"], rgb.HTR3212_PREFERENCE_VERSION)
        self.assertEqual(migrated["mode"], "rgb")
        self.assertEqual(migrated["last_applied_boot_id"], "boot-b")

    def test_off_then_backend_only_on_restores_complete_saved_layout(self):
        controller = self.controller()
        state = controller.get_state()
        lighting = state["lighting"]
        lighting["layout_mode"] = "per-stick"
        for index, zone in enumerate(lighting["zones"]):
            zone["color"] = [200 if index < 4 else 20, 10, 5]
            zone["brightness"] = 180 if index < 4 else 90
        active = controller.set_state(self.request(
            state, mode="rgb", lighting=lighting))
        off = controller.set_state(self.request(active, mode="off"))
        self.assertEqual(off["mode"], "off")
        self.assertEqual(off["resume_lighting"]["layout_mode"], "per-stick")
        self.assertTrue(all(self.brightness(name) == 0
            for name in (f"{side}:{color}{zone}"
                for side in ("l", "r") for color in ("r", "g", "b")
                for zone in range(1, 5))))

        raw_zero_lighting = off["lighting"]
        restored = controller.set_state(self.request(
            off, mode="rgb", lighting=raw_zero_lighting))
        self.assertEqual(restored["mode"], "rgb")
        self.assertEqual(restored["lighting"]["layout_mode"], "per-stick")

    def test_stale_revision_rejects_without_writes(self):
        controller = self.controller()
        state = controller.get_state()
        request = self.request(state, mode="rgb")
        request["revision"] = "stale"
        with self.assertRaisesRegex(ValueError, "state changed"):
            controller.set_state(request)
        self.assertEqual(controller.htr_writes, [])

    def test_invalid_htr_requests_never_write(self):
        controller = self.controller()
        state = controller.get_state()
        base = self.request(state, mode="rgb")
        invalid = []
        for key, value in (
                ("provider", "pocket-evo-v3"),
                ("mode", "battery")):
            request = dict(base)
            request[key] = value
            invalid.append(request)
        bad_effect = json.loads(json.dumps(base))
        bad_effect["lighting"]["effect"] = "breath"
        invalid.append(bad_effect)
        bad_zones = json.loads(json.dumps(base))
        bad_zones["lighting"]["zones"][0]["id"] = "left-0"
        invalid.append(bad_zones)

        for request in invalid:
            with self.subTest(request=request):
                with self.assertRaises(ValueError):
                    controller.set_state(request)
                self.assertEqual(controller.htr_writes, [])

    def test_immediate_write_failure_rolls_back_unchanged_cached_prefix(self):
        controller = self.controller()
        state = controller.get_state()
        lighting = state["lighting"]
        for zone in lighting["zones"]:
            zone["color"] = [100, 80, 60]
            zone["brightness"] = 255
        controller.fail_on_write = 3

        with self.assertRaisesRegex(RuntimeError, "injected"):
            controller.set_state(self.request(
                state, mode="rgb", lighting=lighting))

        self.assertEqual(self.brightness("l:r4"), 0)
        self.assertEqual(self.brightness("l:g4"), 0)
        self.assertFalse(controller.htr_preferences_path.exists())

        self.boot_id.write_text("boot-b\n")
        restarted = self.controller()
        self.assertFalse(restarted.reapply_startup())
        self.assertEqual(restarted.htr_writes, [])

    def test_observed_external_divergence_skips_cached_rollback(self):
        controller = self.controller()
        state = controller.get_state()
        lighting = state["lighting"]
        for zone in lighting["zones"]:
            zone["color"] = [100, 80, 60]
            zone["brightness"] = 255
        controller.fail_on_write = 2
        controller.failure_mutation = lambda: (
            self.leds / "r:b4" / "brightness").write_text("77\n")

        with self.assertRaisesRegex(RuntimeError, "injected"):
            controller.set_state(self.request(
                state, mode="rgb", lighting=lighting))

        self.assertEqual(self.brightness("l:r4"), 100)
        self.assertEqual(self.brightness("r:b4"), 77)

    def test_future_channel_change_observed_before_next_write_aborts_apply(self):
        controller = self.controller()
        state = controller.get_state()
        lighting = state["lighting"]
        for zone in lighting["zones"]:
            zone["color"] = [100, 80, 60]
            zone["brightness"] = 255
        original_write = controller._write_htr3212_brightness
        writes = 0

        def concurrent_write(path, value):
            nonlocal writes
            original_write(path, value)
            writes += 1
            if writes == 1:
                (self.leds / "r:b4" / "brightness").write_text("77\n")

        controller._write_htr3212_brightness = concurrent_write
        with self.assertRaisesRegex(RuntimeError, "changed during apply"):
            controller.set_state(self.request(
                state, mode="rgb", lighting=lighting))

        self.assertEqual(self.brightness("l:r4"), 100)
        self.assertEqual(self.brightness("l:g4"), 0)
        self.assertEqual(self.brightness("r:b4"), 77)
        self.assertEqual(len(controller.htr_writes), 1)
        self.assertFalse(controller.htr_preferences_path.exists())

    def test_preference_failure_rolls_back_cached_state(self):
        controller = self.controller()
        state = controller.get_state()
        lighting = state["lighting"]
        for zone in lighting["zones"]:
            zone["color"] = [100, 80, 60]
            zone["brightness"] = 255
        controller._save_htr3212_preferences = mock.Mock(
            side_effect=OSError("preferences unavailable"))

        with self.assertRaisesRegex(OSError, "preferences unavailable"):
            controller.set_state(self.request(
                state, mode="rgb", lighting=lighting))

        self.assertTrue(all(self.brightness(name) == 0
            for name in (f"{side}:{color}{zone}"
                for side in ("l", "r") for color in ("r", "g", "b")
                for zone in range(1, 5))))

    def test_startup_restores_complete_htr3212_state_once_on_new_boot(self):
        controller = self.controller()
        _active, target = self.saved_active_htr_state(controller)
        self.write_htr_snapshot(tuple((0, 0, 0) for _zone in target))
        self.boot_id.write_text("boot-b\n")
        restarted = self.controller()

        self.assertEqual(
            restarted.reapply_startup(), rgb.PROVIDER_HTR3212_STATIC)
        self.assertEqual(self.htr_snapshot(), target)
        self.assertEqual(len(restarted.htr_writes), 24)
        saved = json.loads(restarted.htr_preferences_path.read_text())
        self.assertEqual(saved["mode"], "rgb")
        self.assertEqual(saved["last_applied_boot_id"], "boot-b")

        reloaded = self.controller()
        self.assertFalse(reloaded.reapply_startup())
        self.assertEqual(reloaded.htr_writes, [])
        self.assertEqual(self.events, [])

    def test_startup_restores_saved_off_state_once(self):
        controller = self.controller()
        active, _target = self.saved_active_htr_state(controller)
        controller.set_state(self.request(active, mode="off"))
        self.write_htr_snapshot(tuple((7, 8, 9) for _zone in range(8)))
        self.boot_id.write_text("boot-b\n")
        restarted = self.controller()

        self.assertEqual(
            restarted.reapply_startup(), rgb.PROVIDER_HTR3212_STATIC)
        self.assertEqual(
            self.htr_snapshot(), tuple((0, 0, 0) for _zone in range(8)))
        self.assertEqual(len(restarted.htr_writes), 24)
        saved = json.loads(restarted.htr_preferences_path.read_text())
        self.assertEqual(saved["mode"], "off")
        reloaded = self.controller()
        self.assertFalse(reloaded.reapply_startup())
        self.assertEqual(reloaded.htr_writes, [])

    def test_startup_same_boot_or_already_matching_target_never_writes(self):
        controller = self.controller()
        _active, target = self.saved_active_htr_state(controller)
        controller.htr_writes.clear()

        self.assertFalse(controller.reapply_startup())
        self.assertEqual(controller.htr_writes, [])

        self.boot_id.write_text("boot-b\n")
        restarted = self.controller()
        self.assertEqual(self.htr_snapshot(), target)
        self.assertFalse(restarted.reapply_startup())
        self.assertEqual(restarted.htr_writes, [])
        saved = json.loads(restarted.htr_preferences_path.read_text())
        self.assertEqual(saved["last_applied_boot_id"], "boot-b")

    def test_same_boot_reload_preserves_newer_external_htr3212_state(self):
        controller = self.controller()
        self.saved_active_htr_state(controller)
        external = list(list(zone) for zone in self.htr_snapshot())
        external[0][0] = 199
        external = tuple(tuple(zone) for zone in external)
        self.write_htr_snapshot(external)
        saved = controller.htr_preferences_path.read_bytes()
        restarted = self.controller()

        self.assertFalse(restarted.reapply_startup())
        self.assertEqual(restarted.htr_writes, [])
        self.assertEqual(self.htr_snapshot(), external)
        self.assertEqual(restarted.htr_preferences_path.read_bytes(), saved)

    def test_startup_rejects_corrupt_or_inconsistent_htr3212_state(self):
        controller = self.controller()
        _active, target = self.saved_active_htr_state(controller)
        valid = json.loads(controller.htr_preferences_path.read_text())
        signatures = {
            "short": "1 2 3",
            "malformed": "x " + " ".join("0" for _index in range(23)),
            "out-of-range": "256 " + " ".join("0" for _index in range(23)),
            "lighting-mismatch": self.controller()._htr3212_signature((
                (target[0][0] + 1, target[0][1], target[0][2]),
                *target[1:],
            )),
        }
        self.boot_id.write_text("boot-b\n")
        for label, signature in signatures.items():
            with self.subTest(label=label):
                candidate = dict(valid)
                candidate["native_signature"] = signature
                candidate["last_applied_boot_id"] = "boot-a"
                controller.htr_preferences_path.write_text(
                    json.dumps(candidate))
                self.write_htr_snapshot(
                    tuple((0, 0, 0) for _zone in range(8)))
                restarted = self.controller()

                self.assertFalse(restarted.reapply_startup())
                self.assertEqual(restarted.htr_writes, [])
                persisted = json.loads(
                    restarted.htr_preferences_path.read_text())
                self.assertEqual(persisted["last_applied_boot_id"], "boot-a")

        metadata_cases = {
            "invalid-mode": {"mode": "battery"},
            "off-with-nonzero-output": {"mode": "off"},
            "wrong-provider": {"provider": "analog-static"},
            "future-version": {"version": 999},
        }
        for label, updates in metadata_cases.items():
            with self.subTest(label=label):
                candidate = dict(valid)
                candidate.update(updates)
                candidate["last_applied_boot_id"] = "boot-a"
                controller.htr_preferences_path.write_text(
                    json.dumps(candidate))
                self.write_htr_snapshot(
                    tuple((0, 0, 0) for _zone in range(8)))
                restarted = self.controller()

                self.assertFalse(restarted.reapply_startup())
                self.assertEqual(restarted.htr_writes, [])
                self.assertEqual(
                    json.loads(restarted.htr_preferences_path.read_text()),
                    candidate)

        controller.htr_preferences_path.write_text("{not-json")
        restarted = self.controller()
        self.assertFalse(restarted.reapply_startup())
        self.assertEqual(restarted.htr_writes, [])
        self.assertEqual(
            restarted.htr_preferences_path.read_text(), "{not-json")

    def test_startup_invalid_htr3212_abi_fails_closed(self):
        controller = self.controller()
        self.saved_active_htr_state(controller)
        saved = controller.htr_preferences_path.read_bytes()
        self.boot_id.write_text("boot-b\n")
        (self.leds / "l:r1").unlink()
        restarted = self.controller()

        self.assertFalse(restarted.reapply_startup())
        self.assertEqual(restarted.htr_writes, [])
        self.assertEqual(self.events, [])
        self.assertEqual(restarted.htr_preferences_path.read_bytes(), saved)

    def test_failed_user_apply_never_replaces_startup_restore_target(self):
        controller = self.controller()
        active, target = self.saved_active_htr_state(controller)
        saved = controller.htr_preferences_path.read_bytes()
        rejected = active["lighting"]
        for zone in rejected["zones"]:
            zone["color"] = [210, 180, 150]
            zone["brightness"] = 255
        controller.htr_writes.clear()
        controller.fail_on_write = 2

        with self.assertRaisesRegex(RuntimeError, "injected"):
            controller.set_state(self.request(
                active, mode="rgb", lighting=rejected))
        self.assertEqual(controller.htr_preferences_path.read_bytes(), saved)

        self.write_htr_snapshot(tuple((0, 0, 0) for _zone in target))
        self.boot_id.write_text("boot-b\n")
        restarted = self.controller()
        self.assertEqual(
            restarted.reapply_startup(), rgb.PROVIDER_HTR3212_STATIC)
        self.assertEqual(self.htr_snapshot(), target)

    def test_failed_preference_save_preserves_prior_startup_restore_target(self):
        controller = self.controller()
        active, target = self.saved_active_htr_state(controller)
        saved = controller.htr_preferences_path.read_bytes()
        rejected = active["lighting"]
        for zone in rejected["zones"]:
            zone["color"] = [201, 171, 141]
            zone["brightness"] = 255
        controller.htr_writes.clear()
        controller._save_htr3212_preferences = mock.Mock(
            side_effect=OSError("preferences unavailable"))

        with self.assertRaisesRegex(OSError, "preferences unavailable"):
            controller.set_state(self.request(
                active, mode="rgb", lighting=rejected))
        self.assertEqual(controller.htr_preferences_path.read_bytes(), saved)
        self.assertEqual(self.htr_snapshot(), target)

        self.write_htr_snapshot(tuple((0, 0, 0) for _zone in target))
        self.boot_id.write_text("boot-b\n")
        restarted = self.controller()
        self.assertEqual(
            restarted.reapply_startup(), rgb.PROVIDER_HTR3212_STATIC)
        self.assertEqual(self.htr_snapshot(), target)

    def test_startup_tombstone_failure_writes_no_htr3212_state(self):
        controller = self.controller()
        _active, target = self.saved_active_htr_state(controller)
        self.write_htr_snapshot(tuple((0, 0, 0) for _zone in target))
        self.boot_id.write_text("boot-b\n")
        restarted = self.controller()
        restarted._save_htr3212_preferences = mock.Mock(
            side_effect=OSError("preferences unavailable"))

        with self.assertRaisesRegex(OSError, "preferences unavailable"):
            restarted.reapply_startup()
        self.assertEqual(restarted.htr_writes, [])
        self.assertEqual(
            self.htr_snapshot(), tuple((0, 0, 0) for _zone in target))

    def test_startup_write_failure_is_tombstoned_and_never_retried(self):
        controller = self.controller()
        _active, target = self.saved_active_htr_state(controller)
        self.write_htr_snapshot(tuple((0, 0, 0) for _zone in target))
        self.boot_id.write_text("boot-b\n")
        restarted = self.controller()
        restarted.fail_on_write = 2

        with self.assertRaisesRegex(RuntimeError, "injected"):
            restarted.reapply_startup()
        saved = json.loads(restarted.htr_preferences_path.read_text())
        self.assertEqual(saved["last_applied_boot_id"], "boot-b")
        reloaded = self.controller()
        self.assertFalse(reloaded.reapply_startup())
        self.assertEqual(reloaded.htr_writes, [])

    def test_production_writer_uses_one_ascii_sysfs_write(self):
        target = self.root / "brightness"
        target.write_text("0\n")
        real_open, real_write, real_close = os.open, os.write, os.close
        with (mock.patch.object(rgb.os, "open", side_effect=real_open) as opened,
              mock.patch.object(rgb.os, "write", side_effect=real_write) as written,
              mock.patch.object(rgb.os, "close", side_effect=real_close) as closed):
            rgb.RGBController._write_htr3212_brightness(target, 123)

        self.assertEqual(opened.call_count, 1)
        self.assertEqual(opened.call_args.args, (
            target, os.O_WRONLY | os.O_CLOEXEC | os.O_TRUNC))
        self.assertEqual(written.call_count, 1)
        self.assertEqual(written.call_args.args[1], b"123\n")
        self.assertEqual(closed.call_count, 1)
        self.assertEqual(target.read_text(), "123\n")

    def test_production_writer_explains_suspend_and_reprobe_failures(self):
        cases = (
            (errno.EBUSY, "suspended; retry after resume"),
            (errno.ENOENT, "interface disappeared; refresh and retry"),
            (errno.ENODEV, "interface disappeared; refresh and retry"),
        )
        for error_number, message in cases:
            with self.subTest(error_number=error_number), mock.patch.object(
                    rgb.os, "open",
                    side_effect=OSError(error_number, os.strerror(error_number))):
                with self.assertRaisesRegex(RuntimeError, message):
                    rgb.RGBController._write_htr3212_brightness(
                        self.root / "missing", 1)


class EvoTestController(rgb.RGBController):
    """Regular-file emulator for the ABI's cross-attribute semantics."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.evo_writes = []

    def _write_evo_attribute(self, path, value):
        path = Path(path)
        self.evo_writes.append((path.name, value))
        path.write_text(value + "\n")
        if path.name == "zone_layout":
            (path.parent / "effect").write_text("static\n")


class PocketEvoRGBControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings_dir = self.root / "settings"
        self.evo_root = self.root / "leds"
        self.evo_root.mkdir()
        self.evo = self.root / "evo-rgb"
        self.evo.mkdir()
        self.layout = tuple(
            value
            for index in range(8)
            for value in (index + 1, index + 2, index + 3, 100 + index)
        )
        attributes = {
            "abi_version": "3\n",
            "zone_index": " ".join(rgb.EVO_ZONE_INDEX) + "\n",
            "zone_layout": " ".join(str(value) for value in self.layout) + "\n",
            "available_effects": (
                "static breath rgb-breath rainbow reactive future-effect\n"),
            "effect": "static\n",
            "calibration": "15 20\n",
            "enabled": "1\n",
        }
        for name, value in attributes.items():
            path = self.evo / name
            path.write_text(value)
            path.chmod(0o644 if name in rgb.EVO_WRITABLE_ATTRIBUTES else 0o444)
        # Every zone's device/rgb link resolves to the same canonical ABI.
        for zone_id in rgb.EVO_ZONE_INDEX:
            device = self.evo_root / f"rgb:joystick-backlight-{zone_id}" / "device"
            device.mkdir(parents=True)
            (device / "rgb").symlink_to(self.evo, target_is_directory=True)

        self.legacy_helper = self.root / "analog_sticks_ledcontrol"
        self.legacy_helper.write_text("#!/bin/sh\nexit 0\n")
        self.legacy_helper.chmod(0o755)
        self.led_control = self.root / "ledcontrol"
        self.led_control.write_text("#!/bin/sh\nexit 0\n")
        self.led_control.chmod(0o755)
        self.boot_id = self.root / "boot_id"
        self.boot_id.write_text("boot-a\n")
        self.values = {
            "led.color": "rgb",
            "analogsticks.led": "255 1 2 3 1 2 3",
        }
        self.events = []

    def tearDown(self):
        self.temporary.cleanup()

    def fake_run(self, command, check=True):
        self.events.append(("run", tuple(command), check))
        if command[0] == str(self.led_control) and command[1] == "list":
            return "off battery rgb"
        return ""

    def get_setting(self, name, default=""):
        return self.values.get(name, default)

    def set_setting(self, name, value):
        self.events.append(("set", name, value))
        self.values[name] = value

    def controller(self, *, root=None, cls=EvoTestController,
                   generic=True, fit_path=None):
        fit = fit_path or (self.root / "missing-fit")
        return cls(
            self.settings_dir,
            run=self.fake_run,
            get_setting=self.get_setting,
            set_setting=self.set_setting,
            get_runtime_capability=lambda name: (
                "true" if generic and name == rgb.ANALOG_STICKS_CAPABILITY
                else ""),
            led_control=self.led_control,
            analog_sticks_led_control=self.legacy_helper,
            led_path=fit,
            boot_id_path=self.boot_id,
            evo_leds_root=root or self.evo_root,
        )

    @staticmethod
    def request(state, *, mode=None, lighting=None):
        return {
            "provider": "pocket-evo-v3",
            "revision": state["revision"],
            "mode": mode or state["mode"],
            "lighting": lighting or state["lighting"],
        }

    def test_abi3_wins_and_deduplicates_all_zone_symlinks(self):
        fit = self.root / "konkr:rgb:joysticks"
        fit.mkdir()
        for name, value in (
            ("brightness", "255\n"), ("max_brightness", "255\n"),
            ("multi_intensity", "255 255 255\n"), ("effect", "static\n"),
        ):
            (fit / name).write_text(value)
        capabilities = self.controller(fit_path=fit).capabilities()

        self.assertTrue(capabilities["available"])
        self.assertEqual(capabilities["provider"], "pocket-evo-v3")
        self.assertEqual(capabilities["effects"], list(rgb.EVO_EFFECTS))
        self.assertFalse(capabilities["shared_zone"])
        status, path, effects, error = self.controller()._discover_evo()
        self.assertEqual((status, path, effects, error), (
            "valid", self.evo.resolve(), rgb.EVO_EFFECTS, ""))
        # Public capabilities cross the RPC boundary and must not disclose a
        # filesystem path or contain a non-serializable Path object.
        json.dumps(capabilities)
        self.assertFalse(any("path" in key for key in capabilities))
        self.assertEqual(self.events, [])

    def test_completely_absent_abi_preserves_generic_evo_s_fallback(self):
        empty = self.root / "unpatched-evo-s"
        empty.mkdir()
        state = self.controller(root=empty).get_state()

        self.assertTrue(state["supported"])
        self.assertEqual(state["provider"], "analog-static")
        self.assertEqual(state["effect"], "static")
        self.assertEqual(self.events, [])

    def test_present_but_invalid_abi_fails_closed_without_legacy_probes(self):
        invalid_values = (
            ("abi_version", "4\n"),
            ("zone_index", " ".join(reversed(rgb.EVO_ZONE_INDEX)) + "\n"),
            ("available_effects", "static breath rainbow reactive\n"),
        )
        for attribute, value in invalid_values:
            with self.subTest(attribute=attribute):
                original = (self.evo / attribute).read_text()
                (self.evo / attribute).chmod(0o644)
                (self.evo / attribute).write_text(value)
                if attribute not in rgb.EVO_WRITABLE_ATTRIBUTES:
                    (self.evo / attribute).chmod(0o444)
                self.events.clear()
                capabilities = self.controller().capabilities()
                self.assertFalse(capabilities["available"])
                self.assertEqual(capabilities["provider"], "pocket-evo-v3")
                self.assertEqual(self.events, [])
                (self.evo / attribute).chmod(0o644)
                (self.evo / attribute).write_text(original)
                if attribute not in rgb.EVO_WRITABLE_ATTRIBUTES:
                    (self.evo / attribute).chmod(0o444)

        (self.evo / "effect").chmod(0o444)
        capabilities = self.controller().capabilities()
        self.assertFalse(capabilities["available"])
        self.assertEqual(capabilities["provider"], "pocket-evo-v3")

    def test_missing_or_ambiguous_evo_interface_fails_closed(self):
        (self.evo / "enabled").unlink()
        self.assertEqual(
            self.controller().capabilities()["provider"], "pocket-evo-v3")
        self.assertFalse(self.controller().capabilities()["available"])

        (self.evo / "enabled").write_text("1\n")
        (self.evo / "enabled").chmod(0o644)
        other = self.root / "other-evo-rgb"
        other.mkdir()
        for name in rgb.EVO_ATTRIBUTES:
            (other / name).write_text((self.evo / name).read_text())
            (other / name).chmod(
                0o644 if name in rgb.EVO_WRITABLE_ATTRIBUTES else 0o444)
        device = self.evo_root / "rgb:joystick-backlight-extra" / "device"
        device.mkdir(parents=True)
        (device / "rgb").symlink_to(other, target_is_directory=True)
        capabilities = self.controller().capabilities()
        self.assertFalse(capabilities["available"])
        self.assertIn("ambiguous", capabilities["error"])

    def test_state_requires_two_identical_canonical_snapshots(self):
        controller = self.controller()
        original = controller._read_evo_snapshot_once
        calls = 0

        def changing(path):
            nonlocal calls
            snapshot = original(path)
            calls += 1
            if calls % 2 == 0:
                snapshot["enabled"] = 1 - snapshot["enabled"]
            return snapshot

        controller._read_evo_snapshot_once = changing
        state = controller.get_state()

        self.assertFalse(state["valid"])
        self.assertIn("unstable", state["error"])
        self.assertEqual(self.events, [])

    def test_malformed_mutable_attributes_fail_closed_without_writes(self):
        malformed = (
            ("zone_layout", " ".join("0" for _ in range(31))),
            ("zone_layout", " ".join([*("0" for _ in range(31)), "256"])),
            ("effect", "breath 10 20 30"),
            ("calibration", "15 101"),
            ("enabled", "2"),
        )
        for name, value in malformed:
            with self.subTest(name=name, value=value):
                original = (self.evo / name).read_text()
                (self.evo / name).write_text(value + "\n")
                controller = self.controller()
                state = controller.get_state()
                self.assertTrue(state["supported"])
                self.assertFalse(state["valid"])
                with self.assertRaises(RuntimeError):
                    controller.set_state({})
                self.assertEqual(controller.evo_writes, [])
                self.assertEqual(self.events, [])
                (self.evo / name).write_text(original)

    def test_stale_lighting_revision_rejects_before_native_write(self):
        controller = self.controller()
        state = controller.get_state()
        request = self.request(state)
        request["revision"] = "stale"

        with self.assertRaisesRegex(ValueError, "state changed"):
            controller.set_state(request)

        self.assertEqual(controller.evo_writes, [])
        self.assertEqual(self.events, [])

    def test_state_parses_zones_effect_calibration_and_gate(self):
        (self.evo / "effect").write_text("reactive 180 1 2 3 4 5 6\n")
        (self.evo / "calibration").write_text("35 45\n")
        (self.evo / "enabled").write_text("0\n")
        state = self.controller().get_state()

        self.assertTrue(state["valid"])
        self.assertEqual(state["mode"], "rgb")
        self.assertTrue(state["temporarily_gated"])
        self.assertEqual(state["lighting"]["effect"], "reactive")
        self.assertEqual(state["lighting"]["brightness"], 180)
        self.assertEqual(state["lighting"]["idle_color"], [1, 2, 3])
        self.assertEqual(state["lighting"]["active_color"], [4, 5, 6])
        self.assertEqual(state["lighting"]["zones"][0], {
            "id": "left-270", "color": [1, 2, 3], "brightness": 100,
        })
        self.assertEqual(state["calibration"], {
            "green_percent": 35, "blue_percent": 45,
        })
        json.dumps(state)

    def test_revision_covers_all_four_mutable_components(self):
        controller = self.controller()
        baseline = controller.get_state()["revision"]
        mutations = (
            ("zone_layout", " ".join("0" for _ in range(32))),
            ("effect", "breath 1 2 3 4"),
            ("calibration", "16 20"),
            ("enabled", "0"),
        )
        for name, value in mutations:
            with self.subTest(name=name):
                old = (self.evo / name).read_text()
                (self.evo / name).write_text(value + "\n")
                revision = controller.get_state()["revision"]
                self.assertNotEqual(revision, baseline)
                (self.evo / name).write_text(old)

    def test_all_effect_grammars_are_exact(self):
        valid = {
            "static": {"effect": "static"},
            "breath 180 1 2 3": {
                "effect": "breath", "brightness": 180, "color": (1, 2, 3)},
            "rgb-breath 42": {"effect": "rgb-breath", "brightness": 42},
            "rainbow": {"effect": "rainbow"},
            "reactive 99 1 2 3 4 5 6": {
                "effect": "reactive", "brightness": 99,
                "idle_color": (1, 2, 3), "active_color": (4, 5, 6)},
        }
        for command, parsed in valid.items():
            with self.subTest(command=command):
                self.assertEqual(rgb._parse_evo_effect(command), parsed)
                self.assertEqual(rgb._evo_effect_command(parsed), command)
        for command in (
            "", "static 1", "breath 1 2 3", "breath 1 2 3 256",
            "rgb-breath -1", "rainbow 1", "reactive 1 2 3 4 5 6",
            "future-effect", "breath 1,2,3,4",
        ):
            with self.subTest(command=command):
                self.assertIsNone(rgb._parse_evo_effect(command))

    def test_static_save_emits_exact_canonical_32_values_only(self):
        controller = self.controller()
        state = controller.get_state()
        lighting = state["lighting"]
        lighting["layout_mode"] = "quadrants"
        for index, zone in enumerate(lighting["zones"]):
            zone["color"] = [index, index + 10, index + 20]
            zone["brightness"] = 200 + index

        applied = controller.set_state(self.request(state, lighting=lighting))

        self.assertEqual(len(controller.evo_writes), 1)
        attribute, command = controller.evo_writes[0]
        self.assertEqual(attribute, "zone_layout")
        expected = tuple(
            value for index in range(8)
            for value in (index, index + 10, index + 20, 200 + index))
        self.assertEqual(tuple(int(value) for value in command.split()), expected)
        self.assertEqual(len(command.split()), 32)
        self.assertEqual(applied["lighting"]["layout_mode"], "quadrants")
        self.assertEqual(self.events, [])

    def test_effect_saves_use_only_evo_abi_and_never_software_correction(self):
        commands = {
            "breath": "breath 64 255 200 100",
            "rgb-breath": "rgb-breath 64",
            "rainbow": "rainbow",
            "reactive": "reactive 64 255 200 100 0 10 20",
        }
        for effect, expected in commands.items():
            with self.subTest(effect=effect):
                (self.evo / "effect").write_text("static\n")
                controller = self.controller()
                state = controller.get_state()
                lighting = state["lighting"]
                lighting.update({
                    "effect": effect,
                    "brightness": 64,
                    "color": [255, 200, 100],
                    "idle_color": [255, 200, 100],
                    "active_color": [0, 10, 20],
                })
                controller.set_state(self.request(state, lighting=lighting))
                self.assertEqual(controller.evo_writes, [("effect", expected)])
                self.assertEqual(self.events, [])

    def test_saved_editor_metadata_requires_exact_native_lighting_snapshot(self):
        controller = self.controller()
        state = controller.get_state()
        lighting = state["lighting"]
        lighting.update({
            "effect": "breath", "brightness": 64, "color": [1, 2, 3],
        })
        controller.set_state(self.request(state, lighting=lighting))

        # Another client replaces the cached Static layout and then selects
        # the same advanced-effect command RKE last used. Matching the effect
        # alone must not make RKE resurrect its older hidden zone metadata.
        external_layout = tuple(
            value for _index in range(8) for value in (9, 8, 7, 6))
        (self.evo / "zone_layout").write_text(
            " ".join(str(value) for value in external_layout) + "\n")
        (self.evo / "effect").write_text("breath 64 1 2 3\n")

        refreshed = controller.get_state()

        self.assertEqual(refreshed["lighting"]["effect"], "breath")
        self.assertEqual(refreshed["lighting"]["zones"][0], {
            "id": "left-270", "color": [9, 8, 7], "brightness": 6,
        })

    def test_advanced_effect_readback_rejects_concurrent_hidden_layout(self):
        external_layout = tuple(
            value for _index in range(8) for value in (9, 8, 7, 6))

        class ExternalLayoutAfterEffect(EvoTestController):
            def _write_evo_attribute(inner_self, path, value):
                super()._write_evo_attribute(path, value)
                if Path(path).name == "effect":
                    (Path(path).parent / "zone_layout").write_text(
                        " ".join(str(item) for item in external_layout) + "\n")

        controller = self.controller(cls=ExternalLayoutAfterEffect)
        state = controller.get_state()
        lighting = state["lighting"]
        lighting.update({
            "effect": "breath", "brightness": 64, "color": [1, 2, 3],
        })

        with self.assertRaisesRegex(
                RuntimeError, "native readback did not match"):
            controller.set_state(self.request(state, lighting=lighting))

        refreshed = controller.get_state()
        self.assertEqual(refreshed["lighting"]["effect"], "breath")
        self.assertEqual(refreshed["lighting"]["zones"][0], {
            "id": "left-270", "color": [9, 8, 7], "brightness": 6,
        })
        self.assertFalse((self.settings_dir / "rgb-settings.json").exists())

    def test_lighting_persistence_failure_uses_guarded_native_rollback(self):
        controller = self.controller()
        state = controller.get_state()
        lighting = state["lighting"]
        lighting.update({
            "effect": "breath", "brightness": 64, "color": [1, 2, 3],
        })
        controller._save_preferences = mock.Mock(
            side_effect=OSError("preferences unavailable"))

        with self.assertRaisesRegex(OSError, "preferences unavailable"):
            controller.set_state(self.request(state, lighting=lighting))

        self.assertEqual(controller.evo_writes, [
            ("effect", "breath 64 1 2 3"),
            ("zone_layout", " ".join(str(value) for value in self.layout)),
        ])
        self.assertEqual((self.evo / "effect").read_text().strip(), "static")

        controller = self.controller()
        state = controller.get_state()
        lighting = state["lighting"]
        lighting.update({
            "effect": "breath", "brightness": 64, "color": [1, 2, 3],
        })

        def external_change(_preferences):
            (self.evo / "effect").write_text("rainbow\n")
            raise OSError("preferences unavailable")

        controller._save_preferences = external_change
        with self.assertRaisesRegex(OSError, "preferences unavailable"):
            controller.set_state(self.request(state, lighting=lighting))
        self.assertEqual(controller.evo_writes, [
            ("effect", "breath 64 1 2 3"),
        ])
        self.assertEqual((self.evo / "effect").read_text().strip(), "rainbow")

        # The inactive Static layout is native state too. When an advanced
        # effect is replaced with Static, a failed preference save must put
        # both the old cached layout and the old selected effect back.
        (self.evo / "zone_layout").write_text(
            " ".join(str(value) for value in self.layout) + "\n")
        (self.evo / "effect").write_text("breath 90 10 20 30\n")
        controller = self.controller()
        state = controller.get_state()
        lighting = state["lighting"]
        lighting["effect"] = "static"
        for zone in lighting["zones"]:
            zone["color"] = [200, 100, 50]
            zone["brightness"] = 80
        rejected_layout = " ".join(
            str(value) for _index in range(8)
            for value in (200, 100, 50, 80))
        controller._save_preferences = mock.Mock(
            side_effect=OSError("preferences unavailable"))

        with self.assertRaisesRegex(OSError, "preferences unavailable"):
            controller.set_state(self.request(state, lighting=lighting))

        self.assertEqual(controller.evo_writes, [
            ("zone_layout", rejected_layout),
            ("zone_layout", " ".join(str(value) for value in self.layout)),
            ("effect", "breath 90 10 20 30"),
        ])
        self.assertEqual(
            (self.evo / "zone_layout").read_text().strip(),
            " ".join(str(value) for value in self.layout))
        self.assertEqual(
            (self.evo / "effect").read_text().strip(),
            "breath 90 10 20 30")

    def test_static_editor_layout_mode_is_metadata_not_an_effect(self):
        controller = self.controller()
        state = controller.get_state()
        lighting = state["lighting"]
        lighting["layout_mode"] = "quadrants"
        for zone in lighting["zones"]:
            zone["color"] = [20, 30, 40]
            zone["brightness"] = 80

        applied = controller.set_state(self.request(state, lighting=lighting))

        self.assertEqual(applied["lighting"]["effect"], "static")
        self.assertEqual(applied["lighting"]["layout_mode"], "quadrants")
        self.assertEqual(controller.evo_writes[0][0], "zone_layout")

    def test_off_saves_resume_state_and_on_restores_without_enabled_write(self):
        (self.evo / "effect").write_text("breath 90 10 20 30\n")
        (self.evo / "enabled").write_text("0\n")
        controller = self.controller()
        active = controller.get_state()
        self.assertTrue(active["temporarily_gated"])

        off = controller.set_state(self.request(active, mode="off"))
        self.assertEqual(off["mode"], "off")
        self.assertTrue(off["temporarily_gated"])
        self.assertEqual(off["resume_lighting"]["effect"], "breath")
        self.assertEqual(controller.evo_writes[0][0], "zone_layout")
        self.assertNotIn("enabled", [write[0] for write in controller.evo_writes])

        controller.evo_writes.clear()
        enabled = controller.set_state(self.request(off, mode="rgb"))
        self.assertEqual(controller.evo_writes, [("effect", "breath 90 10 20 30")])
        self.assertEqual(enabled["mode"], "rgb")
        self.assertTrue(enabled["temporarily_gated"])
        self.assertEqual(
            enabled["lighting"]["zones"], active["lighting"]["zones"])

        # Repeating the cycle must not replace the saved Static draft with the
        # all-zero layout that remains hidden beneath the advanced effect.
        controller.evo_writes.clear()
        second_off = controller.set_state(self.request(enabled, mode="off"))
        self.assertEqual(second_off["resume_lighting"]["zones"],
                         active["lighting"]["zones"])
        controller.evo_writes.clear()
        enabled = controller.set_state(self.request(second_off, mode="rgb"))
        self.assertEqual(controller.evo_writes, [
            ("effect", "breath 90 10 20 30")])
        self.assertEqual(enabled["lighting"]["zones"],
                         active["lighting"]["zones"])

        # The all-zero native Off layout must not erase the saved Static draft
        # underneath an advanced effect. Selecting Static later restores the
        # exact pre-Off zone layout in one complete command.
        controller.evo_writes.clear()
        static_lighting = enabled["lighting"]
        static_lighting["effect"] = "static"
        controller.set_state(self.request(enabled, lighting=static_lighting))
        self.assertEqual(controller.evo_writes, [(
            "zone_layout",
            " ".join(str(value) for value in self.layout),
        )])

    def test_calibration_save_reset_and_raw_are_separate_transactions(self):
        controller = self.controller()
        state = controller.get_state()

        saved = controller.set_calibration({
            "provider": "pocket-evo-v3", "revision": state["revision"],
            "action": "save", "green_percent": 30, "blue_percent": 40,
        })
        self.assertEqual(controller.evo_writes, [("calibration", "30 40")])
        self.assertEqual(saved["calibration_override"], {
            "green_percent": 30, "blue_percent": 40,
        })

        controller.evo_writes.clear()
        raw = controller.set_calibration({
            "provider": "pocket-evo-v3", "revision": saved["revision"],
            "action": "raw",
        })
        self.assertEqual(controller.evo_writes, [("calibration", "100 100")])
        self.assertEqual(raw["calibration_override"], {
            "green_percent": 100, "blue_percent": 100,
        })

        controller.evo_writes.clear()
        reset = controller.set_calibration({
            "provider": "pocket-evo-v3", "revision": raw["revision"],
            "action": "reset",
        })
        self.assertEqual(controller.evo_writes, [("calibration", "15 20")])
        self.assertIsNone(reset["calibration_override"])

    def test_invalid_or_stale_calibration_never_writes(self):
        controller = self.controller()
        state = controller.get_state()
        invalid = (
            {"action": "save", "green_percent": -1, "blue_percent": 20},
            {"action": "save", "green_percent": 20, "blue_percent": 101},
            {"action": "preview"},
        )
        for values in invalid:
            with self.subTest(values=values):
                request = {
                    "provider": "pocket-evo-v3",
                    "revision": state["revision"], **values,
                }
                with self.assertRaises(ValueError):
                    controller.set_calibration(request)
        stale = {
            "provider": "pocket-evo-v3", "revision": "stale",
            "action": "reset",
        }
        with self.assertRaisesRegex(ValueError, "state changed"):
            controller.set_calibration(stale)
        self.assertEqual(controller.evo_writes, [])

    def test_calibration_persistence_failure_rolls_back_only_if_unchanged(self):
        controller = self.controller()
        state = controller.get_state()
        controller._save_preferences = mock.Mock(
            side_effect=OSError("preferences unavailable"))
        with self.assertRaisesRegex(OSError, "preferences unavailable"):
            controller.set_calibration({
                "provider": "pocket-evo-v3", "revision": state["revision"],
                "action": "save", "green_percent": 30, "blue_percent": 40,
            })
        self.assertEqual(controller.evo_writes, [
            ("calibration", "30 40"), ("calibration", "15 20"),
        ])
        self.assertEqual((self.evo / "calibration").read_text().strip(), "15 20")

        controller = self.controller()
        state = controller.get_state()

        def external_change(_preferences):
            (self.evo / "calibration").write_text("77 77\n")
            raise OSError("preferences unavailable")

        controller._save_preferences = external_change
        with self.assertRaisesRegex(OSError, "preferences unavailable"):
            controller.set_calibration({
                "provider": "pocket-evo-v3", "revision": state["revision"],
                "action": "save", "green_percent": 30, "blue_percent": 40,
            })
        self.assertEqual(controller.evo_writes, [("calibration", "30 40")])
        self.assertEqual((self.evo / "calibration").read_text().strip(), "77 77")

    def test_v3_preferences_do_not_leak_into_unpatched_generic_provider(self):
        controller = self.controller()
        state = controller.get_state()
        controller.set_calibration({
            "provider": "pocket-evo-v3", "revision": state["revision"],
            "action": "save", "green_percent": 30, "blue_percent": 40,
        })
        empty = self.root / "unpatched"
        empty.mkdir()

        generic = self.controller(root=empty).get_state()

        self.assertEqual(generic["provider"], "analog-static")
        self.assertEqual(generic["color"], [1, 2, 3])
        self.assertNotIn("calibration", generic)

    def test_generic_fallback_save_preserves_dormant_evo_preferences(self):
        controller = self.controller()
        state = controller.get_state()
        lighting = state["lighting"]
        lighting.update({
            "effect": "reactive", "brightness": 70,
            "idle_color": [10, 20, 30], "active_color": [40, 50, 60],
        })
        state = controller.set_state(self.request(state, lighting=lighting))
        controller.set_calibration({
            "provider": "pocket-evo-v3", "revision": state["revision"],
            "action": "save", "green_percent": 30, "blue_percent": 40,
        })

        unpatched = self.root / "unpatched"
        unpatched.mkdir()
        generic = self.controller(root=unpatched)
        generic_state = generic.get_state()
        generic.set_state({
            "provider": "analog-static",
            "revision": generic_state["revision"],
            "mode": "rgb",
            "effect": "static",
            "color": [80, 90, 100],
            "brightness": 110,
            "correction": False,
        })

        saved = json.loads((self.settings_dir / "rgb-settings.json").read_text())
        self.assertEqual(saved["provider"], "pocket-evo-v3")
        self.assertEqual(saved["calibration_override"], {
            "green_percent": 30, "blue_percent": 40,
        })
        self.assertEqual(saved["resume_lighting"]["effect"], "reactive")
        self.assertEqual(
            saved["legacy_preferences"]["provider"], "analog-static")

        restored = self.controller().get_state()
        self.assertEqual(restored["calibration_override"], {
            "green_percent": 30, "blue_percent": 40,
        })

    def test_startup_restores_only_explicit_calibration_once_per_boot(self):
        controller = self.controller()
        self.assertFalse(controller.reapply_startup())
        state = controller.get_state()
        controller.set_calibration({
            "provider": "pocket-evo-v3", "revision": state["revision"],
            "action": "save", "green_percent": 30, "blue_percent": 40,
        })
        controller.evo_writes.clear()
        (self.evo / "calibration").write_text("15 20\n")
        (self.evo / "effect").write_text("rainbow\n")
        self.boot_id.write_text("boot-b\n")

        self.assertEqual(controller.reapply_startup(), "calibration")
        self.assertEqual(controller.evo_writes, [("calibration", "30 40")])
        self.assertEqual((self.evo / "effect").read_text().strip(), "rainbow")
        self.assertFalse(controller.reapply_startup())

    def test_startup_calibration_does_not_retry_over_external_same_boot_write(self):
        controller = self.controller()
        state = controller.get_state()
        controller.set_calibration({
            "provider": "pocket-evo-v3", "revision": state["revision"],
            "action": "save", "green_percent": 30, "blue_percent": 40,
        })
        controller.evo_writes.clear()
        (self.evo / "calibration").write_text("15 20\n")
        self.boot_id.write_text("boot-b\n")

        def write_then_external(path, value):
            path = Path(path)
            controller.evo_writes.append((path.name, value))
            path.write_text(value + "\n")
            if path.name == "calibration":
                path.write_text("77 77\n")

        controller._write_evo_attribute = write_then_external

        self.assertFalse(controller.reapply_startup())
        self.assertEqual(controller.evo_writes, [("calibration", "30 40")])
        self.assertEqual((self.evo / "calibration").read_text().strip(), "77 77")
        saved = json.loads((self.settings_dir / "rgb-settings.json").read_text())
        self.assertEqual(saved["last_calibration_boot_id"], "boot-b")

        # A Decky/plugin restart in the same boot sees the tombstone and must
        # not replay the saved override over the external value.
        restarted = self.controller()
        self.assertFalse(restarted.reapply_startup())
        self.assertEqual(restarted.evo_writes, [])
        self.assertEqual((self.evo / "calibration").read_text().strip(), "77 77")

    def test_startup_calibration_tombstone_failure_writes_no_native_state(self):
        controller = self.controller()
        state = controller.get_state()
        controller.set_calibration({
            "provider": "pocket-evo-v3", "revision": state["revision"],
            "action": "save", "green_percent": 30, "blue_percent": 40,
        })
        controller.evo_writes.clear()
        (self.evo / "calibration").write_text("15 20\n")
        self.boot_id.write_text("boot-b\n")
        controller._save_preferences = mock.Mock(
            side_effect=OSError("preferences unavailable"))

        with self.assertRaisesRegex(OSError, "preferences unavailable"):
            controller.reapply_startup()

        self.assertEqual(controller.evo_writes, [])
        self.assertEqual((self.evo / "calibration").read_text().strip(), "15 20")

    def test_production_writer_uses_exactly_one_os_write(self):
        target = self.root / "single-write"
        target.write_text("old\n")
        real_open, real_write, real_close = os.open, os.write, os.close
        with (mock.patch.object(rgb.os, "open", side_effect=real_open) as opened,
              mock.patch.object(rgb.os, "write", side_effect=real_write) as written,
              mock.patch.object(rgb.os, "close", side_effect=real_close) as closed):
            rgb.RGBController._write_evo_attribute(target, "rainbow")

        self.assertEqual(opened.call_count, 1)
        self.assertEqual(written.call_count, 1)
        self.assertEqual(written.call_args.args[1], b"rainbow\n")
        self.assertEqual(closed.call_count, 1)
        self.assertEqual(target.read_text(), "rainbow\n")

    def test_production_writer_explains_suspend_and_reprobe_failures(self):
        cases = (
            (errno.EBUSY, "suspended; retry after resume"),
            (errno.ENOENT, "interface disappeared; refresh and retry"),
            (errno.ENODEV, "interface disappeared; refresh and retry"),
        )
        for error_number, message in cases:
            with self.subTest(error_number=error_number), mock.patch.object(
                    rgb.os, "open",
                    side_effect=OSError(error_number, os.strerror(error_number))):
                with self.assertRaisesRegex(RuntimeError, message):
                    rgb.RGBController._write_evo_attribute(
                        self.root / "missing", "rainbow")


if __name__ == "__main__":
    unittest.main()
