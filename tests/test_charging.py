import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

import charging


BATTERY_NORMAL = """mode=normal
capacity=42
charge_behaviour=[auto] inhibit-charge
start_threshold=95
end_threshold=100
status=Discharging
"""

BATTERY_LIMIT_50 = """mode=limit
limit=50
capacity=50
charge_behaviour=auto [inhibit-charge]
start_threshold=50
end_threshold=55
status=Full
"""

BATTERY_BYPASS = """mode=bypass
capacity=42
charge_behaviour=auto [inhibit-charge]
start_threshold=75
end_threshold=80
status=Discharging
"""

PUMP_OFF = """enabled=0
profile=normal
state=idle
last_error=0
last_end_reason=none
requested_voltage_uv=0
usb_online=1
usb_type=USB [PD_PPS]
charge_behaviour=[auto] inhibit-charge
master_online=0
master_health=Good
slave_online=0
slave_health=Good
"""

PUMP_ACTIVE = """enabled=1
profile=fast
state=pump
last_error=0
last_end_reason=none
requested_voltage_uv=11000000
usb_online=1
usb_type=USB [PD_PPS]
charge_behaviour=[auto] inhibit-charge
master_online=1
master_health=Good
slave_online=1
slave_health=Good
"""


def input_power(path, valid, microwatts):
    return (f"input_power_path={path}\n"
            f"input_power_valid={valid}\n"
            f"input_power_uw={microwatts}\n")


def executable(path, body):
    path.write_text("#!/bin/sh\nset -eu\n" + body)
    path.chmod(0o755)
    return path


