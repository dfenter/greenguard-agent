"""
stripe_import.py — Set up Stripe billing from Google Calendar appointments.

Reads upcoming events from Google Calendar (primary), determines each
customer's service type, and creates Stripe subscriptions / draft invoices.

Billing starts 3 days after each customer's next upcoming appointment.
Customers already in Stripe are matched by email (no duplicate creation).
Customers already subscribed are skipped.

Usage:
    python3 stripe_import.py                  # dry run
    python3 stripe_import.py --execute        # live
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import stripe
from dotenv import load_dotenv
from googleapiclient.discovery import build

import sku_engine
import stripe_client

load_dotenv()
stripe.api_key = os.getenv("STRIPE_LIVE_KEY") or os.getenv("STRIPE_SECRET_KEY", "")

PRICES_FILE = Path(__file__).parent / "stripe_prices.json"
TZ          = timezone(timedelta(hours=-5))   # America/Chicago CDT
END_DATE    = datetime(2027, 3, 31, tzinfo=TZ)


# ── Google Calendar auth ──────────────────────────────────────────────────────

def _get_calendar_service():
    from gmail_client import authenticate
    _, creds = authenticate()
    return build("calendar", "v3", credentials=creds)


# ── Event field extraction ────────────────────────────────────────────────────

def _extract_email(desc: str) -> str | None:
    m = re.search(r"Email:\s*(\S+@\S+)", desc)
    return m.group(1).strip().lower() if m else None

def _extract_phone(desc: str) -> str | None:
    m = re.search(r"Phone(?:\s*Number)?:\s*([\d\s\-\(\)\.+]+)", desc)
    return m.group(1).strip() if m else None


# ── Fetch all upcoming calendar events ───────────────────────────────────────

def fetch_appointments(cal) -> list[dict]:
    """Return all future timed events from now → END_DATE, flattened."""
    now     = datetime.now(TZ)
    appts   = []
    page_token = None

    while True:
        kwargs = dict(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=END_DATE.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=500,
            fields="nextPageToken,items(id,summary,start,description)",
        )
        if page_token:
            kwargs["pageToken"] = page_token

        resp       = cal.events().list(**kwargs).execute()
        items      = resp.get("items", [])
        page_token = resp.get("nextPageToken")

        for ev in items:
            raw_start = ev["start"].get("dateTime")
            if not raw_start:
                continue   # skip all-day events
            dt      = datetime.fromisoformat(raw_start).astimezone(TZ)
            summary = ev.get("summary", "")
            desc    = ev.get("description", "") or ""

            # name = everything before the first ":"  (Acuity format: "Name: Service")
            parts        = summary.split(":", 1)
            name         = parts[0].strip()
            service_type = parts[1].strip() if len(parts) > 1 else summary.strip()

            email = _extract_email(desc)
            phone = _extract_phone(desc)

            if not email:
                continue   # can't match to a Stripe customer without email

            appts.append({
                "name":         name,
                "email":        email,
                "phone":        phone,
                "service_type": service_type,
                "dt":           dt,
            })

        if not page_token:
            break

    return appts


# ── Service type → SKU slug ───────────────────────────────────────────────────

_WORD_TO_N = {"one": 1, "two": 2, "three": 3, "four": 4, "six": 6, "ten": 10}
_N_TO_SLUG = {
    1: "tank-exchange-1", 2: "tank-exchange-2", 3: "tank-exchange-3",
    4: "tank-exchange-4", 6: "tank-exchange-6", 10: "tank-exchange-10",
}

def _type_to_slug(service_type: str) -> str | None:
    t = service_type.lower()

    if "mosqitter" in t and "installation" in t: return "mosqitter-installation"
    if "mosqitter" in t and "troubleshoot" in t: return "mosqitter-troubleshoot"
    if "mosqitter" in t and "rental" in t:       return "mosqitter-rental"
    if "mosqitter" in t:                         return "mosqitter-service"

    if "3x biogents" in t or "3 biogents" in t: return "biogents-co2-3"
    if "2x biogents" in t or "2 biogents" in t: return "biogents-co2-2"
    if "biogents" in t:                          return "biogents-co2-1"

    if "equipment" in t and "pickup" in t:       return "equipment-pickup"
    if "equipment delivery" in t:                return "tank-exchange-1"

    if "assessment" in t:                        return "property-assessment"
    if "refill check" in t:                      return "tank-refill-check"
    if "barrier" in t:                           return "barrier-treatment"
    if "tank rental" in t:                       return "tank-rental"

    first_word = t.split()[0] if t.split() else ""
    if first_word in _WORD_TO_N and ("tank" in t or "pound" in t):
        return _N_TO_SLUG.get(_WORD_TO_N[first_word], "tank-exchange-1")

    m = re.match(r"^(\d+)\s+", t)
    if m and ("tank" in t or "pound" in t):
        return _N_TO_SLUG.get(int(m.group(1)), "tank-exchange-1")

    if "tank" in t or "pound" in t:
        return "tank-exchange-1"

    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def run(dry_run: bool = True):
    mode = "DRY RUN" if dry_run else "LIVE"
    print(f"\n{'='*62}")
    print(f"  Stripe Import from Google Calendar  |  {mode}")
    print(f"{'='*62}\n")

    if not stripe.api_key:
        print("Error: STRIPE_LIVE_KEY not set in .env")
        sys.exit(1)

    if not PRICES_FILE.exists():
        print("Error: stripe_prices.json not found — run stripe_setup.py first")
        sys.exit(1)

    prices = json.loads(PRICES_FILE.read_text())

    print("  Connecting to Google Calendar …")
    cal   = _get_calendar_service()
    appts = fetch_appointments(cal)
    print(f"  {len(appts)} upcoming timed events fetched\n")

    # Group by email — take earliest appointment as primary, rest as history
    by_email: dict[str, list[dict]] = {}
    for a in sorted(appts, key=lambda x: x["dt"]):
        by_email.setdefault(a["email"], []).append(a)

    print(f"  {len(by_email)} unique customers\n")
    print(f"  {'─'*58}")

    created = skipped_existing = skipped_onetime = failed = unmapped = 0

    for email, customer_appts in sorted(by_email.items()):
        first        = customer_appts[0]
        name         = first["name"]
        phone        = first["phone"]
        service_type = first["service_type"]
        next_dt      = first["dt"]
        slug         = _type_to_slug(service_type)
        sku          = sku_engine.resolve(slug) if slug else None

        if not slug or not sku:
            print(f"  MAP?  {name:<28} {email}  '{service_type[:40]}'")
            unmapped += 1
            continue

        price_id     = prices.get(sku.code)
        billing_date = (next_dt + timedelta(days=3)).date()

        if sku.price_cents == 0:
            print(f"  FREE  {name:<28} {email}  ({sku.code})")
            skipped_onetime += 1
            continue

        if not price_id:
            print(f"  ERR   {name:<28} no Stripe price for {sku.code}")
            failed += 1
            continue

        # Find or create Stripe customer
        if not dry_run:
            customer_id = stripe_client.get_or_create_customer(name, email, phone or None)
        else:
            customer_id = stripe_client.find_customer(email) or "new"

        exists    = customer_id != "new"
        found_str = "existing" if exists else "new"

        if sku.billing_type == "recurring":
            if not dry_run and exists:
                existing_sub = stripe_client.get_active_subscription(customer_id)
                if existing_sub:
                    print(f"  SKIP  {name:<28} already subscribed ({existing_sub['status']})")
                    skipped_existing += 1
                    continue

            if not dry_run:
                stripe_client.create_subscription(customer_id, price_id, next_dt)

            marker = "  ~   " if dry_run else "  ✓   "
            print(f"{marker}{name:<28} {sku.code:<12} bills {billing_date}  ({found_str} | {len(customer_appts)} appts)")
            created += 1

        else:
            if not dry_run:
                stripe_client.create_draft_invoice(
                    customer_id, sku.price_cents, sku.code, sku.label, next_dt
                )
            marker = "  ~   " if dry_run else "  ✓   "
            print(f"{marker}{name:<28} {sku.code:<12} invoice {billing_date}")
            created += 1

    print(f"\n  {'─'*58}")
    verb = "Would set up" if dry_run else "Set up"
    print(f"  {verb:<18}: {created}")
    print(f"  {'Already subscribed':<18}: {skipped_existing}")
    print(f"  {'Free/zero-cost':<18}: {skipped_onetime}")
    print(f"  {'Unmapped service':<18}: {unmapped}")
    print(f"  {'Failed':<18}: {failed}")

    if unmapped:
        print(f"\n  Fix unmapped types above, then re-run.")
    if dry_run and created:
        print(f"\n  Run with --execute to set up {created} customer(s) in Stripe.")
    print(f"\n{'='*62}\n")


if __name__ == "__main__":
    execute = "--execute" in sys.argv
    run(dry_run=not execute)
