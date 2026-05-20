"""
stripe_import.py — Set up Stripe billing for existing Acuity customers.

Reads the Acuity schedule export CSV, determines each customer's primary
service type, and creates Stripe customers + subscriptions/queued invoices.

Billing starts 3 days after each customer's next upcoming appointment.
Customers already in Stripe are matched by email (existing cards auto-charge).

Usage:
    python3 stripe_import.py                          # dry run
    python3 stripe_import.py --execute                # live
    python3 stripe_import.py --csv path/to/file.csv  # custom CSV path
"""

import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import stripe
from dotenv import load_dotenv

import sku_engine
import stripe_client

load_dotenv()
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

DEFAULT_CSV = Path.home() / "Downloads" / "schedule2026-05-19.csv"
PRICES_FILE = Path(__file__).parent / "stripe_prices.json"
TZ          = timezone(timedelta(hours=-5))  # America/Chicago CDT


# ── Acuity type → Cal.com slug mapping ───────────────────────────────────────

_WORD_TO_N = {"one": 1, "two": 2, "three": 3, "four": 4, "six": 6, "ten": 10}
_N_TO_SLUG = {1: "tank-exchange-1", 2: "tank-exchange-2", 3: "tank-exchange-3",
              4: "tank-exchange-4", 6: "tank-exchange-6", 10: "tank-exchange-10"}


def _type_to_slug(appt_type: str) -> str | None:
    import re
    t = appt_type.lower()

    # Mosqitter
    if "mosqitter" in t and "installation" in t:  return "mosqitter-installation"
    if "mosqitter" in t and "troubleshoot" in t:  return "mosqitter-troubleshoot"
    if "mosqitter" in t and "rental" in t:         return "mosqitter-rental"
    if "mosqitter" in t:                           return "mosqitter-service"

    # Biogents trap rental (check count prefix first)
    if "3x biogents" in t:                         return "biogents-co2-3"
    if "2x biogents" in t:                         return "biogents-co2-2"
    if "biogents" in t:                            return "biogents-co2-1"

    # Equipment delivery only — standalone tank delivery, treat as single tank
    if "equipment delivery" in t:                  return "tank-exchange-1"

    # Free / check
    if "assessment" in t:                          return "property-assessment"
    if "refill check" in t:                        return "tank-refill-check"
    if "barrier" in t:                             return "barrier-treatment"

    # Tank exchanges — Acuity titles start with count word ("One", "Two", etc.)
    # or a digit ("10 Tank Exchange")
    first_word = t.split()[0] if t.split() else ""
    if first_word in _WORD_TO_N and ("tank" in t or "pound" in t):
        return _N_TO_SLUG.get(_WORD_TO_N[first_word], "tank-exchange-1")

    # Numeric prefix: "10 Tank Exchange", "6 CO2 Tank..."
    m = re.match(r"^(\d+)\s+", t)
    if m and ("tank" in t or "pound" in t):
        n = int(m.group(1))
        return _N_TO_SLUG.get(n, "tank-exchange-1")

    # Fallback for "20 Pound CO2 Tank Rental..." (single tank, no count word)
    if "tank" in t or "pound" in t:               return "tank-exchange-1"

    return None


# ── CSV parsing ───────────────────────────────────────────────────────────────