class ChargingParserTests(unittest.TestCase):
    def result(self, stdout):
        return {
            "command": ["helper", "status"], "started": True, "ok": True,
            "timed_out": False, "exit_status": 0, "stdout": stdout,
            "stderr": "",
        }

    def test_limit_50_and_full_is_an_inhibited_limit_not_bypass(self):
        parsed = charging._parse_battery(
            self.result(BATTERY_LIMIT_50), True, 123.0)

        self.assertTrue(parsed["valid"])
        self.assertFalse(parsed["transitional"])
        self.assertEqual(parsed["mode"], "limit")
        self.assertEqual(parsed["limit"], 50)
        self.assertEqual(parsed["charge_behaviour"], "inhibit-charge")
        self.assertEqual(parsed["battery_status"], "Full")

    def test_contradictory_battery_snapshot_is_transitional(self):
        output = BATTERY_NORMAL.replace(
            "charge_behaviour=[auto] inhibit-charge",
            "charge_behaviour=auto [inhibit-charge]")
        parsed = charging._parse_battery(self.result(output), True, 123.0)

        self.assertTrue(parsed["valid"])
        self.assertTrue(parsed["transitional"])
        self.assertIn("Normal policy", parsed["transition_reason"])

    def test_complete_dual_pump_state_is_active(self):
        parsed = charging._parse_pump(
            self.result(PUMP_ACTIVE), True, 123.0)

        self.assertTrue(parsed["valid"])
        self.assertEqual(parsed["phase"], "active")

    def test_active_parser_requires_source_behaviour_and_both_healthy_pumps(self):
        output = PUMP_ACTIVE.replace("usb_online=1", "usb_online=0").replace(
            "usb_type=USB [PD_PPS]", "usb_type=USB [PD]").replace(
            "charge_behaviour=[auto] inhibit-charge",
            "charge_behaviour=auto [inhibit-charge]")

        parsed = charging._parse_pump(self.result(output), True, 123.0)

        self.assertTrue(parsed["valid"])
        self.assertTrue(parsed["transitional"])
        self.assertEqual(parsed["phase"], "transitional")
        self.assertIn("source loss", parsed["transition_reason"])
        self.assertIn("PD-PPS", parsed["transition_reason"])
        self.assertIn("auto charging behaviour", parsed["transition_reason"])

    def test_nonzero_error_takes_precedence_over_profile(self):
        output = PUMP_ACTIVE.replace("last_error=0", "last_error=-5")
        parsed = charging._parse_pump(self.result(output), True, 123.0)

        self.assertTrue(parsed["valid"])
        self.assertEqual(parsed["phase"], "error")

    def test_pair_reconciliation_preserves_a_coherent_active_snapshot(self):
        battery = charging._parse_battery(
            self.result(BATTERY_NORMAL), True, 123.0)
        pump = charging._parse_pump(
            self.result(PUMP_ACTIVE), True, 123.0)

        charging._reconcile_status_pair(battery, pump)

        self.assertFalse(battery["transitional"])
        self.assertFalse(pump["transitional"])
        self.assertEqual(pump["phase"], "active")

    def test_pair_reconciliation_rejects_bypass_plus_active(self):
        battery = charging._parse_battery(
            self.result(BATTERY_BYPASS), True, 123.0)
        pump = charging._parse_pump(
            self.result(PUMP_ACTIVE), True, 123.0)

        charging._reconcile_status_pair(battery, pump)

        self.assertTrue(battery["transitional"])
        self.assertTrue(pump["transitional"])
        self.assertEqual(pump["phase"], "transitional")
        self.assertIn("inhibited battery policy", pump["transition_reason"])

    def test_pair_reconciliation_rejects_mismatched_behaviour(self):
        battery = charging._parse_battery(
            self.result(BATTERY_NORMAL), True, 123.0)
        output = PUMP_ACTIVE.replace(
            "charge_behaviour=[auto] inhibit-charge",
            "charge_behaviour=auto [inhibit-charge]")
        pump = charging._parse_pump(self.result(output), True, 123.0)

        charging._reconcile_status_pair(battery, pump)

        self.assertTrue(battery["transitional"])
        self.assertEqual(pump["phase"], "transitional")
        self.assertIn("different charging behaviour", pump["transition_reason"])

    def test_pair_reconciliation_requires_online_pd_pps_for_active(self):
        battery = charging._parse_battery(
            self.result(BATTERY_NORMAL), True, 123.0)
        output = PUMP_ACTIVE.replace("usb_online=1", "usb_online=0").replace(
            "usb_type=USB [PD_PPS]", "usb_type=USB [PD]")
        pump = charging._parse_pump(self.result(output), True, 123.0)

        charging._reconcile_status_pair(battery, pump)

        self.assertEqual(pump["phase"], "transitional")
        self.assertIn("source loss", pump["transition_reason"])
        self.assertIn("PD-PPS", pump["transition_reason"])

    def test_invalid_battery_cannot_skip_active_pump_source_checks(self):
        battery = charging._parse_battery(
            self.result("mode=normal\ncapacity=\n"), True, 123.0)
        pump = charging._parse_pump(
            self.result(PUMP_ACTIVE), True, 123.0)
        # Exercise the pair boundary independently of the parser guard.
        pump["usb_online"] = False
        pump["usb_type"] = "PD"

        charging._reconcile_status_pair(battery, pump)

        self.assertFalse(battery["valid"])
        self.assertTrue(pump["valid"])
        self.assertTrue(pump["transitional"])
        self.assertEqual(pump["phase"], "transitional")
        self.assertIn("source loss", pump["transition_reason"])
        self.assertIn("PD-PPS", pump["transition_reason"])

    def test_valid_usb_input_power_tuples_are_parsed_atomically(self):
        offline = PUMP_OFF.replace("usb_online=1", "usb_online=0")
        starting = PUMP_ACTIVE.replace("state=pump", "state=pump-init")
        cases = (
            (PUMP_OFF, "qcom", "1", "39681000", True, "39681000"),
            (PUMP_OFF, "qcom", "1", "0", True, "0"),
            (PUMP_OFF, "qcom", "1", "9223372036854775807", True,
             "9223372036854775807"),
            (PUMP_ACTIVE, "dual-pump", "1", "25000000", True, "25000000"),
            (PUMP_ACTIVE, "dual-pump", "1", "0", True, "0"),
            (offline, "offline", "0", "0", False, None),
            (starting, "transition", "0", "0", False, None),
            (PUMP_OFF, "unavailable", "0", "0", False, None),
        )

        for base, path, valid, microwatts, measured, expected in cases:
            with self.subTest(path=path, valid=valid, microwatts=microwatts):
                parsed = charging._parse_pump(
                    self.result(base + input_power(path, valid, microwatts)),
                    True, 123.0)

                self.assertTrue(parsed["valid"])
                self.assertTrue(parsed["input_power"]["available"])
                self.assertEqual(parsed["input_power"]["path"], path)
                self.assertEqual(parsed["input_power"]["valid"], measured)
                self.assertEqual(parsed["input_power"]["microwatts"], expected)
                self.assertEqual(parsed["input_power"]["error"], "")

    def test_legacy_helper_without_input_power_keeps_base_pump_valid(self):
        parsed = charging._parse_pump(
            self.result(PUMP_OFF), True, 123.0)

        self.assertTrue(parsed["valid"])
        self.assertEqual(parsed["phase"], "off")
        self.assertFalse(parsed["input_power"]["available"])
        self.assertFalse(parsed["input_power"]["valid"])
        self.assertIsNone(parsed["input_power"]["microwatts"])
        self.assertEqual(parsed["input_power"]["error"], "")

    def test_malformed_input_power_invalidates_only_optional_telemetry(self):
        offline = PUMP_OFF.replace("usb_online=1", "usb_online=0")
        cases = (
            PUMP_OFF + "input_power_path=qcom\ninput_power_valid=1\n",
            PUMP_OFF + "input_power_path qcom\n" +
            "input_power_valid=1\ninput_power_uw=1\n",
            PUMP_OFF + input_power("qcom", "1", "1") +
            "input_power_uw=2\n",
            PUMP_OFF + input_power("other", "1", "1"),
            PUMP_OFF + input_power("qcom", "true", "1"),
            PUMP_OFF + input_power("qcom", "1", "-1"),
            PUMP_OFF + input_power("qcom", "1", "9223372036854775808"),
            PUMP_OFF + input_power("qcom", "0", "0"),
            PUMP_OFF + input_power("offline", "0", "1"),
            PUMP_OFF + input_power("offline", "0", "0"),
            offline + input_power("qcom", "1", "1"),
            PUMP_OFF + input_power("dual-pump", "1", "25000000"),
        )

        for output in cases:
            with self.subTest(output=output.splitlines()[-3:]):
                parsed = charging._parse_pump(
                    self.result(output), True, 123.0)

                self.assertTrue(parsed["valid"])
                self.assertEqual(parsed["phase"], "off")
                self.assertFalse(parsed["input_power"]["available"])
                self.assertFalse(parsed["input_power"]["valid"])
                self.assertEqual(parsed["input_power"]["path"], "unavailable")
                self.assertIsNone(parsed["input_power"]["microwatts"])
                self.assertTrue(parsed["input_power"]["error"])


