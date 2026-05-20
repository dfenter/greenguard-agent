"""
GreenGuard USA — Appointment Type Converter

Converts an existing booking to a different service type.
Example: Free Property Assessment → Biogents CO₂ Service — 1 Trap

What it does:
  - Finds the customer's existing draft invoice or trialing subscription
  - Preserves the original appointment date and 3-day billing window
  - Voids/cancels the original billing record
  - Creates the correct new invoice or subscription

Usage:
    python3 admin_convert.py
    python3 admin_convert.py --email customer@example.com
"""

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

PRICES_FILE = Path(__file__).parent / "stripe_prices.json"

# Only show bookable service types (not add-ons)
_SERVICES = sku_engine.all_skus()


def _load_prices() -> dict[str, str]:
    if not PRICES_FILE.exists():
        print("Error: stripe_prices.json not found — run stripe_setup.py first")
        sys.exit(1)
    return json.loads(PRICES_FILE.read_text())


def _get_draft_invoice(customer_id: str) -> dict | None:
    """Return most recent draft invoice with appointment metadata."""
    for inv in stripe.Invoice.list(customer=customer_id, status="draft", limit=5).data:
        meta = {k: v for k, v in inv.metadata.items()} if inv.metadata else {}
        if "appointment_date" in meta or "billing_date" in meta:
            return {
                "id":               inv.id,
                "appointment_date": meta.get("appointment_date", ""),
                "billing_date":     meta.get("billing_date", ""),
                "sku":              meta.get("sku", ""),
                "amount":           inv.amount_due,
            }
    return None


def _get_trialing_subscription(customer_id: str) -> dict | None:
    """Return active trialing subscription."""
    subs = stripe.Subscription.list(customer=customer_id, status="trialing", limit=1)
    if subs.data:
        s = subs.data[0]
        meta = {k: v for k, v in s.metadata.items()} if s.metadata else {}
        trial_end = datetime.fromtimestamp(s.trial_end, tz=timezone.utc) if s.trial_end else None
        return {
            "id":               s.id,
            "price_id":         s["items"].data[0].price.id,
            "appointment_date": meta.get("appointment_date", ""),
            "trial_end":        trial_end,
        }
    return None


def _parse_appointment_dt(date_str: str) -> datetime:
    """Parse appointment_date string to UTC datetime."""
    try:
        return datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _select_new_service() -> sku_engine.SKU:
    """Let admin pick a new service type."""
    print("\n  ── New service type ─────────────────────────────────")
    for i, sku in enumerate(_SERVICES, 1):
        billing = "monthly" if sku.billing_type == "recurring" else "one-time"
        price   = f"${sku.price_cents/100:.2f}" if sku.price_cents else "free"
        print(f"  {i:>2}.  {sku.label:<44} {price} ({billing})")
    print()

    while True:
        raw = input("  Select new service type #: ").strip()
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(_SERVICES):
                return _SERVICES[idx]
        except ValueError:
            pass
        print("  Invalid selection.")


def main():
    print(f"\n{'='*58}")
    print(f"  GreenGuard USA — Appointment Type Converter")
    print(f"{'='*58}\n")

    # Get customer
    email = ""
    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--email" and i + 1 < len(sys.argv) - 1:
            email = sys.argv[i + 2]
    if not email:
        email = input("  Customer email: ").strip().lower()
    if not email:
        sys.exit(1)

    customer_id = stripe_client.find_customer(email)
    if not customer_id:
        print(f"  No Stripe customer found for {email}")
        sys.exit(1)

    cust = stripe.Customer.retrieve(customer_id)
    print(f"  Customer: {cust.name} ({email})\n")

    # Find existing billing record
    draft_inv = _get_draft_invoice(customer_id)
    trialing_sub = _get_trialing_subscription(customer_id)

    if not draft_inv and not trialing_sub:
        print("  No pending invoice or trialing subscription found.")
        print("  This customer may have a $0 appointment (assessment/check) — no billing was created.")
        use_today = input("  Use today as the appointment date? (y/n) [y]: ").strip().lower()
        appointment_dt = datetime.now(timezone.utc) if use_today != "n" else None
        if not appointment_dt:
            sys.exit(0)
        existing_type = "none"
    elif draft_inv:
        print(f"  Found draft invoice: {draft_inv['id']}")
        print(f"    SKU:              {draft_inv['sku']}")
        print(f"    Appointment date: {draft_inv['appointment_date']}")
        print(f"    Billing date:     {draft_inv['billing_date']}")
        print(f"    Amount:           ${draft_inv['amount']/100:.2f}")
        appointment_dt = _parse_appointment_dt(draft_inv["appointment_date"])
        existing_type = "draft"
    else:
        trial_end_str = trialing_sub["trial_end"].strftime("%Y-%m-%d") if trialing_sub["trial_end"] else "unknown"
        print(f"  Found trialing subscription: {trialing_sub['id']}")
        print(f"    Appointment date: {trialing_sub['appointment_date']}")
        print(f"    First charge:     {trial_end_str}")
        appointment_dt = _parse_appointment_dt(trialing_sub["appointment_date"])
        existing_type = "subscription"

    billing_date = (appointment_dt + timedelta(days=3)).date().isoformat()
    print(f"\n  Appointment date will be preserved: {appointment_dt.date()}")
    print(f"  New billing date:                   {billing_date}\n")

    # Select new service
    new_sku = _select_new_service()
    prices  = _load_prices()
    price_id = prices.get(new_sku.code)
    if not price_id and new_sku.price_cents > 0:
        print(f"  Error: no Stripe price ID for {new_sku.code}")
        sys.exit(1)

    billing = "monthly" if new_sku.billing_type == "recurring" else "one-time"
    price_str = f"${new_sku.price_cents/100:.2f}" if new_sku.price_cents else "free"
    print(f"\n  Converting to: {new_sku.label}")
    print(f"  Price:         {price_str} ({billing})")
    print(f"  Bills on:      {billing_date}\n")

    confirm = input("  Apply conversion? (y/n): ").strip().lower()
    if confirm != "y":
        print("  Cancelled.")
        return

    # Void / cancel existing billing
    if existing_type == "draft":
        stripe.Invoice.void_invoice(draft_inv["id"])
        print(f"  Voided draft invoice {draft_inv['id']}")
    elif existing_type == "subscription":
        stripe.Subscription.cancel(trialing_sub["id"])
        print(f"  Cancelled subscription {trialing_sub['id']}")

    # Create new billing record
    if new_sku.price_cents == 0:
        print(f"  New service is free — no billing created.")

    elif new_sku.billing_type == "recurring":
        sub_id = stripe_client.create_subscription(customer_id, price_id, appointment_dt)
        print(f"  Created subscription {sub_id} — first charge {billing_date}")

    else:
        inv_id = stripe_client.create_draft_invoice(
            customer_id, price_id, new_sku.code, new_sku.label, appointment_dt,
        )
        print(f"  Created draft invoice {inv_id} — sends {billing_date}")

    print(f"\n  Conversion complete.")
    print(f"  Stripe customer: https://dashboard.stripe.com/customers/{customer_id}\n")


if __name__ == "__main__":
    main()
