"""
calcom_import.py — Import Acuity appointments from Google Calendar into Cal.com

Reads Google Calendar events synced from Acuity, maps service names to Cal.com
event type slugs, and creates matching Cal.com bookings.

Usage:
    python calcom_import.py              # dry run — shows what would be created
    python calcom_import.py --execute   # live — actually creates bookings

Optional date override (defaults: +7 days today → 2027-03-31):
    python calcom_import.py --execute --start 2026-06-01 --end 2026-12-31

Duplicate prevention: successfully imported Acuity IDs are logged in
.calcom_imported.json and skipped on subsequent runs.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY  = os.getenv("CALCOM_API_KEY", "")
BASE     = "https://api.cal.com/v2"
VERSION  = "2024-08-13"
TZ_NAME  = os.getenv("CALENDAR_TIMEZONE", "America/Chicago")
TZ       = ZoneInfo(TZ_NAME)

IMPORT_LOG = Path(__file__).parent / ".calcom_imported.json"


def _headers() -> dict:
    if not API_KEY:
        raise RuntimeError("CALCOM_API_KEY not set in .env")
    return {
        "Authorization": f"Bearer {API_KEY}",
        "cal-api-version": VERSION,
        "Content-Type": "application/json",
    }


# ── Duplicate prevention ──────────────────────────────────────────────────────

def _load_imported() -> set[str]:
    if IMPORT_LOG.exists():
        try:
            return set(json.loads(IMPORT_LOG.read_text()))
        except Exception:
            return set()
    return set()


def _save_imported(ids: set[str]):
    IMPORT_LOG.write_text(json.dumps(sorted(ids), indent=2))


# ── Cal.com event types ───────────────────────────────────────────────────────

def get_event_type_map() -> dict[str, int]:
    """Return {slug: event_type_id} from the Cal.com account."""
    resp = requests.get(f"{BASE}/event-types", headers=_headers(), timeout=10)
    if not resp.ok:
        print(f"  Warning: could not fetch event types ({resp.status_code}) — slugs will be flagged unmapped.")
        return {}
    data = resp.json().get("data", {})
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        groups = data.get("eventTypeGroups", [])
        items = []
        for g in groups:
            items.extend(g.get("eventTypes", []))
    else:
        return {}
    return {et["slug"]: et["id"] for et in items if "slug" in et and "id" in et}


# ── Keyword → Cal.com slug ────────────────────────────────────────────────────
# Rules checked top-to-bottom; first match wins. Most specific first.

_SLUG_RULES: list[tuple[list[str], str]] = [
    # Mosqitter
    (["mosqitter", "install"],      "mosqitter-installation"),
    (["mosqitter", "troubleshoot"], "mosqitter-troubleshoot"),
    (["mosqitter", "rental"],       "mosqitter-rental"),
    (["mosqitter", "rent"],         "mosqitter-rental"),
    (["mosqitter"],                 "mosqitter-service"),
    # Biogents (check quantity digit)
    (["biogents", "3"],  "biogents-co2-3"),
    (["biogents", "2"],  "biogents-co2-2"),
    (["biogents"],       "biogents-co2-1"),
    # Refill check before generic tank rules
    (["refill"],         "tank-refill-check"),
    # Tank exchange (use "exchange" not "tank" to avoid matching refill)
    (["exchange", "4"],  "tank-exchange-4"),
    (["exchange", "3"],  "tank-exchange-3"),
    (["exchange", "2"],  "tank-exchange-2"),
    (["exchange"],       "tank-exchange-1"),
    # Other services
    (["assessment"],     "property-assessment"),
    (["barrier"],        "barrier-treatment"),
]


def _summary_to_slug(summary: str) -> str | None:
    s = summary.lower()
    for keywords, slug in _SLUG_RULES:
        if all(kw in s for kw in keywords):
            return slug
    return None


# ── Field extraction ──────────────────────────────────────────────────────────

def _extract_acuity_id(desc: str) -> str | None:
    m = re.search(r"AcuityID=(\d+)", desc)
    return m.group(1) if m else None


def _extract_email(desc: str) -> str | None:
    m = re.search(r"Email:\s*(\S+@\S+)", desc)
    return m.group(1).strip() if m else None


def _extract_phone(desc: str) -> str | None:
    m = re.search(r"Phone(?:\s*Number)?:\s*([\d\s\-\(\)\+\.]+)", desc)
    if m:
        digits = re.sub(r"[^\d\+]", "", m.group(1).strip())
        if len(digits) >= 10:
            return digits
    return None


def _extract_name(summary: str) -> str:
    """Customer name is the part before ':' in the summary, or the full summary."""
    return summary.split(":")[0].strip() or summary.strip()


# ── Booking creation ──────────────────────────────────────────────────────────

def create_booking(
    event_type_id: int,
    start: datetime,
    name: str,
    email: str,
    phone: str | None,
    dry_run: bool,
) -> tuple[bool, str]:
    body: dict = {
        "eventTypeId": event_type_id,
        "start": start.isoformat(),
        "attendee": {
            "name":     name,
            "email":    email,
            "timeZone": TZ_NAME,
        },
    }
    if phone:
        body["attendee"]["phoneNumber"] = phone

    if dry_run:
        return True, "dry-run"

    resp = requests.post(f"{BASE}/bookings", headers=_headers(), json=body, timeout=10)
    if resp.status_code in (200, 201):
        uid = resp.json().get("data", {}).get("uid", "?")
        return True, uid
    try:
        msg = resp.json().get("error", {}).get("message", resp.text[:140])
    except Exception:
        msg = resp.text[:140]
    return False, msg


# ── Main ──────────────────────────────────────────────────────────────────────

def run(start: datetime, end: datetime, dry_run: bool = True):
    mode = "DRY RUN — no changes will be made" if dry_run else "LIVE — bookings will be created"
    print(f"\n{'='*62}")
    print(f"  Cal.com Import  |  {mode}")
    print(f"  Range: {start.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}")
    print(f"{'='*62}\n")

    print("  Fetching Cal.com event types …")
    slug_to_id = get_event_type_map()
    if slug_to_id:
        print(f"  {len(slug_to_id)} event type(s) found.\n")
    else:
        print("  No event types found. Run event_type_setup.py first.\n")

    imported = _load_imported()
    print(f"  Already imported: {len(imported)} Acuity ID(s)\n")

    # Google Calendar fetch
    from gmail_client import authenticate
    from calendar_client import get_calendar_service

    _, creds = authenticate()
    cal = get_calendar_service(creds)

    result = (
        cal.events()
        .list(
            calendarId="primary",
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            fields="items(summary,start,end,description)",
        )
        .execute()
    )

    all_events = result.get("items", [])
    acuity_events = [
        (aid, ev)
        for ev in all_events
        if (aid := _extract_acuity_id(ev.get("description", "") or ""))
    ]

    print(f"  {len(all_events)} total calendar events, {len(acuity_events)} from Acuity.\n")
    print(f"  {'─'*58}")

    created = skipped_dup = failed = unmapped = 0
    new_imported: set[str] = set()

    for acuity_id, ev in acuity_events:
        summary   = ev.get("summary", "")
        raw_start = ev.get("start", {}).get("dateTime")
        if not raw_start:
            continue

        dt_start = datetime.fromisoformat(raw_start).astimezone(TZ)
        ts_str   = dt_start.strftime("%Y-%m-%d %H:%M")
        desc     = ev.get("description", "") or ""
        name     = _extract_name(summary)
        email    = _extract_email(desc)
        phone    = _extract_phone(desc)
        slug     = _summary_to_slug(summary)
        et_id    = slug_to_id.get(slug) if slug else None

        if acuity_id in imported:
            print(f"  SKIP  {name:<26} {ts_str}  (already imported)")
            skipped_dup += 1
            continue

        if not email:
            print(f"  WARN  {name:<26} {ts_str}  no email — skipped")
            failed += 1
            continue

        if not et_id:
            slug_str = slug or "no rule matched"
            print(f"  MAP?  {name:<26} {ts_str}  unmapped: '{summary}' → {slug_str}")
            unmapped += 1
            continue

        ok, msg = create_booking(et_id, dt_start, name, email, phone, dry_run)

        if ok:
            marker = "  ~   " if dry_run else "  ✓   "
            print(f"{marker}{name:<26} {ts_str}  {slug}")
            new_imported.add(acuity_id)
            created += 1
        else:
            print(f"  ✗    {name:<26} {ts_str}  {msg}")
            failed += 1

    if not dry_run and new_imported:
        _save_imported(imported | new_imported)

    print(f"  {'─'*58}")
    print(f"\n  Results:")
    verb = "Would create" if dry_run else "Created"
    print(f"    {verb:<20}: {created}")
    print(f"    {'Already imported':<20}: {skipped_dup}")
    print(f"    {'Unmapped service':<20}: {unmapped}")
    print(f"    {'Failed / no email':<20}: {failed}")

    if unmapped:
        print(f"\n  To fix unmapped services, add keyword rules to _SLUG_RULES in calcom_import.py.")
    if dry_run and created > 0:
        print(f"\n  Run with --execute to create {created} booking(s) in Cal.com.")
    print(f"\n{'='*62}\n")


if __name__ == "__main__":
    execute = "--execute" in sys.argv
    dry_run = not execute

    today  = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    start  = today + timedelta(days=7)
    end    = datetime(2027, 3, 31, 23, 59, tzinfo=TZ)

    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--start" and i + 1 < len(args):
            start = datetime.fromisoformat(args[i + 1]).replace(tzinfo=TZ)
        if arg == "--end" and i + 1 < len(args):
            end = datetime.fromisoformat(args[i + 1]).replace(hour=23, minute=59, tzinfo=TZ)

    run(start, end, dry_run)
