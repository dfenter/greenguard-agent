"""
Stripe API wrapper for GreenGuard USA.

Billing is intentionally delayed 3 days after the appointment date:
  - Subscriptions: trial_end = appointment_dt + 3 days (Stripe auto-charges after)
  - One-time:      draft invoice created at booking, finalized by daily billing runner
"""

import os
from datetime import datetime, timedelta, timezone

import stripe
from dotenv import load_dotenv

load_dotenv()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

BILLING_DELAY_DAYS = 3


def _check_key():
    if not stripe.api_key:
        raise RuntimeError("STRIPE_SECRET_KEY not set in .env")


# ── Customers ─────────────────────────────────────────────────────────────────

def find_customer(email: str) -> str | None:
    _check_key()
    results = stripe.Customer.search(query=f'email:"{email}"', limit=1)
    return results.data[0].id if results.data else None


def create_customer(name: str, email: str, phone: str | None = None) -> str:
    _check_key()
    params: dict = {"name": name, "email": email}
    if phone:
        params["phone"] = phone
    return stripe.Customer.create(**params).id


def get_or_create_customer(name: str, email: str, phone: str | None = None) -> str:
    return find_customer(email) or create_customer(name, email, phone)


# ── Subscriptions (recurring billing) ────────────────────────────────────────

def get_active_subscription(customer_id: str) -> dict | None:
    """Return active or trialing subscription, or None."""
    _check_key()
    for status in ("active", "trialing"):
        subs = stripe.Subscription.list(customer=customer_id, status=status, limit=1)
        if subs.data:
            s = subs.data[0]
            return {"id": s.id, "price_id": s["items"].data[0].price.id, "status": status}
    return None


def create_subscription(
    customer_id: str,
    price_id: str,
    appointment_dt: datetime,
    quantity: int = 1,
) -> str:
    """
    Create a monthly subscription with a trial ending 3 days after the appointment.
    quantity > 1 handles multiple units (e.g. 2 Mosqitter rentals = quantity=2).
    Stripe auto-charges on trial_end and monthly thereafter.
    Returns subscription ID.
    """
    _check_key()
    billing_start = appointment_dt + timedelta(days=BILLING_DELAY_DAYS)
    trial_end_ts  = int(billing_start.astimezone(timezone.utc).timestamp())

    sub = stripe.Subscription.create(
        customer=customer_id,
        items=[{"price": price_id, "quantity": quantity}],
        trial_end=trial_end_ts,
        collection_method="send_invoice",
        days_until_due=14,
        metadata={"appointment_date": appointment_dt.date().isoformat()},
    )
    return sub.id


def update_subscription_quantity(subscription_id: str, quantity: int) -> bool:
    """Update the unit quantity on an existing subscription (e.g. add a second Mosqitter)."""
    _check_key()
    try:
        sub = stripe.Subscription.retrieve(subscription_id)
        item_id = sub["items"].data[0].id
        stripe.Subscription.modify(
            subscription_id,
            items=[{"id": item_id, "quantity": quantity}],
        )
        return True
    except stripe.StripeError:
        return False


# ── One-time invoices ─────────────────────────────────────────────────────────

def create_draft_invoice(
    customer_id: str,
    price_id: str,
    sku_code: str,
    sku_label: str,
    appointment_dt: datetime,
) -> str:
    """
    Create a draft Stripe invoice with billing scheduled 3 days after appointment.
    Invoice stays in draft until the daily billing runner finalizes it.
    Returns invoice ID.
    """
    _check_key()
    billing_date = (appointment_dt + timedelta(days=BILLING_DELAY_DAYS)).date().isoformat()

    invoice = stripe.Invoice.create(
        customer=customer_id,
        auto_advance=False,          # stays as draft until we explicitly finalize
        collection_method="send_invoice",
        days_until_due=14,
        description=f"GreenGuard service — {sku_label}",
        metadata={
            "billing_date": billing_date,
            "sku": sku_code,
            "appointment_date": appointment_dt.date().isoformat(),
        },
    )
    # Attach the line item directly to this draft invoice
    stripe.InvoiceItem.create(
        customer=customer_id,
        price=price_id,
        invoice=invoice.id,
        description=sku_label,
    )
    return invoice.id


# ── Daily billing runner ──────────────────────────────────────────────────────

def process_due_invoices() -> list[dict]:
    """
    Find all draft invoices where billing_date <= today and finalize + send them.
    Called by the /billing/run endpoint daily at 6am CT.
    Returns list of {invoice_id, customer_email, sku, billing_date} for each processed.
    """
    _check_key()
    today = datetime.now(timezone.utc).date().isoformat()
    processed = []

    for invoice in stripe.Invoice.list(status="draft", limit=100).auto_paging_iter():
        billing_date = (invoice.metadata or {}).get("billing_date", "")
        if not billing_date or billing_date > today:
            continue

        try:
            stripe.Invoice.finalize_invoice(invoice.id)
            stripe.Invoice.send_invoice(invoice.id)
            processed.append({
                "invoice_id":     invoice.id,
                "customer":       invoice.customer,
                "sku":            (invoice.metadata or {}).get("sku", ""),
                "billing_date":   billing_date,
                "amount":         f"${invoice.amount_due / 100:.2f}",
            })
        except stripe.StripeError as e:
            processed.append({
                "invoice_id":  invoice.id,
                "customer":    invoice.customer,
                "error":       str(e),
                "billing_date": billing_date,
            })

    return processed
