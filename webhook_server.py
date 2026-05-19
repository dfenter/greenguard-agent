"""
GreenGuard USA — Cal.com webhook receiver + Stripe billing orchestrator.

Receives Cal.com booking webhooks, resolves the SKU, and creates the
appropriate Stripe subscription (recurring) or invoice (one-time).

Run locally:
    uvicorn webhook_server:app --host 0.0.0.0 --port 8000

Deploy to Render/Railway:
    Set start command to: uvicorn webhook_server:app --host 0.0.0.0 --port $PORT
"""

import hashlib
import hmac
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

import db
import sku_engine
import stripe_client

load_dotenv()

CALCOM_WEBHOOK_SECRET = os.getenv("CALCOM_WEBHOOK_SECRET", "")
PRICES_FILE = Path(__file__).parent / "stripe_prices.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("webhook")

app = FastAPI(title="GreenGuard Webhook Server")

db.init_db()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _verify_signature(body: bytes, sig_header: str | None) -> bool:
    """Verify Cal.com HMAC-SHA256 webhook signature."""
    if not CALCOM_WEBHOOK_SECRET:
        log.warning("CALCOM_WEBHOOK_SECRET not set — skipping signature verification")
        return True
    if not sig_header:
        return False
    expected = hmac.new(
        CALCOM_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, sig_header.lstrip("sha256="))


def _load_prices() -> dict[str, str]:
    if not PRICES_FILE.exists():
        raise RuntimeError("stripe_prices.json not found — run stripe_setup.py first")
    return json.loads(PRICES_FILE.read_text())


# ── Webhook endpoint ──────────────────────────────────────────────────────────

@app.post("/webhook/calcom")
async def calcom_webhook(
    request: Request,
    x_cal_signature_256: str | None = Header(default=None),
):
    body = await request.body()

    if not _verify_signature(body, x_cal_signature_256):
        log.warning("Webhook signature verification failed")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    trigger = payload.get("triggerEvent", "")
    if trigger != "BOOKING_CREATED":
        return JSONResponse({"status": "ignored", "trigger": trigger})

    booking = payload.get("payload", {})
    uid     = booking.get("uid", "")
    slug    = booking.get("eventTypeSlug") or booking.get("eventType", {}).get("slug", "")

    # Attendee info
    attendees = booking.get("attendees", [])
    attendee  = next((a for a in attendees if not a.get("host")), attendees[0] if attendees else {})
    name      = attendee.get("name", "Unknown")
    email     = attendee.get("email", "")
    phone     = attendee.get("phoneNumber") or attendee.get("phone")

    log.info(f"Booking received: uid={uid} slug={slug} email={email}")

    # Idempotency check
    if db.is_webhook_processed(uid):
        log.info(f"Duplicate webhook for uid={uid} — skipping")
        return JSONResponse({"status": "duplicate"})

    if not email:
        log.warning(f"No email for booking uid={uid} — cannot create Stripe customer")
        return JSONResponse({"status": "no_email"})

    # SKU resolution
    sku = sku_engine.resolve(slug)
    if sku is None:
        log.warning(f"Unknown slug '{slug}' for booking uid={uid}")
        db.record_webhook(uid, slug or "UNKNOWN", "", "")
        return JSONResponse({"status": "unknown_slug", "slug": slug})

    # Load Stripe price IDs
    try:
        prices = _load_prices()
    except RuntimeError as e:
        log.error(str(e))
        raise HTTPException(status_code=500, detail=str(e))

    price_id = prices.get(sku.code)
    if not price_id:
        log.error(f"No Stripe price ID for SKU {sku.code} — run stripe_setup.py")
        raise HTTPException(status_code=500, detail=f"Missing price for {sku.code}")

    # Get or create Stripe customer
    customer_id = stripe_client.get_or_create_customer(name, email, phone)
    log.info(f"Stripe customer: {customer_id} ({email})")

    invoice_id = ""

    if sku.billing_type == "recurring":
        existing = stripe_client.get_active_subscription(customer_id)
        if existing:
            log.info(f"Customer {email} already has active subscription {existing['id']} — skipping")
        else:
            sub_id = stripe_client.create_subscription(customer_id, price_id)
            log.info(f"Created subscription {sub_id} for {email} ({sku.code})")

    elif sku.billing_type == "one_time" and sku.price_cents > 0:
        stripe_client.add_invoice_item(customer_id, price_id, sku.label)
        invoice_id = stripe_client.finalize_and_send_invoice(
            customer_id,
            description=f"GreenGuard service — {sku.label}",
        )
        log.info(f"Sent invoice {invoice_id} to {email} ({sku.code})")

    else:
        log.info(f"Zero-cost visit ({sku.code}) for {email} — no billing action")

    db.record_webhook(uid, sku.code, customer_id, invoice_id)
    return JSONResponse({"status": "ok", "sku": sku.code, "customer": customer_id})


@app.get("/health")
def health():
    return {"status": "ok"}
