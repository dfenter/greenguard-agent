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
TX_TAX_RATE        = 8.25   # Texas combined sales tax %
_tax_rate_id: str | None = None


def get_tax_rate_id() -> str:
    """Return cached Texas 8.25% tax rate ID, creating it in Stripe if needed."""
    global _tax_rate_id
    if _tax_rate_id:
        return _tax_rate_id
    # Check stripe_prices.json for cached ID
    import json
    from pathlib import Path
    pf = Path(__file__).parent / "stripe_prices.json"
    if pf.exists():
        data = json.loads(pf.read_text())
        if "_TAX_RATE" in data:
            _tax_rate_id = data["_TAX_RATE"]
            return _tax_rate_id
    # Create new tax rate in Stripe
    _check_key()
    rate = stripe.TaxRate.create(
        display_name="Texas Sales Tax",
        percentage=TX_TAX_RATE,
        inclusive=False,
        jurisdiction="TX",
        description="Texas combined state and local tax 8.25%",
    )
    _tax_rate_id = rate.id
    if pf.exists():
        data = json.loads(pf.read_text())
        data["_TAX_RATE"] = _tax_rate_id
        pf.write_text(json.dumps(data, indent=2))
    return _tax_rate_id


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


def has_payment_method(customer_id: str) -> bool:
    """Return True if customer has a saved payment method (card) in Stripe."""
    _check_key()
    methods = stripe.PaymentMethod.list(customer=customer_id, type="card", limit=1)
    return bool(methods.data)


def _collection_method(customer_id: str) -> dict:
    """
    Return the right Stripe billing params based on whether the customer
    has a saved card. Existing customers auto-charge; new ones get an invoice email.
    """
    if has_payment_method(customer_id):
        return {"collection_method": "charge_automatically"}
    return {"collection_method": "send_invoice", "days_until_due": 14}


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
        default_tax_rates=[get_tax_rate_id()],
        **_collection_method(customer_id),
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
    price_cents: int,
    sku_code: str,
    sku_label: str,
    appointment_dt: datetime,
) -> str:
    """
    Create a draft Stripe invoice with billing scheduled 3 days after appointment.
    Uses price_cents directly so recurring vs one-time price type doesn't matter.
    Invoice stays in draft until the daily billing runner finalizes it.
    Returns invoice ID.
    """
    _check_key()
    billing_date = (appointment_dt + timedelta(days=BILLING_DELAY_DAYS)).date().isoformat()

    invoice = stripe.Invoice.create(
        customer=customer_id,
        auto_advance=False,
        default_tax_rates=[get_tax_rate_id()],
        **_collection_method(customer_id),
        description=f"GreenGuard service — {sku_label}",
        metadata={
            "billing_date": billing_date,
            "sku": sku_code,
            "appointment_date": appointment_dt.date().isoformat(),
        },
    )
    stripe.InvoiceItem.create(
        customer=customer_id,
        amount=price_cents,
        currency="usd",
        invoice=invoice.id,
        description=sku_label,
    )
    return invoice.id


# ── Daily billing runner ──────────────────────────────────────────────────────

def process_due_invoices(prices: dict, addons_config: dict) -> list[dict]:
    """
    Find all draft invoices where billing_date <= today, add any customer default
    add-ons, apply tax, finalize and send.
    Called by the /billing/run endpoint daily at 6am CT.

    prices:        {SKU_CODE: stripe_price_id} from stripe_prices.json
    addons_config: {email: [SKU_CODE, ...]} from customer_addons.json
    """
    _check_key()
    today      = datetime.now(timezone.utc).date().isoformat()
    tax_id     = get_tax_rate_id()
    processed  = []

    # Build email → customer_id lookup for add-on matching
    email_to_id: dict[str, str] = {}

    for invoice in stripe.Invoice.list(status="draft", limit=100).auto_paging_iter():
        meta = {k: v for k, v in invoice.metadata.to_dict().items()} if invoice.metadata else {}
        billing_date = meta.get("billing_date", "")
        if not billing_date or billing_date > today:
            continue

        try:
            customer_id = invoice.customer
            # Resolve customer email for add-on lookup
            if customer_id not in email_to_id:
                cust = stripe.Customer.retrieve(customer_id)
                email_to_id[customer_id] = (cust.email or "").lower()
            email = email_to_id[customer_id]

            # Add customer default add-ons — format: {SKU: quantity}
            for addon_sku, qty in addons_config.get(email, {}).items():
                addon_price_id = prices.get(addon_sku)
                if addon_price_id and qty > 0:
                    for _ in range(qty):
                        stripe.InvoiceItem.create(
                            customer=customer_id,
                            pricing={"price": addon_price_id},
                            invoice=invoice.id,
                        )

            # Ensure tax rate is on the invoice
            stripe.Invoice.modify(invoice.id, default_tax_rates=[tax_id])

            stripe.Invoice.finalize_invoice(invoice.id)
            stripe.Invoice.send_invoice(invoice.id)
            inv = stripe.Invoice.retrieve(invoice.id)
            processed.append({
                "invoice_id":   invoice.id,
                "customer":     customer_id,
                "email":        email,
                "sku":          meta.get("sku", ""),
                "billing_date": billing_date,
                "amount":       f"${inv.amount_due / 100:.2f}",
            })
        except stripe.StripeError as e:
            processed.append({
                "invoice_id":   invoice.id,
                "customer":     invoice.customer,
                "error":        str(e),
                "billing_date": billing_date,
            })

    return processed
