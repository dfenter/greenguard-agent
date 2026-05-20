"""
Cal.com API v2 client for GreenGuard USA.
Handles booking lookups and rescheduling.
"""

import os
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

_API_KEY  = os.getenv("CALCOM_API_KEY", "")
_BASE     = "https://api.cal.com/v2"
_VERSION  = "2024-08-13"
_TZ       = ZoneInfo(os.getenv("CALENDAR_TIMEZONE", "America/Chicago"))


def _headers() -> dict:
    if not _API_KEY:
        raise RuntimeError("CALCOM_API_KEY not set in .env")
    return {
        "Authorization": f"Bearer {_API_KEY}",
        "cal-api-version": _VERSION,
        "Content-Type": "application/json",
    }


def list_bookings(start: datetime, end: datetime) -> list[dict]:
    """
    Return Cal.com bookings between start and end.
    Each entry: {uid, start, title, attendee_name, attendee_email, status}
    Only returns upcoming/accepted bookings.
    """
    params = {
        "startTime": start.isoformat(),
        "endTime":   end.isoformat(),
        "limit":     100,
        "status":    "upcoming",
    }
    resp = requests.get(f"{_BASE}/bookings", headers=_headers(), params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    bookings = []
    for b in data.get("data", {}).get("bookings", []):
        attendee = next((a for a in b.get("attendees", []) if not a.get("host")), {})
        bookings.append({
            "uid":            b["uid"],
            "start":          datetime.fromisoformat(b["start"]).astimezone(_TZ),
            "title":          b.get("title", ""),
            "attendee_name":  attendee.get("name", ""),
            "attendee_email": attendee.get("email", ""),
            "status":         b.get("status", ""),
        })
    return bookings


def get_booking(uid: str) -> dict | None:
    """Fetch a single booking by UID. Returns None if not found."""
    resp = requests.get(f"{_BASE}/bookings/{uid}", headers=_headers(), timeout=10)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json().get("data")


def list_event_types() -> list[dict]:
    """Return [{id, title, slug}] for all event types."""
    resp = requests.get(f"{_BASE}/event-types", headers=_headers(), timeout=10)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    if isinstance(data, dict):
        items = data.get("eventTypeGroups", [{}])[0].get("eventTypes", [])
    else:
        items = data if isinstance(data, list) else []
    return [{"id": et["id"], "title": et["title"], "slug": et.get("slug", "")} for et in items]


def create_booking(
    event_type_id: int,
    start_utc_iso: str,
    customer_name: str,
    customer_email: str,
    customer_phone: str,
    service_address: str,
    notes: str = "",
    timezone: str = "America/Chicago",
) -> dict:
    """Create a Cal.com booking. service_address becomes the 'Where:' line in the email."""
    body: dict = {
        "eventTypeId": event_type_id,
        "start": start_utc_iso,
        "attendee": {
            "name": customer_name,
            "email": customer_email,
            "timeZone": timezone,
            "phoneNumber": customer_phone,
        },
        "location": service_address,
    }
    if notes:
        body["bookingFieldsResponses"] = {"notes": notes}
    resp = requests.post(f"{_BASE}/bookings", headers=_headers(), json=body, timeout=15)
    resp.raise_for_status()
    return resp.json().get("data", {})


def reschedule_booking(uid: str, new_start: datetime, notify: bool = False) -> tuple[bool, str]:
    """
    Reschedule a Cal.com booking.
    notify=False suppresses customer email (default — for route optimization moves).
    Returns (success: bool, message: str).
    """
    body: dict = {
        "start":            new_start.isoformat(),
        "rescheduleReason": "Route optimization",
    }
    if not notify:
        body["sendEmailsTo"] = "none"   # suppress all notification emails

    resp = requests.post(
        f"{_BASE}/bookings/{uid}/reschedule",
        headers=_headers(),
        json=body,
        timeout=10,
    )
    if resp.status_code in (200, 201):
        return True, "OK"
    try:
        msg = resp.json().get("error", {}).get("message", resp.text)
    except Exception:
        msg = resp.text
    return False, msg