class ChargingBoundaryTests(unittest.TestCase):
    def test_public_wrapper_unsupported_result_is_detected(self):
        result = {
            "ok": False,
            "stderr": "Charging-mode control is not supported on this device",
        }

        self.assertTrue(charging._reported_unsupported(result))

    def test_stale_or_transitional_bypass_is_not_live(self):
        live = {
            "available": True, "valid": True, "stale": False,
            "transitional": False, "mode": "bypass",
        }

        self.assertTrue(charging._live_bypass(live))
        self.assertFalse(charging._live_bypass({**live, "stale": True}))
        self.assertFalse(charging._live_bypass({**live, "transitional": True}))
        self.assertFalse(charging._live_bypass({**live, "available": False}))

    def test_input_power_adds_no_private_or_direct_hardware_fallback(self):
        source = Path(charging.__file__).read_text()

        for forbidden in (
                "qcom-battmgr-usb", "hl7139_master", "hl7139_slave",
                "KPFE-CHARGE-MONITOR", "systemd-run", "/usr/lib/autostart"):
            self.assertNotIn(forbidden, source)

    def test_battery_temperature_is_signed_tenths_celsius(self):
        with tempfile.TemporaryDirectory() as temporary:
            temperature = Path(temporary) / "temp"
            for raw, expected in (("360\n", 360), ("-15\n", -15),
                                  ("", None), ("36.0\n", None),
                                  ("unknown\n", None)):
                with self.subTest(raw=raw):
                    temperature.write_text(raw)
                    self.assertEqual(
                        charging._read_battery_temperature(temperature), expected)
            temperature.unlink()
            self.assertIsNone(charging._read_battery_temperature(temperature))
            temperature.write_bytes(b"\xff")
            self.assertIsNone(charging._read_battery_temperature(temperature))


