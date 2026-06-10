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


def offered_service(service_id=30572, facility_id=2067, name="First offered location", address="First offered address"):
    return zc_monitor.OfferedService(
        service_id=service_id,
        facility_id=facility_id,
        facility_name=name,
        facility_address=address,
        service={"id": service_id, "facilityId": facility_id},
    )


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
    def test_find_service_cards_uses_all_offered_locations_from_search_results(self):
        response = {
            "results": [
                {"id": 30572, "facilityId": 2067},
                {"id": 30552, "facilityId": 2061},
            ],
            "references": {
                "facility$2067": {
                    "name": "First offered location",
                    "location": {"address": "First offered address"},
                },
                "facility$2061": {
                    "name": "Second offered location",
                    "location": {"address": "Second offered address"},
                },
            },
        }

        with mock.patch.object(zc_monitor, "request_json", return_value=response) as request_json:
            services = zc_monitor.find_service_cards("api-key")

        self.assertEqual([service.facility_id for service in services], [2067, 2061])
        self.assertEqual(services[0].facility_name, "First offered location")
        request_json.assert_called_once_with(
            "/book/v2/services",
            {
                "q": zc_monitor.SERVICE_QUERY,
                "p": zc_monitor.SEARCH_POINT,
                "s": "d",
                "c": zc_monitor.SEARCH_LIMIT,
            },
            "api-key",
        )

    def test_check_availability_uses_calendar_slots_for_any_offered_location(self):
        first_location = offered_service()
        second_location = offered_service(
            service_id=30552,
            facility_id=2061,
            name="Second offered location",
            address="Second offered address",
        )

        def slots_for_service(_api_key, service, _days):
            if service.facility_id == 2067:
                return [{"date": "2026-06-20", "slots": [{"time": "09:15"}]}]
            return []

        with mock.patch.object(
            zc_monitor,
            "find_service_cards",
            return_value=[first_location, second_location],
        ), mock.patch.object(zc_monitor, "find_slots", side_effect=slots_for_service):
            availability = zc_monitor.check_availability("api-key", 180)

        self.assertTrue(availability.available)
        self.assertIn("First offered location", availability.message)
        self.assertIn("2026-06-20 at 09:15", availability.message)
        self.assertEqual(availability.details["source"], "offered-locations")
        self.assertIn("slot:2067:30572:2026-06-20:09:15", availability.details["availabilityKeys"])

    def test_check_availability_reports_not_available_without_slots(self):
        with mock.patch.object(
            zc_monitor,
            "find_service_cards",
            return_value=[offered_service()],
        ), mock.patch.object(zc_monitor, "find_slots", return_value=[]):
            availability = zc_monitor.check_availability("api-key", 180)

        self.assertFalse(availability.available)
        self.assertIn("No availability", availability.message)
        self.assertIn("1 offered locations", availability.message)


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
            self.assertEqual(state["alertedAvailabilityKeys"], ["simulation:first-offered-location:first-slot"])

    def test_existing_alerted_slot_does_not_notify_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            state_file.write_text(
                json.dumps(
                    {
                        "available": True,
                        "alertedAvailabilityKeys": ["slot:2067:30572:2026-06-20:09:15"],
                    }
                )
            )
            calls = []
            available = zc_monitor.Availability(
                True,
                "Appointment available",
                {"availabilityKeys": ["slot:2067:30572:2026-06-20:09:15"]},
            )

            with mock.patch.object(zc_monitor, "check_availability", return_value=available):
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = zc_monitor.run_once(
                        args(state_file=state_file),
                        notifier=lambda *call_args, **call_kwargs: calls.append((call_args, call_kwargs)),
                    )

            self.assertEqual(exit_code, 0)
            self.assertEqual(calls, [])
            state = json.loads(state_file.read_text())
            self.assertEqual(state["alertedAvailabilityKeys"], ["slot:2067:30572:2026-06-20:09:15"])

    def test_new_available_slot_notifies_even_when_another_slot_was_previously_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            state_file.write_text(
                json.dumps(
                    {
                        "available": True,
                        "alertedAvailabilityKeys": ["slot:2061:30552:2026-06-20:09:15"],
                    }
                )
            )
            calls = []
            available = zc_monitor.Availability(
                True,
                "Appointment available",
                {
                    "availabilityKeys": [
                        "slot:2061:30552:2026-06-20:09:15",
                        "slot:2067:30572:2026-06-21:10:00",
                    ]
                },
            )

            with mock.patch.object(zc_monitor, "check_availability", return_value=available):
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = zc_monitor.run_once(
                        args(state_file=state_file),
                        notifier=lambda *call_args, **call_kwargs: calls.append((call_args, call_kwargs)),
                    )

            self.assertEqual(exit_code, 0)
            self.assertEqual(len(calls), 1)
            state = json.loads(state_file.read_text())
            self.assertEqual(
                state["alertedAvailabilityKeys"],
                [
                    "slot:2061:30552:2026-06-20:09:15",
                    "slot:2067:30572:2026-06-21:10:00",
                ],
            )

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