def _parse_dt(raw: str) -> datetime | None:
    for fmt in ("%B %d, %Y %I:%M %p", "%B %d, %Y %I:%M%p"):
        try:
            return datetime.strptime(raw.strip(), fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    return None


def load_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def future_appointments(rows: list[dict]) -> list[dict]:
    now = datetime.now(TZ)
    out = []
    for r in rows:
        dt = _parse_dt(r.get("Start Time", ""))
        if dt and dt > now:
            r["_dt"] = dt
            out.append(r)
    return out


def by_customer(rows: list[dict]) -> dict[str, list[dict]]:
    customers: dict[str, list[dict]] = {}
    for r in rows:
        email = r.get("Email", "").strip().lower()
        if email:
            customers.setdefault(email, []).append(r)
    # Sort each customer's appointments ascending
    for appts in customers.values():
        appts.sort(key=lambda x: x["_dt"])
    return customers


# ── Main ──────────────────────────────────────────────────────────────────────

def run(csv_path: Path, dry_run: bool = True):
    mode = "DRY RUN" if dry_run else "LIVE"
    print(f"\n{'='*62}")
    print(f"  Stripe Customer Import  |  {mode}")
    print(f"  Source: {csv_path.name}")
    print(f"{'='*62}\n")

    if not stripe.api_key:
        print("Error: STRIPE_SECRET_KEY not set in .env")
        sys.exit(1)

    if not PRICES_FILE.exists():
        print("Error: stripe_prices.json not found — run stripe_setup.py first")
        sys.exit(1)

    prices = json.loads(PRICES_FILE.read_text())

    rows    = load_csv(csv_path)
    future  = future_appointments(rows)
    customers = by_customer(future)

    print(f"  {len(rows)} total rows → {len(future)} future appointments → {len(customers)} unique customers\n")
    print(f"  {'─'*58}")

    created = skipped_existing = skipped_onetime = failed = unmapped = 0

    for email, appts in sorted(customers.items()):
        first    = appts[0]
        name     = f"{first.get('First Name','').strip()} {first.get('Last Name','').strip()}".strip()
        phone    = first.get("Phone", "").strip()
        appt_type = first.get("Type", "").strip()
        next_dt  = first["_dt"]
        slug     = _type_to_slug(appt_type)
        sku      = sku_engine.resolve(slug) if slug else None

        if not slug or not sku:
            print(f"  MAP?  {name:<28} {email}  unmapped: '{appt_type[:45]}'")
            unmapped += 1
            continue

        price_id = prices.get(sku.code)
        if not price_id and sku.price_cents > 0:
            print(f"  ERR   {name:<28} no Stripe price for {sku.code}")
            failed += 1
            continue

        billing_date = (next_dt + timedelta(days=3)).date()

        if sku.price_cents == 0:
            print(f"  FREE  {name:<28} {email}  ({sku.code})")
            skipped_onetime += 1
            continue

        # Find or create Stripe customer
        if not dry_run:
            customer_id = stripe_client.get_or_create_customer(name, email, phone or None)
        else:
            customer_id = stripe_client.find_customer(email) or "new"

        exists = customer_id != "new"

        if sku.billing_type == "recurring":
            # Check for existing subscription
            if not dry_run and exists:
                existing_sub = stripe_client.get_active_subscription(customer_id)
                if existing_sub:
                    print(f"  SKIP  {name:<28} already subscribed ({existing_sub['status']})")
                    skipped_existing += 1
                    continue

            if not dry_run:
                stripe_client.create_subscription(customer_id, price_id, next_dt)

            marker    = "  ~   " if dry_run else "  ✓   "
            found_str = "existing" if exists else "new"
            count_str = f"{len(appts)} appts"
            print(f"{marker}{name:<28} {sku.code:<12} bills {billing_date}  ({found_str} | {count_str})")
            created += 1

        else:
            # One-time — create draft invoice
            if not dry_run:
                stripe_client.create_draft_invoice(
                    customer_id, price_id, sku.code, sku.label, next_dt
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
        print(f"\n  Unmapped types need manual review.")
    if dry_run and created:
        print(f"\n  Run with --execute to set up {created} customer(s) in Stripe.")
    print(f"\n{'='*62}\n")


if __name__ == "__main__":
    execute  = "--execute" in sys.argv
    dry_run  = not execute

    csv_path = DEFAULT_CSV
    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--csv" and i + 1 < len(sys.argv) - 1:
            csv_path = Path(sys.argv[i + 2])

    if not csv_path.exists():
        print(f"CSV not found: {csv_path}")
        sys.exit(1)

    run(csv_path, dry_run)
