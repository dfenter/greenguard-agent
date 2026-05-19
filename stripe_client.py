"""
Stripe API wrapper for GreenGuard USA.
Handles customer lookup/creation, subscriptions, and one-time invoices.
"""

import os
import stripe
from dotenv import load_dotenv

load_dotenv()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")


def _check_key():
    if not stripe.api_key:
        raise RuntimeError("STRIPE_SECRET_KEY not set in .env")


# ── Customers ─────────────────────────────────────────────────────────────────

def find_customer(email: str) -> str | None:
    """Return Stripe customer ID for this email, or None if not found."""
    _check_key()
    results = stripe.Customer.search(query=f'email:"{email}"', limit=1)
    if results.data:
        return results.data[0].id
    return None


def create_customer(name: str, email: str, phone: str | None = None) -> str:
    """Create a Stripe customer and return their customer ID."""
    _check_key()
    params: dict = {"name": name, "email": email}
    if phone:
        params["phone"] = phone
    customer = stripe.Customer.create(**params)
    return customer.id


def get_or_create_customer(name: str, email: str, phone: str | None = None) -> str:
    """Find existing customer by email or create a new one."""
    cid = find_customer(email)
    if cid:
        return cid
    return create_customer(name, email, phone)


# ── Subscriptions ─────────────────────────────────────────────────────────────

def get_active_subscription(customer_id: str) -> dict | None:
    """Return active subscription dict or None."""
    _check_key()
    subs = stripe.Subscription.list(customer=customer_id, status="active", limit=1)
    if subs.data:
        s = subs.data[0]
        return {"id": s.id, "price_id": s["items"].data[0].price.id}
    return None


def create_subscription(customer_id: str, price_id: str) -> str:
    """
    Create a monthly subscription. Returns subscription ID.
    Uses trial_end='now' so billing starts immediately on the next billing cycle.
    Invoice is created and sent immediately on creation.
    """
    _check_key()
    sub = stripe.Subscription.create(
        customer=customer_id,
        items=[{"price": price_id}],
        payment_behavior="default_incomplete",
        expand=["latest_invoice.payment_intent"],
        collection_method="send_invoice",
        days_until_due=14,
    )
    return sub.id


# ── One-time invoices ─────────────────────────────────────────────────────────

def add_invoice_item(customer_id: str, price_id: str, description: str) -> None:
    """Add a line item to the customer's pending invoice."""
    _check_key()
    stripe.InvoiceItem.create(
        customer=customer_id,
        price=price_id,
        description=description,
    )


def finalize_and_send_invoice(customer_id: str, description: str = "") -> str:
    """
    Create a Stripe invoice from pending invoice items, finalize, and send.
    Returns invoice ID.
    """
    _check_key()
    invoice = stripe.Invoice.create(
        customer=customer_id,
        collection_method="send_invoice",
        days_until_due=14,
        description=description,
    )
    invoice = stripe.Invoice.finalize_invoice(invoice.id)
    stripe.Invoice.send_invoice(invoice.id)
    return invoice.id
