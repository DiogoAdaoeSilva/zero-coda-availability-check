#!/usr/bin/env python3
"""Monitor Zerocoda for appointment availability across offered locations."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any


API_BASE = "https://api-asst-santipaolocarlo.zerocoda.it"
SITE_ORIGIN = "https://asst-santipaolocarlo.zerocoda.it"
BOOKING_URL = (
    "https://asst-santipaolocarlo.zerocoda.it/prenotazione/"
    "?zc_f=Scelta+%2F+Revoca+%2F+Cambio+del+medico"
    "&zc_p=45.45877%2C9.157912&zc_s=d"
)

# This is a public key embedded in the Zerocoda page, not a private credential.
DEFAULT_API_KEY = "HV2UQJ5ITCWCPU3F6RTTESDFY3TOUZP0PWQ5YGIH14UVTSXRHC"

SERVICE_QUERY = "Scelta / Revoca / Cambio del medico"
SEARCH_POINT = "45.45877,9.157912"
SEARCH_SORT = "d"
SEARCH_LIMIT = 50
MONITOR_NAME = "Scelta / Revoca / Cambio del medico offered locations"


@dataclass(frozen=True)
class Availability:
    available: bool
    message: str
    details: dict[str, Any]


@dataclass(frozen=True)
class OfferedService:
    service_id: int
    facility_id: int
    facility_name: str
    facility_address: str
    service: dict[str, Any]


def request_json(path: str, params: dict[str, Any], api_key: str) -> dict[str, Any]:
    query = urllib.parse.urlencode(params, doseq=True)
    url = f"{API_BASE}{path}?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Origin": SITE_ORIGIN,
            "Referer": f"{SITE_ORIGIN}/prenotazione/",
            "User-Agent": "Mozilla/5.0 appointment-monitor/1.0",
            "X-Api-Key": api_key,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc

    data = json.loads(payload)
    if isinstance(data, dict) and data.get("code") and data.get("code") != 200:
        raise RuntimeError(f"API error from {url}: {data}")
    return data


def facility_reference(data: dict[str, Any], facility_id: int) -> dict[str, Any]:
    reference = data.get("references", {}).get(f"facility${facility_id}")
    return reference if isinstance(reference, dict) else {}


def facility_label(facility: dict[str, Any], facility_id: int) -> tuple[str, str]:
    name = facility.get("name") or f"Facility {facility_id}"
    location = facility.get("location") if isinstance(facility.get("location"), dict) else {}
    address = location.get("address") or ""
    return name, address


def find_service_cards(api_key: str) -> list[OfferedService]:
    data = request_json(
        "/book/v2/services",
        {
            "q": SERVICE_QUERY,
            "p": SEARCH_POINT,
            "s": SEARCH_SORT,
            "c": SEARCH_LIMIT,
        },
        api_key,
    )
    services: list[OfferedService] = []
    for service in data.get("results", []):
        service_id = service.get("id") or service.get("functionId")
        facility_id = service.get("facilityId")
        if not service_id or not facility_id:
            continue

        facility = facility_reference(data, int(facility_id))
        facility_name, facility_address = facility_label(facility, int(facility_id))
        services.append(
            OfferedService(
                service_id=int(service_id),
                facility_id=int(facility_id),
                facility_name=facility_name,
                facility_address=facility_address,
                service=service,
            )
        )
    return services


def find_slots(api_key: str, service: OfferedService, days: int) -> list[dict[str, Any]]:
    today = date.today()
    data = request_json(
        f"/book/v1/calendars/{service.service_id}",
        {
            "fid": service.facility_id,
            "since": today.isoformat(),
            "until": (today + timedelta(days=days)).isoformat(),
            "st": 1,
        },
        api_key,
    )
    return data.get("slotDays", [])


def slot_time_label(slot: Any) -> str | None:
    if isinstance(slot, dict):
        return slot.get("time") or slot.get("from") or slot.get("start")
    if slot:
        return str(slot)
    return None


def first_slot_summary(slot_days: list[dict[str, Any]]) -> str:
    first_day = slot_days[0]
    day = first_day.get("date") or first_day.get("day") or "unknown date"
    slots = first_day.get("slots") or first_day.get("slotTimes") or []
    if slots:
        time_label = slot_time_label(slots[0])
        if time_label:
            return f"{day} at {time_label}"
    return str(day)


def location_summary(service: OfferedService) -> str:
    if service.facility_address:
        return f"{service.facility_name} ({service.facility_address})"
    return service.facility_name


def service_early_key(service: OfferedService, early: Any) -> str:
    return f"early:{service.facility_id}:{service.service_id}:{early}"


def slot_keys(service: OfferedService, slot_days: list[dict[str, Any]]) -> list[str]:
    keys: list[str] = []
    for slot_day in slot_days:
        day = slot_day.get("date") or slot_day.get("day") or "unknown-date"
        slots = slot_day.get("slots") or slot_day.get("slotTimes") or []
        if not slots:
            keys.append(f"day:{service.facility_id}:{service.service_id}:{day}")
            continue
        for slot in slots:
            keys.append(f"slot:{service.facility_id}:{service.service_id}:{day}:{slot_time_label(slot) or 'unknown-time'}")
    return keys


def availability_keys(availability: Availability) -> set[str]:
    keys = availability.details.get("availabilityKeys", [])
    if not isinstance(keys, list):
        return set()
    return {str(key) for key in keys}


def booking_message(availability: Availability) -> str:
    return f"{availability.message}\n{BOOKING_URL}"


def check_availability(api_key: str, days: int) -> Availability:
    services = find_service_cards(api_key)
    if not services:
        return Availability(False, "No offered locations were found in search results.", {})

    available_locations: list[dict[str, Any]] = []
    keys: list[str] = []

    for service in services:
        early = service.service.get("earlyAvailability")
        if early:
            available_locations.append(
                {
                    "source": "service-search",
                    "service": service,
                    "summary": str(early),
                }
            )
            keys.append(service_early_key(service, early))
            continue

        slot_days = find_slots(api_key, service, days)
        if slot_days:
            available_locations.append(
                {
                    "source": "calendar",
                    "service": service,
                    "summary": first_slot_summary(slot_days),
                    "slotDays": slot_days,
                }
            )
            keys.extend(slot_keys(service, slot_days))

    if available_locations:
        lines = [
            f"- {location_summary(item['service'])}: {item['summary']}"
            for item in available_locations
        ]
        return Availability(
            True,
            "Appointment available at one or more offered locations:\n" + "\n".join(lines),
            {
                "source": "offered-locations",
                "availabilityKeys": keys,
                "availableLocations": available_locations,
            },
        )

    return Availability(False, f"No availability at any of {len(services)} offered locations.", {"services": services})


def simulated_availability() -> Availability:
    return Availability(
        True,
        "TEST: Appointment available at one or more offered locations:\n"
        "- Example offered location: simulated first slot",
        {"source": "simulation", "availabilityKeys": ["simulation:first-offered-location:first-slot"]},
    )


def send_telegram(message: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram API returned HTTP {exc.code}: {body_text}") from exc
    return True


def notify(title: str, message: str, require_telegram: bool = False) -> None:
    print("\a", end="", flush=True)
    telegram_sent = send_telegram(message)
    if require_telegram and not telegram_sent:
        raise RuntimeError("Telegram notification was required, but Telegram is not configured.")
    if sys.platform == "darwin":
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification {json.dumps(message)} with title {json.dumps(title)}',
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def load_previous_available(state_file: Path) -> bool:
    try:
        return bool(json.loads(state_file.read_text()).get("available"))
    except FileNotFoundError:
        return False
    except (json.JSONDecodeError, OSError):
        return False


def load_previous_alerted_keys(state_file: Path) -> set[str]:
    try:
        data = json.loads(state_file.read_text())
    except FileNotFoundError:
        return set()
    except (json.JSONDecodeError, OSError):
        return set()

    keys = data.get("alertedAvailabilityKeys", [])
    if isinstance(keys, list):
        return {str(key) for key in keys}
    return set()


def save_state(
    state_file: Path,
    availability: Availability,
    alerted: bool = False,
    alerted_keys: set[str] | None = None,
) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    current_keys = sorted(availability_keys(availability))
    saved_alerted_keys = sorted(alerted_keys if alerted_keys is not None else (set(current_keys) if alerted else set()))
    state_file.write_text(
        json.dumps(
            {
                "available": availability.available,
                "message": availability.message,
                "availabilityKeys": current_keys,
                "alertedAvailabilityKeys": saved_alerted_keys,
                "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "lastAlertedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z") if alerted else None,
                "bookingUrl": BOOKING_URL,
            },
            indent=2,
        )
        + "\n"
    )


def run_once(args: argparse.Namespace, notifier=notify) -> int:
    if args.simulate_available:
        availability = simulated_availability()
    else:
        api_key = os.environ.get("ZC_API_KEY") or DEFAULT_API_KEY
        availability = check_availability(api_key, args.days)
    was_available = load_previous_available(args.state_file)
    previous_alerted_keys = load_previous_alerted_keys(args.state_file)
    current_keys = availability_keys(availability)

    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {availability.message}")
    if availability.available:
        print(f"Book here: {BOOKING_URL}")
        has_new_availability = bool(current_keys) and not current_keys.issubset(previous_alerted_keys)
        should_alert = args.notify or has_new_availability or (not current_keys and not was_available)
        if should_alert:
            notifier(
                "Zerocoda appointment available",
                booking_message(availability),
                require_telegram=args.require_telegram,
            )
        alerted_keys = current_keys if should_alert else current_keys.intersection(previous_alerted_keys)
        save_state(args.state_file, availability, alerted=should_alert, alerted_keys=alerted_keys)
        return 0
    save_state(args.state_file, availability)
    return 0 if args.exit_zero_if_unavailable else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true", help="keep checking until interrupted")
    parser.add_argument("--interval", type=int, default=300, help="seconds between checks in loop mode")
    parser.add_argument("--days", type=int, default=180, help="calendar days to scan as a fallback")
    parser.add_argument("--notify", action="store_true", help="send a notification even if already alerted")
    parser.add_argument(
        "--require-telegram",
        action="store_true",
        help="fail if an alert should be sent but Telegram is not configured",
    )
    parser.add_argument(
        "--simulate-available",
        action="store_true",
        help="pretend an appointment is available, useful for testing the full alert path",
    )
    parser.add_argument(
        "--exit-zero-if-unavailable",
        action="store_true",
        help="return success when no appointment is available, useful for scheduled CI checks",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="send a test Telegram message using TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(".zc-monitor-state.json"),
        help="state file used to avoid duplicate alerts",
    )
    args = parser.parse_args()

    if args.test_telegram:
        if not send_telegram(f"Zerocoda monitor test for {MONITOR_NAME}"):
            print(
                "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID before testing Telegram.",
                file=sys.stderr,
            )
            return 2
        print("Sent Telegram test message.")
        return 0

    if not args.loop:
        try:
            return run_once(args)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2

    print(f"Monitoring {MONITOR_NAME} every {args.interval} seconds. Press Ctrl-C to stop.")
    while True:
        try:
            run_once(args)
        except Exception as exc:
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - check failed: {exc}", file=sys.stderr)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
