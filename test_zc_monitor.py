import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import zc_monitor


def args(**overrides):
    defaults = {
        "days": 180,
        "exit_zero_if_unavailable": False,
        "notify": False,
        "require_telegram": False,
        "simulate_available": False,
        "state_file": Path("unused-state.json"),
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class SlotFormattingTests(unittest.TestCase):
    def test_first_slot_summary_uses_first_day_and_time(self):
        slot_days = [{"date": "2026-06-20", "slots": [{"time": "09:15"}, {"time": "09:30"}]}]

        self.assertEqual(zc_monitor.first_slot_summary(slot_days), "2026-06-20 at 09:15")

    def test_booking_message_includes_booking_url(self):
        availability = zc_monitor.Availability(True, "Appointment available", {})

        message = zc_monitor.booking_message(availability)

        self.assertIn("Appointment available", message)
        self.assertIn(zc_monitor.BOOKING_URL, message)


class AvailabilityTests(unittest.TestCase):
    def test_check_availability_uses_calendar_slots_when_service_has_no_early_availability(self):
        with mock.patch.object(
            zc_monitor,
            "find_service_card",
            return_value={"facilityId": zc_monitor.FACILITY_ID},
        ), mock.patch.object(
            zc_monitor,
            "find_slots",
            return_value=[{"date": "2026-06-20", "slots": [{"time": "09:15"}]}],
        ):
            availability = zc_monitor.check_availability("api-key", 180)

        self.assertTrue(availability.available)
        self.assertIn("2026-06-20 at 09:15", availability.message)
        self.assertEqual(availability.details["source"], "calendar")

    def test_check_availability_reports_not_available_without_slots(self):
        with mock.patch.object(
            zc_monitor,
            "find_service_card",
            return_value={"facilityId": zc_monitor.FACILITY_ID},
        ), mock.patch.object(zc_monitor, "find_slots", return_value=[]):
            availability = zc_monitor.check_availability("api-key", 180)

        self.assertFalse(availability.available)
        self.assertIn("No availability", availability.message)


class StateAndAlertTests(unittest.TestCase):
    def test_failed_required_alert_does_not_save_available_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"

            def failing_notifier(*_args, **_kwargs):
                raise RuntimeError("Telegram notification was required")

            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
                zc_monitor.run_once(
                    args(simulate_available=True, notify=True, require_telegram=True, state_file=state_file),
                    notifier=failing_notifier,
                )

            self.assertFalse(state_file.exists())

    def test_successful_alert_saves_last_alerted_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            calls = []

            def successful_notifier(*call_args, **call_kwargs):
                calls.append((call_args, call_kwargs))

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = zc_monitor.run_once(
                    args(simulate_available=True, notify=True, state_file=state_file),
                    notifier=successful_notifier,
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(len(calls), 1)
            state = json.loads(state_file.read_text())
            self.assertTrue(state["available"])
            self.assertIsNotNone(state["lastAlertedAt"])

    def test_unavailable_ci_mode_saves_clean_state_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            unavailable = zc_monitor.Availability(False, "No availability", {})

            with mock.patch.object(zc_monitor, "check_availability", return_value=unavailable):
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = zc_monitor.run_once(args(exit_zero_if_unavailable=True, state_file=state_file))

            self.assertEqual(exit_code, 0)
            state = json.loads(state_file.read_text())
            self.assertFalse(state["available"])
            self.assertIsNone(state["lastAlertedAt"])


if __name__ == "__main__":
    unittest.main()
