"""
Finds available 2-hour service windows in Google Calendar
for the next DAYS_AHEAD business days (Mon–Sat, 7am–6pm).
Also provides address extraction and driving distance calculation for daily routes.
"""

import re
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import googlemaps
from googleapiclient.discovery import build

BUSINESS_START = time(7, 0)
BUSINESS_END = time(18, 0)
SLOT_HOURS = 2
DAYS_AHEAD = 14
MAX_SLOTS = 4


def get_calendar_service(credentials):
    return build("calendar", "v3", credentials=credentials)


def _fmt_time(dt: datetime) -> str:
    h = dt.hour % 12 or 12
    suffix = "am" if dt.hour < 12 else "pm"
    return f"{h}{suffix}"


def get_available_slots(calendar_service, timezone: str = "America/New_York") -> list[str]:
    """Return up to MAX_SLOTS human-readable 2-hour windows that are free on the calendar."""
    tz = ZoneInfo(timezone)
    now = datetime.now(tz)
    end_window = now + timedelta(days=DAYS_AHEAD)

    # Fetch all events in the window
    result = (
        calendar_service.events()
        .list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=end_window.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )

    # Build sorted list of busy intervals (skip all-day events)
    busy: list[tuple[datetime, datetime]] = []
    for event in result.get("items", []):
        raw_start = event["start"].get("dateTime")
        raw_end = event["end"].get("dateTime")
        if raw_start and raw_end:
            busy.append((
                datetime.fromisoformat(raw_start).astimezone(tz),
                datetime.fromisoformat(raw_end).astimezone(tz),
            ))
    busy.sort(key=lambda x: x[0])

    slots: list[str] = []

    # Walk day by day, looking for free 2-hour windows
    day = now.replace(hour=BUSINESS_START.hour, minute=0, second=0, microsecond=0)
    if day <= now:
        day += timedelta(days=1)

    while day < end_window and len(slots) < MAX_SLOTS:
        # Skip Sundays (weekday == 6)
        if day.weekday() == 6:
            day += timedelta(days=1)
            continue

        cursor = day.replace(hour=BUSINESS_START.hour, minute=0, second=0, microsecond=0)

        while cursor.hour + SLOT_HOURS <= BUSINESS_END.hour and len(slots) < MAX_SLOTS:
            slot_end = cursor.replace(hour=cursor.hour + SLOT_HOURS)

            conflict = any(cursor < b_end and slot_end > b_start for b_start, b_end in busy)
            if conflict:
                # Jump past the conflicting event
                overlapping = [b_end for b_start, b_end in busy if cursor < b_end and slot_end > b_start]
                if overlapping:
                    latest = max(overlapping)
                    cursor = latest.replace(minute=0, second=0, microsecond=0)
                    if latest.minute:
                        cursor += timedelta(hours=1)
                    continue
            else:
                date_str = f"{cursor.strftime('%A, %b')} {cursor.day}"
                slot_str = f"{date_str} at {_fmt_time(cursor)}–{_fmt_time(slot_end)}"
                slots.append(slot_str)
                cursor = slot_end  # advance past this slot

        day += timedelta(days=1)

    return slots


def extract_address(event: dict) -> str | None:
    """
    Extract the best street address from a calendar event.

    Priority:
    1. Acuity 'Address' section in description (customer-entered service address)
    2. Top-level 'location' field (mirrors Acuity's Location section)

    Returns None if only a city/state is found (no street number).
    """
    description = event.get("description", "") or ""

    # Try the explicit 'Address' section Acuity puts in the description
    addr_match = re.search(
        r"Address\n={4,}\nPlease enter the address for the service to be performed::\s*(.+?)(?:\n\n|\Z)",
        description,
        re.DOTALL,
    )
    if addr_match:
        addr = addr_match.group(1).strip()
        if addr and re.search(r"\d", addr):
            return addr

    # Fall back to the top-level location field (same as Acuity's Location section)
    location = (event.get("location") or "").strip()
    if location and re.search(r"\d", location):
        return location

    return None


def get_route_distances(
    calendar_service,
    maps_api_key: str,
    origin: str,
    date: datetime | None = None,
    timezone: str = "America/Chicago",
) -> list[dict]:
    """
    Return today's appointments in chronological order with driving distance
    from the previous stop (starting from origin/depot).

    Each entry: {summary, start, address, distance_miles, duration_minutes}
    distance_miles/duration_minutes are None when address is unavailable.
    """
    tz = ZoneInfo(timezone)
    if date is None:
        date = datetime.now(tz)

    day_start = date.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = date.replace(hour=23, minute=59, second=59, microsecond=0)

    result = (
        calendar_service.events()
        .list(
            calendarId="primary",
            timeMin=day_start.isoformat(),
            timeMax=day_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )

    appointments = [
        {
            "summary": ev.get("summary", ""),
            "start": datetime.fromisoformat(ev["start"]["dateTime"]).astimezone(tz),
            "address": extract_address(ev),
        }
        for ev in result.get("items", [])
        if ev["start"].get("dateTime")  # skip all-day events
    ]

    if not appointments:
        return []

    # Build one batched matrix for all stops (including depot) rather than one call per stop
    gmaps = googlemaps.Client(key=maps_api_key)
    known = [appt for appt in appointments if appt["address"]]
    all_locs = [origin] + [a["address"] for a in known]

    n = len(all_locs)
    dist: list[list[int]] = [[0] * n for _ in range(n)]
    mins: list[list[int]] = [[0] * n for _ in range(n)]
    BATCH = 10
    for i0 in range(0, n, BATCH):
        for j0 in range(0, n, BATCH):
            result = gmaps.distance_matrix(
                origins=all_locs[i0:i0 + BATCH],
                destinations=all_locs[j0:j0 + BATCH],
                mode="driving",
            )
            for ri, row in enumerate(result["rows"]):
                for rj, el in enumerate(row["elements"]):
                    if el["status"] == "OK":
                        dist[i0 + ri][j0 + rj] = el["distance"]["value"]
                        mins[i0 + ri][j0 + rj] = el["duration"]["value"]

    addr_to_idx = {a["address"]: i + 1 for i, a in enumerate(known)}
    route = []
    prev_idx = 0

    for appt in appointments:
        if not appt["address"]:
            route.append({**appt, "distance_miles": None, "duration_minutes": None})
            continue
        cur_idx = addr_to_idx[appt["address"]]
        distance_miles   = round(dist[prev_idx][cur_idx] / 1609.34, 1)
        duration_minutes = round(mins[prev_idx][cur_idx] / 60)
        route.append({**appt, "distance_miles": distance_miles, "duration_minutes": duration_minutes})
        prev_idx = cur_idx

    return route
