"""
GreenGuard USA — Admin Invoice Tool

Add equipment and add-on line items to a customer's open draft invoice,
or create a standalone invoice for any customer.

Usage:
    python3 admin_invoice.py
    python3 admin_invoice.py --email customer@example.com
    python3 admin_invoice.py --send   # finalize and send immediately (skip draft)
"""

import sys
import os
from datetime import datetime, timezone

import stripe
from dotenv import load_dotenv

import sku_engine
import stripe_client

load_dotenv()
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

# All billable items admin can add (add-ons + main SKUs that make sense standalone)
_ALL_ITEMS: list[sku_engine.SKU] = (
    sku_engine.all_addons()
    + [s for s in sku_engine.all_skus() if s.price_cents > 0]
)


def _load_prices() -> dict[str, str]:
    from pathlib import Path
    f = Path(__file__).parent / "stripe_prices.json"
    if not f.exists():
        print("Error: stripe_prices.json not found — run stripe_setup.py first")
        sys.exit(1)
    import json
    return json.loads(f.read_text())


def _find_open_draft(customer_id: str) -> dict | None:
    """Return the most recent draft invoice for this customer, if any."""
    drafts = stripe.Invoice.list(customer=customer_id, status="draft", limit=5)
    if drafts.data:
        inv = drafts.data[0]
        billing_date = (inv.metadata or {}).get("billing_date", "scheduled")
        return {"id": inv.id, "billing_date": billing_date,
                "amount": inv.amount_due, "lines": inv.lines.data}
    return None


def _print_invoice(inv: dict):
    print(f"\n  Draft invoice: {inv['id']}")
    print(f"  Bills on:     {inv['billing_date']}")
    if inv["lines"]:
        print(f"  Current items:")
        for line in inv["lines"]:
            desc = line.get("description", "")
            amt  = line.get("amount", 0)
            print(f"    • {desc}  ${amt/100:.2f}")
    else:
        print("  No items yet.")
    print()


def _select_items() -> list[tuple[sku_engine.SKU, int]]:
    """Interactive item selection. Returns list of (sku, quantity)."""
    print("\n  ── Available items ──────────────────────────────────")
    for i, sku in enumerate(_ALL_ITEMS, 1):
        print(f"  {i:>2}.  {sku.label:<44} ${sku.price_cents/100:.2f}")
    print("   0.  Done — no more items")
    print()

    selected = []
    while True:
        raw = input("  Add item # (or 0 to finish): ").strip()
        if raw == "0" or raw == "":
            break
        try:
            idx = int(raw) - 1
            if idx < 0 or idx >= len(_ALL_ITEMS):
                print("  Invalid number.")
                continue
            sku = _ALL_ITEMS[idx]

            qty_raw = input(f"  Quantity for '{sku.label}' [1]: ").strip()
            qty = int(qty_raw) if qty_raw else 1
            if qty < 1:
                continue

            selected.append((sku, qty))
            print(f"  ✓ Added: {qty} × {sku.label}  (${sku.price_cents * qty / 100:.2f})\n")
        except (ValueError, IndexError):
            print("  Invalid input.")

    return selected


def main():
    send_now = "--send" in sys.argv

    print(f"\n{'='*58}")
    print(f"  GreenGuard USA — Admin Invoice Tool")
    print(f"{'='*58}\n")

    # Get customer email
    email = ""
    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--email" and i + 1 < len(sys.argv) - 1:
            email = sys.argv[i + 2]
    if not email:
        email = input("  Customer email: ").strip().lower()
    if not email:
        print("  No email provided.")
        sys.exit(1)

    # Find or create customer
    customer_id = stripe_client.find_customer(email)
    if not customer_id:
        print(f"  No Stripe customer found for {email}")
        name = input("  Customer name (to create): ").strip()
        if not name:
            sys.exit(1)
        customer_id = stripe_client.create_customer(name, email)
        print(f"  Created customer: {customer_id}")
    else:
        cust = stripe.Customer.retrieve(customer_id)
        print(f"  Customer: {cust.name} ({email})  [{customer_id}]")

    # Check for open draft invoice
    draft = _find_open_draft(customer_id)
    if draft:
        print(f"\n  Found open draft invoice:")
        _print_invoice(draft)
        use_draft = input("  Add to this invoice? (y/n) [y]: ").strip().lower()
        invoice_id = draft["id"] if use_draft != "n" else None
    else:
        print(f"\n  No open draft invoice — will create a new one.")
        invoice_id = None

    # Select items
    selected = _select_items()
    if not selected:
        print("  No items selected — nothing to do.")
        return

    prices = _load_prices()

    # Total preview
    total = sum(s.price_cents * q for s, q in selected)
    print(f"\n  ── Invoice preview ──────────────────────────────────")
    for sku, qty in selected:
        line_total = sku.price_cents * qty / 100
        print(f"    {qty} × {sku.label:<42} ${line_total:.2f}")
    print(f"    {'TOTAL':<46} ${total/100:.2f}")
    print()

    confirm = input("  Add these items? (y/n): ").strip().lower()
    if confirm != "y":
        print("  Cancelled.")
        return

    # Create invoice if needed
    if invoice_id is None:
        billing_date = datetime.now(timezone.utc).date().isoformat() if send_now else None
        inv = stripe.Invoice.create(
            customer=customer_id,
            auto_advance=False,
            collection_method="send_invoice",
            days_until_due=14,
            description="GreenGuard USA — Equipment and services",
            metadata={"billing_date": billing_date or "manual", "source": "admin"},
        )
        invoice_id = inv.id
        print(f"\n  Created draft invoice: {invoice_id}")

    # Add line items
    for sku, qty in selected:
        price_id = prices.get(sku.code)
        if not price_id:
            print(f"  ⚠ No Stripe price for {sku.code} — skipping (run stripe_setup.py)")
            continue
        for _ in range(qty):
            stripe.InvoiceItem.create(
                customer=customer_id,
                pricing={"price": price_id},
                invoice=invoice_id,
                description=sku.label,
            )
        print(f"  ✓ Added {qty} × {sku.label}")

    # Finalize and send if requested
    if send_now:
        stripe.Invoice.finalize_invoice(invoice_id)
        stripe.Invoice.send_invoice(invoice_id)
        print(f"\n  Invoice finalized and sent to {email}")
    else:
        billing_info = draft["billing_date"] if draft else "run /billing/run or use --send"
        print(f"\n  Draft saved — will send on: {billing_info}")
        print(f"  To send now: python3 admin_invoice.py --email {email} --send")

    print(f"\n  Stripe invoice: https://dashboard.stripe.com/invoices/{invoice_id}\n")


if __name__ == "__main__":
    main()
