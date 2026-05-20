"""
One-time script to create GreenGuard USA event types in Cal.com.
Run once during migration: python event_type_setup.py

Creates all service types from the unified SKU system with correct durations.
Safe to re-run — skips types that already exist by slug.
"""

import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY  = os.getenv("CALCOM_API_KEY", "")
BASE     = "https://api.cal.com/v2"
VERSION  = "2024-08-13"

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "cal-api-version": VERSION,
    "Content-Type": "application/json",
}

# SKU catalog → Cal.com event types
# (title, slug, duration_minutes, description, price_cents)
EVENT_TYPES = [
    # Tank-only exchanges — $39.99 delivery + $49.99/tank
    ("CO2 Tank Exchange — 1 Tank",   "tank-exchange-1",  30,
     "Exchange of 1 × 20 lb CO₂ tank. Includes $39.99 delivery + $49.99/tank.", 8998),
    ("CO2 Tank Exchange — 2 Tanks",  "tank-exchange-2",  45,
     "Exchange of 2 × 20 lb CO₂ tanks. Includes $39.99 delivery + $49.99/tank.", 13997),
    ("CO2 Tank Exchange — 3 Tanks",  "tank-exchange-3",  60,
     "Exchange of 3 × 20 lb CO₂ tanks. Includes $39.99 delivery + $49.99/tank.", 18996),
    ("CO2 Tank Exchange — 4 Tanks",  "tank-exchange-4",  60,
     "Exchange of 4 × 20 lb CO₂ tanks. Includes $39.99 delivery + $49.99/tank.", 23995),
    ("CO2 Tank Exchange — 10 Tanks", "tank-exchange-10", 90,
     "Bulk exchange of 10 × 20 lb CO₂ tanks. Includes $39.99 delivery + $49.99/tank.", 53989),

    # Biogents CO₂ rental service
    ("Biogents CO₂ Service — 1 Trap",  "biogents-co2-1",  30,
     "Monthly service for 1 Biogents Mosquitaire CO₂ trap.", 15999),
    ("Biogents CO₂ Service — 2 Traps", "biogents-co2-2",  45,
     "Monthly service for 2 Biogents Mosquitaire CO₂ traps.", 26699),
    ("Biogents CO₂ Service — 3 Traps", "biogents-co2-3",  60,
     "Monthly service for 3 Biogents Mosquitaire CO₂ traps.", 39999),

    # Mosqitter Grand
    ("Mosqitter Grand — Monthly Rental",  "mosqitter-rental",       45,
     "Monthly Mosqitter Grand CO₂ trap rental and service.", 29999),
    ("Mosqitter Grand — Monthly Service", "mosqitter-service",      45,
     "Monthly service visit for owned Mosqitter Grand system.", 12999),
    ("Mosqitter Grand — Installation",    "mosqitter-installation", 90,
     "Full installation and initial setup of Mosqitter Grand system.", 19999),
    ("Mosqitter Grand — Troubleshooting", "mosqitter-troubleshoot", 60,
     "Diagnostic troubleshooting visit for Mosqitter Grand system.", 7999),

    # Free / assessment
    ("Free Property Assessment", "property-assessment", 30,
     "Free on-site mosquito assessment. No charge.", 0),
    ("Tank Refill Check",        "tank-refill-check",   20,
     "Quick check and refill verification at your property.", 0),

    # Barrier treatment
    ("GreenGuard Barrier Treatment", "barrier-treatment", 30,
     "Barrier spray treatment for mosquito control around your property.", 4999),

    # Equipment pickup
    ("Equipment Pickup", "equipment-pickup", 30,
     "Scheduled pickup of GreenGuard equipment from your property.", 0),

    # Tank rental
    ("CO₂ Tank Monthly Rental", "tank-rental", 30,
     "Monthly CO₂ tank rental service. Tank delivered and swapped each month.", 12499),
]


def get_existing_slugs() -> set[str]:
    resp = requests.get(f"{BASE}/event-types", headers=HEADERS, timeout=10)
    if not resp.ok:
        return set()
    items = resp.json().get("data", [])
    if isinstance(items, dict):
        items = items.get("eventTypeGroups", [{}])[0].get("eventTypes", [])
    return {et.get("slug", "") for et in items}


def create_event_type(title, slug, duration, description, price_cents) -> bool:
    body = {
        "title":       title,
        "slug":        slug,
        "lengthInMinutes": duration,
        "description": description,
    }
    if price_cents > 0:
        body["price"] = price_cents

    resp = requests.post(f"{BASE}/event-types", headers=HEADERS, json=body, timeout=10)
    return resp.status_code in (200, 201)


def main():
    if not API_KEY:
        print("Error: CALCOM_API_KEY not set in .env")
        sys.exit(1)

    print("Fetching existing event types …")
    existing = get_existing_slugs()
    print(f"Found {len(existing)} existing types.\n")

    created = skipped = failed = 0
    for title, slug, duration, description, price in EVENT_TYPES:
        if slug in existing:
            print(f"  SKIP  {title}")
            skipped += 1
            continue
        ok = create_event_type(title, slug, duration, description, price)
        if ok:
            print(f"  ✓     {title}  ({duration} min)")
            created += 1
        else:
            print(f"  ✗     {title}  — failed")
            failed += 1

    print(f"\nDone: {created} created, {skipped} skipped, {failed} failed.")


if __name__ == "__main__":
    main()
