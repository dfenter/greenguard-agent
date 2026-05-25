"""
Stripe API wrapper for GreenGuard USA.

Billing is intentionally delayed after the appointment date:
  - Subscriptions: trial_end = appointment_dt + BILLING_DELAY_DAYS (Stripe auto-charges after)
  - One-time:      draft invoice created at booking, finalized by daily billing runner

Set BILLING_DELAY_DAYS in .env to override (default: 5).
"""

import os
from datetime import datetime, timedelta, timezone

import stripe
from dotenv import load_dotenv

load_dotenv()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

BILLING_DELAY_DAYS = int(os.getenv("BILLING_DELAY_DAYS", "5"))
TX_TAX_RATE        = 8.25   # Texas combined sales tax %
_tax_rate_id: str | None = None


def get_tax_rate_id() -> str:
    """Return cached Texas 8.25% tax rate ID, creating it in Stripe if needed.

    Source-of-truth ordering:
      1. STRIPE_TAX_RATE_ID env var (matches what the portal uses)
      2. _TAX_RATE in stripe_prices_live.json
      3. Create fresh in Stripe and cache to _live.json
    """
    global _tax_rate_id
    if _tax_rate_id:
        return _tax_rate_id
    env_id = os.getenv("STRIPE_TAX_RATE_ID")
    if env_id:
        _tax_rate_id = env_id
        return _tax_rate_id
    import json
    from pathlib import Path
    pf = Path(__file__).parent / "stripe_prices_live.json"
    if pf.exists():
        data = json.loads(pf.read_text())
        if data.get("_TAX_RATE"):
            _tax_rate_id = data["_TAX_RATE"]
            return _tax_rate_id
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

def has_successful_payment(customer_id: str) -> bool:
    """Return True if the customer has at least one paid invoice on record."""
    _check_key()
    paid = stripe.Invoice.list(customer=customer_id, status="paid", limit=1)
    return bool(paid.data)


def _collect_failed_invoice(invoice) -> dict:
    """
    Attempt to collect a single open/past-due subscription invoice.
    Only proceeds if the customer has a prior successful payment on record.
    - charge_automatically: force a new payment attempt via Invoice.pay()
    - send_invoice: re-send the payment link email via Invoice.send_invoice()
    Returns a result dict.
    """
    inv_id = invoice.id
    email  = ""
    try:
        cust  = stripe.Customer.retrieve(invoice.customer)
        email = (cust.email or "").lower()
        amount = f"${invoice.amount_due / 100:.2f}"

        # Only auto-collect if customer has successfully paid before
        if not has_successful_payment(invoice.customer):
            return {
                "invoice_id":  inv_id,
                "customer":    invoice.customer,
                "email":       email,
                "amount":      amount,
                "action":      "skipped — no prior successful payment",
                "alert":       f"NO PAYMENT HISTORY — {email} has never successfully paid. Manual follow-up required.",
                "failed_retry": False,
            }

        if invoice.collection_method == "charge_automatically":
            stripe.Invoice.pay(inv_id, forgive=True)
            action = "retried charge"
        else:
            stripe.Invoice.send_invoice(inv_id)
            action = "resent payment link"

        return {
            "invoice_id":   inv_id,
            "customer":     invoice.customer,
            "email":        email,
            "amount":       amount,
            "action":       action,
            "failed_retry": False,
        }
    except stripe.StripeError as e:
        return {
            "invoice_id":   inv_id,
            "customer":     invoice.customer,
            "email":        email,
            "error":        str(e),
            "failed_retry": True,
        }


def process_due_invoices(prices: dict, addons_config: dict) -> list[dict]:
    """
    Phase 1 — Draft invoices: finalize and send any draft invoices whose
    billing_date <= today, adding customer default add-ons and tax.

    Phase 2 — Failed subscription invoices: for any open subscription invoice
    that is past its due date, attempt to collect each one individually
    (retry charge or re-send payment link).

    Called by the /billing/run endpoint daily at 6am CT.

    prices:        {SKU_CODE: stripe_price_id} from stripe_prices.json
    addons_config: {email: {SKU_CODE: quantity}} from customer_addons.json
    """
    _check_key()
    today     = datetime.now(timezone.utc).date().isoformat()
    tax_id    = get_tax_rate_id()
    processed = []

    # ── Phase 1: draft invoices due today ────────────────────────────────────
    email_to_id: dict[str, str] = {}

    for invoice in stripe.Invoice.list(status="draft", limit=100).auto_paging_iter():
        meta         = invoice.metadata.to_dict() if invoice.metadata else {}
        billing_date = meta.get("billing_date", "")
        if not billing_date or billing_date > today:
            continue

        try:
            customer_id = invoice.customer
            if customer_id not in email_to_id:
                cust = stripe.Customer.retrieve(customer_id)
                email_to_id[customer_id] = (cust.email or "").lower()
            email = email_to_id[customer_id]

            for addon_sku, qty in addons_config.get(email, {}).items():
                if not isinstance(qty, int):
                    continue
                addon_price_id = prices.get(addon_sku)
                if addon_price_id and qty > 0:
                    for _ in range(qty):
                        stripe.InvoiceItem.create(
                            customer=customer_id,
                            pricing={"price": addon_price_id},
                            invoice=invoice.id,
                        )

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
                "phase":        "draft",
            })
        except stripe.StripeError as e:
            processed.append({
                "invoice_id":   invoice.id,
                "customer":     invoice.customer,
                "error":        str(e),
                "billing_date": billing_date,
                "phase":        "draft",
            })

    # ── Phase 2: open subscription invoices with failed payments ─────────────
    # One collection attempt per customer per run (oldest failed invoice first).
    # Avoids flooding the customer with multiple emails in a single day.
    seen_customers: set[str] = set()

    for sub_status in ("past_due", "unpaid"):
        for sub in stripe.Subscription.list(status=sub_status, limit=100).auto_paging_iter():
            customer_id = sub.customer
            if customer_id in seen_customers:
                continue

            # Get oldest open invoice first (ascending created order)
            open_invoices = stripe.Invoice.list(
                customer=customer_id,
                subscription=sub.id,
                status="open",
                limit=100,
            ).auto_paging_iter()

            open_list = sorted(
                (inv for inv in open_invoices if inv.amount_due > 0),
                key=lambda i: i.created,
            )
            if not open_list:
                continue

            seen_customers.add(customer_id)
            oldest       = open_list[0]
            failed_count = len(open_list)

            result = _collect_failed_invoice(oldest)
            result["phase"]        = "failed_subscription"
            result["failed_count"] = failed_count
            # Flag customers with multiple failures for admin review
            if failed_count > 1:
                total_owed = sum(i.amount_due for i in open_list)
                result["alert"] = (
                    f"MULTIPLE FAILURES — {failed_count} unpaid invoices, "
                    f"${total_owed / 100:.2f} total owed. Collecting oldest first."
                )
            processed.append(result)

    return processed
