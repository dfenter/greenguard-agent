"""
GreenGuard USA — Cal.com webhook receiver + Stripe billing orchestrator.

Billing is delayed 3 days after appointment date:
  - Recurring: Stripe subscription with trial_end = appointment + 3 days
  - One-time:  Draft invoice created at booking, finalized by /billing/run daily job

Run locally:
    uvicorn webhook_server:app --host 0.0.0.0 --port 8000

Deploy to Render:
    Start command: uvicorn webhook_server:app --host 0.0.0.0 --port $PORT
"""

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

import db
import sku_engine
import stripe_client

load_dotenv()

CALCOM_WEBHOOK_SECRET = os.getenv("CALCOM_WEBHOOK_SECRET", "")
PRICES_FILE  = Path(__file__).parent / "stripe_prices.json"
ADDONS_FILE  = Path(__file__).parent / "customer_addons.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("webhook")

app = FastAPI(title="GreenGuard Webhook Server")

db.init_db()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _verify_signature(body: bytes, sig_header: str | None) -> bool:
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


def _parse_appointment_dt(booking: dict) -> datetime:
    """Parse Cal.com startTime into a UTC-aware datetime."""
    raw = booking.get("startTime", "")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


# ── Booking webhook ───────────────────────────────────────────────────────────

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
    log.info(f"Webhook received: trigger={trigger}")
    db.record_raw_webhook(trigger, body.decode())

    if trigger != "BOOKING_CREATED":
        return JSONResponse({"status": "ignored", "trigger": trigger})

    booking = payload.get("payload", {})
    uid     = booking.get("uid", "")
    slug    = booking.get("type") or booking.get("eventTypeSlug") or booking.get("eventType", {}).get("slug", "")

    attendees    = booking.get("attendees", [])
    attendee     = next((a for a in attendees if not a.get("host")), attendees[0] if attendees else {})
    name         = attendee.get("name", "Unknown")
    email        = attendee.get("email", "")
    phone        = attendee.get("phoneNumber") or attendee.get("phone")
    appointment_dt = _parse_appointment_dt(booking)

    log.info(f"Booking: uid={uid} slug={slug} email={email} appt={appointment_dt.date()}")

    if db.is_webhook_processed(uid):
        log.info(f"Duplicate webhook uid={uid} — skipping")
        return JSONResponse({"status": "duplicate"})

    if not email:
        log.warning(f"No email for uid={uid}")
        return JSONResponse({"status": "no_email"})

    sku = sku_engine.resolve(slug)
    if sku is None:
        log.warning(f"Unknown slug '{slug}' for uid={uid}")
        db.record_webhook(uid, slug or "UNKNOWN", "", "")
        return JSONResponse({"status": "unknown_slug", "slug": slug})

    try:
        prices = _load_prices()
    except RuntimeError as e:
        log.error(str(e))
        raise HTTPException(status_code=500, detail=str(e))

    price_id = prices.get(sku.code)
    if not price_id:
        log.error(f"No Stripe price ID for {sku.code}")
        raise HTTPException(status_code=500, detail=f"Missing price for {sku.code}")

    customer_id = stripe_client.get_or_create_customer(name, email, phone)
    log.info(f"Stripe customer: {customer_id} ({email})")

    invoice_id = ""

    if sku.billing_type == "recurring":
        existing = stripe_client.get_active_subscription(customer_id)
        if existing:
            log.info(f"{email} already has subscription {existing['id']} ({existing['status']}) — skipping")
        else:
            sub_id = stripe_client.create_subscription(customer_id, price_id, appointment_dt)
            billing_start = appointment_dt.date().isoformat()
            log.info(f"Created subscription {sub_id} for {email} — first charge 3 days after {billing_start}")

    elif sku.billing_type == "one_time" and sku.price_cents > 0:
        invoice_id = stripe_client.create_draft_invoice(
            customer_id, sku.price_cents, sku.code, sku.label, appointment_dt,
        )
        billing_date = (appointment_dt.date().__add__(__import__("datetime").timedelta(days=3))).isoformat()
        log.info(f"Draft invoice {invoice_id} for {email} — sends {billing_date}")

    else:
        log.info(f"Zero-cost visit ({sku.code}) for {email} — no billing action")

    db.record_webhook(uid, sku.code, customer_id, invoice_id)
    return JSONResponse({
        "status":   "ok",
        "sku":      sku.code,
        "customer": customer_id,
        "billing":  f"3 days after {appointment_dt.date()}",
    })


# ── Daily billing runner ──────────────────────────────────────────────────────

@app.post("/billing/run")
async def billing_run(request: Request):
    """
    Called daily by GitHub Actions at 6am CT.
    Finalizes and sends all draft Stripe invoices where billing_date <= today.
    """
    prices = _load_prices()
    addons = json.loads(ADDONS_FILE.read_text()) if ADDONS_FILE.exists() else {}
    results = stripe_client.process_due_invoices(prices, addons)
    log.info(f"Billing run: {len(results)} invoice(s) processed")
    for r in results:
        if "error" in r:
            log.error(f"Invoice {r['invoice_id']} failed: {r['error']}")
        else:
            log.info(f"Sent invoice {r['invoice_id']} — {r.get('amount')} — {r['sku']}")
    return JSONResponse({"processed": len(results), "invoices": results})


# ── Health + debug ────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/debug/webhooks")
def debug_webhooks():
    return JSONResponse(db.get_raw_webhooks())
