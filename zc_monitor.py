#!/usr/bin/env python3
"""Monitor Zerocoda for Bande Nere appointment availability."""

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
    "&zc_p=45.45877%2C9.157912&zc_s=a"
)

# This is a public key embedded in the Zerocoda page, not a private credential.
DEFAULT_API_KEY = "HV2UQJ5ITCWCPU3F6RTTESDFY3TOUZP0PWQ5YGIH14UVTSXRHC"

SERVICE_QUERY = "Scelta / Revoca / Cambio del medico"
FACILITY_ID = 2061
FACILITY_NAME = "Bande Nere - Ufficio Scelta e Revoca"
SERVICE_ID = 30552


@dataclass(frozen=True)
class Availability:
    available: bool
    message: str
    details: dict[str, Any]


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


def find_service_card(api_key: str) -> dict[str, Any] | None:
    data = request_json(
        "/book/v2/services",
        {
            "q": SERVICE_QUERY,
            "p": "45.45877,9.157912",
            "s": "a",
            "c": 20,
        },
        api_key,
    )
    for service in data.get("results", []):
        if service.get("facilityId") == FACILITY_ID:
            return service
    return None


def find_slots(api_key: str, days: int) -> list[dict[str, Any]]:
    today = date.today()
    data = request_json(
        f"/book/v1/calendars/{SERVICE_ID}",
        {
            "fid": FACILITY_ID,
            "since": today.isoformat(),
            "until": (today + timedelta(days=days)).isoformat(),
            "st": 1,
        },
        api_key,
    )
    return data.get("slotDays", [])


def first_slot_summary(slot_days: list[dict[str, Any]]) -> str:
    first_day = slot_days[0]
    day = first_day.get("date") or first_day.get("day") or "unknown date"
    slots = first_day.get("slots") or first_day.get("slotTimes") or []
    if slots:
        first_slot = slots[0]
        if isinstance(first_slot, dict):
            time_label = first_slot.get("time") or first_slot.get("from") or first_slot.get("start")
        else:
            time_label = str(first_slot)
        if time_label:
            return f"{day} at {time_label}"
    return str(day)


def booking_message(availability: Availability) -> str:
    return f"{availability.message}\n{BOOKING_URL}"


def check_availability(api_key: str, days: int) -> Availability:
    service = find_service_card(api_key)
    if not service:
        return Availability(False, f"{FACILITY_NAME} was not found in search results.", {})

    early = service.get("earlyAvailability")
    if early:
        return Availability(
            True,
            f"Appointment available at {FACILITY_NAME}: {early}",
            {"source": "service-search", "service": service},
        )

    slot_days = find_slots(api_key, days)
    if slot_days:
        return Availability(
            True,
            f"Appointment available at {FACILITY_NAME}: {first_slot_summary(slot_days)}",
            {"source": "calendar", "slotDays": slot_days},
        )

    return Availability(False, f"No availability at {FACILITY_NAME}.", {"service": service})


def simulated_availability() -> Availability:
    return Availability(
        True,
        f"TEST: Appointment available at {FACILITY_NAME}: simulated first slot",
        {"source": "simulation"},
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


def save_state(state_file: Path, availability: Availability, alerted: bool = False) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps(
            {
                "available": availability.available,
                "message": availability.message,
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

    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {availability.message}")
    if availability.available:
        print(f"Book here: {BOOKING_URL}")
        should_alert = args.notify or not was_available
        if should_alert:
            notifier(
                "Zerocoda appointment available",
                booking_message(availability),
                require_telegram=args.require_telegram,
            )
        save_state(args.state_file, availability, alerted=should_alert)
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
        if not send_telegram(f"Zerocoda monitor test for {FACILITY_NAME}"):
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

    print(f"Monitoring {FACILITY_NAME} every {args.interval} seconds. Press Ctrl-C to stop.")
    while True:
        try:
            run_once(args)
        except Exception as exc:
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - check failed: {exc}", file=sys.stderr)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