class ChargingControllerTests(unittest.TestCase):
    @staticmethod
    def coherent_bypass_status():
        return {
            "captured_at": 123.0,
            "coherent": True,
            "battery": {
                "available": True, "valid": True, "stale": False,
                "transitional": False, "mode": "bypass",
            },
            "pump": {
                "available": True, "valid": True, "stale": False,
                "transitional": False, "phase": "off",
            },
            "operation": None,
        }

    def helpers(self, root, battery_mutation_exit=0):
        battery_log = root / "battery.log"
        pump_log = root / "pump.log"
        environment_log = root / "environment.log"
        battery = executable(root / "charging_mode", f'''
printf '%s\\n' "$*" >> "{battery_log}"
printf '%s\\n' "${{ROCKNIX_FUNCTIONS-unset}}|${{BATTERY-unset}}|${{USB-unset}}|${{MASTER-unset}}|${{SLAVE-unset}}|${{COORDINATOR-unset}}|${{QUIRK_DEVICE-unset}}|${{HW_DEVICE-unset}}" > "{environment_log}"
if [ "$1" = status ]; then
  printf '%s' '{BATTERY_NORMAL}'
  exit 0
fi
exit {battery_mutation_exit}
''')
        pump = executable(root / "kpfe_fast_charge", f'''
printf '%s\\n' "$*" >> "{pump_log}"
if [ "$1" = status ]; then
  printf '%s' '{PUMP_OFF}'
  exit 0
fi
exit 0
''')
        return battery, pump, battery_log, pump_log, environment_log

    def test_exact_limit_command_and_two_follow_up_statuses(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            battery, pump, battery_log, pump_log, environment_log = self.helpers(root)
            controller = charging.ChargingController(root, battery, pump)

            result = controller.set_battery_policy("limit", 80)

            self.assertEqual(
                battery_log.read_text().splitlines(), ["limit 80", "status"])
            self.assertEqual(pump_log.read_text().splitlines(), ["status"])
            self.assertEqual(result["operation"]["command"], [str(battery), "limit", "80"])
            self.assertTrue(result["operation"]["ok"])
            self.assertTrue(result["battery"]["valid"])
            self.assertTrue(result["pump"]["valid"])
            self.assertTrue(result["coherent"])
            self.assertEqual(environment_log.read_text().strip(), "unset|unset|unset|unset|unset|unset|unset|unset")

    def test_status_serializes_one_current_battery_temperature_sample(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            battery, pump, _, _, _ = self.helpers(root)
            temperature = root / "battery-temp"
            temperature.write_text("360\n")
            controller = charging.ChargingController(
                root, battery, pump, temperature)

            status = controller.get_status()

            self.assertEqual(status["battery_temperature_deci_c"], 360)
            temperature.write_text("malformed\n")
            self.assertIsNone(
                controller.get_status()["battery_temperature_deci_c"])

    def test_failed_mutation_is_not_retried_and_status_still_refreshes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            battery, pump, battery_log, pump_log, _ = self.helpers(
                root, battery_mutation_exit=1)
            controller = charging.ChargingController(root, battery, pump)

            result = controller.set_battery_policy("bypass")

            self.assertEqual(
                battery_log.read_text().splitlines(), ["bypass", "status"])
            self.assertEqual(pump_log.read_text().splitlines(), ["status"])
            self.assertFalse(result["operation"]["ok"])
            self.assertTrue(result["battery"]["valid"])

    def test_incoherent_latest_pair_never_reports_cached_bypass(self):
        with tempfile.TemporaryDirectory() as temporary:
            controller = charging.ChargingController(temporary)
            controller.latest_status = self.coherent_bypass_status()
            self.assertTrue(controller.cached_bypass_active())

            controller.latest_status["coherent"] = False

            self.assertFalse(controller.cached_bypass_active())

    def test_risk_confirmation_is_fresh_and_exact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            battery, pump, _, pump_log, _ = self.helpers(root)
            controller = charging.ChargingController(root, battery, pump)

            with self.assertRaises(ValueError):
                controller.set_pump_profile("fast", False)
            self.assertFalse(pump_log.exists())

            result = controller.set_pump_profile("fast", True)

            self.assertEqual(pump_log.read_text().splitlines(), [
                "enable fast --acknowledge-experimental-risk", "status"])
            self.assertEqual(result["operation"]["command"], [
                str(pump), "enable", "fast",
                "--acknowledge-experimental-risk"])

    def test_invalid_values_start_no_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            battery, pump, battery_log, pump_log, _ = self.helpers(root)
            controller = charging.ChargingController(root, battery, pump)

            for mode, limit in (("limit", 75), ("limit", "bad"),
                                ("limit", 80.5), ("normal", 80)):
                with self.assertRaises(ValueError):
                    controller.set_battery_policy(mode, limit)
            with self.assertRaises(ValueError):
                controller.set_pump_profile("turbo", True)

            self.assertFalse(battery_log.exists())
            self.assertFalse(pump_log.exists())

    def test_last_good_status_is_retained_and_marked_stale(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            battery, pump, _, _, _ = self.helpers(root)
            controller = charging.ChargingController(root, battery, pump)
            first = controller.get_status()
            self.assertTrue(first["battery"]["valid"])
            self.assertTrue(first["coherent"])
            executable(battery, "printf 'mode=normal\\ncapacity=\\n'\n")

            second = controller.get_status()

            self.assertTrue(second["battery"]["valid"])
            self.assertTrue(second["battery"]["stale"])
            self.assertFalse(second["coherent"])
            self.assertIn("missing or empty", second["battery"]["refresh_error"])
            self.assertEqual(second["battery"]["captured_at"], first["battery"]["captured_at"])

    def test_stale_status_never_retains_previous_usb_input_wattage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            battery, pump, _, _, _ = self.helpers(root)
            executable(
                pump,
                f"printf '%s' '{PUMP_OFF + input_power('qcom', '1', '39681000')}'\n",
            )
            controller = charging.ChargingController(root, battery, pump)

            first = controller.get_status()
            self.assertTrue(first["coherent"])
            self.assertEqual(
                first["pump"]["input_power"]["microwatts"], "39681000")
            executable(pump, "printf 'enabled=\\n'\n")

            second = controller.get_status()

            self.assertFalse(second["coherent"])
            self.assertTrue(second["pump"]["stale"])
            self.assertTrue(second["pump"]["input_power"]["stale"])
            self.assertFalse(second["pump"]["input_power"]["valid"])
            self.assertIsNone(second["pump"]["input_power"]["microwatts"])

    def test_optional_telemetry_failure_does_not_disable_coherent_controls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            battery, pump, _, _, _ = self.helpers(root)
            executable(
                pump,
                f"printf '%s' '{PUMP_OFF}input_power_path=qcom\\n'\n",
            )
            controller = charging.ChargingController(root, battery, pump)

            status = controller.get_status()

            self.assertTrue(status["coherent"])
            self.assertTrue(status["pump"]["valid"])
            self.assertFalse(status["pump"]["input_power"]["available"])
            self.assertIn("partial", status["pump"]["input_power"]["error"])

    def test_incoherent_pair_discards_fresh_usb_input_wattage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            battery, pump, _, _, _ = self.helpers(root)
            executable(
                pump,
                f"printf '%s' '{PUMP_OFF.replace('charge_behaviour=[auto] inhibit-charge', 'charge_behaviour=auto [inhibit-charge]') + input_power('qcom', '1', '39681000')}'\n",
            )
            controller = charging.ChargingController(root, battery, pump)

            status = controller.get_status()

            self.assertFalse(status["coherent"])
            self.assertFalse(status["pump"]["input_power"]["available"])
            self.assertFalse(status["pump"]["input_power"]["valid"])
            self.assertIsNone(status["pump"]["input_power"]["microwatts"])

    def test_unavailable_helpers_never_fabricate_usb_input_wattage(self):
        with tempfile.TemporaryDirectory() as temporary:
            status = charging.ChargingController(temporary).get_status()

            self.assertFalse(status["coherent"])
            self.assertFalse(status["pump"]["available"])
            self.assertFalse(status["pump"]["input_power"]["available"])
            self.assertFalse(status["pump"]["input_power"]["valid"])
            self.assertIsNone(status["pump"]["input_power"]["microwatts"])

    def test_timeout_terminates_and_reaps_the_helper(self):
        with tempfile.TemporaryDirectory() as temporary:
            helper = executable(Path(temporary) / "slow-helper", "sleep 30\n")
            started = time.monotonic()

            result = charging._execute(helper, ["status"], 0.05)

            self.assertTrue(result["timed_out"])
            self.assertFalse(result["ok"])
            self.assertLess(time.monotonic() - started, 2)

    def test_file_lock_serializes_poll_and_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = root / "events.log"
            battery = executable(root / "charging_mode", f'''
echo "battery-$1-start" >> "{events}"
sleep 0.1
if [ "$1" = status ]; then printf '%s' '{BATTERY_NORMAL}'; fi
echo "battery-$1-end" >> "{events}"
''')
            pump = executable(root / "kpfe_fast_charge", f'''
echo "pump-$1-start" >> "{events}"
sleep 0.1
if [ "$1" = status ]; then printf '%s' '{PUMP_OFF}'; fi
echo "pump-$1-end" >> "{events}"
''')
            first = charging.ChargingController(root, battery, pump)
            second = charging.ChargingController(root, battery, pump)
            threads = [
                threading.Thread(target=first.get_status),
                threading.Thread(target=lambda: second.set_battery_policy("normal")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            lines = events.read_text().splitlines()
            # Each transaction is three contiguous helper executions. If the
            # file lock failed, starts from both transactions would interleave.
            starts = [line for line in lines if line.endswith("-start")]
            self.assertIn(starts, ([
                "battery-status-start", "pump-status-start",
                "battery-normal-start", "battery-status-start", "pump-status-start",
            ], [
                "battery-normal-start", "battery-status-start", "pump-status-start",
                "battery-status-start", "pump-status-start",
            ]))


if __name__ == "__main__":
    unittest.main()
