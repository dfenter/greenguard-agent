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
import secrets
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import calcom_client
import db
import sku_engine
import stripe_client

load_dotenv()

CALCOM_WEBHOOK_SECRET = os.getenv("CALCOM_WEBHOOK_SECRET", "")
ADMIN_PASSWORD        = os.getenv("ADMIN_PASSWORD", "")
PRICES_FILE  = Path(__file__).parent / "stripe_prices.json"
ADDONS_FILE  = Path(__file__).parent / "customer_addons.json"

_basic_auth = HTTPBasic()
_TZ_CT = ZoneInfo("America/Chicago")


def _require_admin(credentials: HTTPBasicCredentials = Depends(_basic_auth)):
    ok = ADMIN_PASSWORD and secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not ok:
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic realm=GreenGuard Admin"})

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
    alerts  = [r for r in results if r.get("alert")]
    log.info(f"Billing run: {len(results)} invoice(s) processed, {len(alerts)} alert(s)")
    for r in results:
        if r.get("alert"):
            log.warning(f"ALERT {r['email']}: {r['alert']}")
        if "error" in r:
            log.error(f"Invoice {r['invoice_id']} failed: {r['error']}")
        else:
            phase = r.get("phase", "")
            log.info(f"[{phase}] {r.get('email')} — {r.get('amount')} — {r.get('action', r.get('sku', ''))}")
    return JSONResponse({
        "processed": len(results),
        "alerts":    len(alerts),
        "invoices":  results,
    })


# ── Admin booking page ───────────────────────────────────────────────────────

_ADMIN_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GreenGuard — New Booking</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0f4f0;color:#1a2e1a;min-height:100vh;padding:24px 16px}}
.card{{max-width:540px;margin:0 auto;background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);padding:32px}}
h1{{font-size:22px;color:#2d5a2d;margin-bottom:4px}}
.sub{{color:#666;font-size:14px;margin-bottom:28px}}
.field{{margin-bottom:18px}}
label{{display:block;font-size:13px;font-weight:600;color:#444;margin-bottom:6px}}
input,select,textarea{{width:100%;padding:10px 12px;border:1px solid #d4d4d4;border-radius:8px;font-size:15px;color:#1a2e1a;background:#fff;transition:border-color .15s}}
input:focus,select:focus,textarea:focus{{outline:none;border-color:#2d5a2d}}
.row{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
button{{width:100%;padding:13px;background:#2d5a2d;color:#fff;border:none;border-radius:8px;font-size:16px;font-weight:600;cursor:pointer;margin-top:8px;transition:background .15s}}
button:hover{{background:#1e3d1e}}
.msg{{padding:12px;border-radius:8px;margin-bottom:20px;font-size:14px}}
.ok{{background:#e8f5e8;color:#2d5a2d;border:1px solid #a5d6a7}}
.err{{background:#ffeaea;color:#c62828;border:1px solid #ef9a9a}}
@media(max-width:480px){{.row{{grid-template-columns:1fr}}}}
</style></head>
<body><div class="card">
<h1>GreenGuard USA</h1>
<p class="sub">Create a new customer booking</p>
{msg}
<form method="post" action="/admin/book">
<div class="field"><label>Service Type</label>
<select name="event_type_id" required>{options}</select></div>
<div class="row">
<div class="field"><label>First Name</label><input type="text" name="first_name" required placeholder="Jane"></div>
<div class="field"><label>Last Name</label><input type="text" name="last_name" required placeholder="Smith"></div>
</div>
<div class="field"><label>Email</label><input type="email" name="email" required placeholder="jane@example.com"></div>
<div class="field"><label>Phone</label><input type="tel" name="phone" placeholder="(512) 555-1234"></div>
<div class="field"><label>Service Address</label><input type="text" name="address" required placeholder="1234 Oak St, Austin TX 78701"></div>
<div class="field"><label>Date &amp; Time (Central Time)</label><input type="datetime-local" name="start" required></div>
<div class="field"><label>Notes (optional)</label><textarea name="notes" rows="3" placeholder="Any special requests…"></textarea></div>
<button type="submit">Create Booking</button>
</form></div></body></html>"""


def _event_type_options() -> str:
    try:
        types = calcom_client.list_event_types()
        return "".join(f'<option value="{et["id"]}">{et["title"]}</option>' for et in types)
    except Exception:
        return '<option value="">Could not load event types</option>'


@app.get("/admin", response_class=HTMLResponse)
def admin_page(_: None = Depends(_require_admin)):
    return _ADMIN_HTML.format(msg="", options=_event_type_options())


@app.post("/admin/book", response_class=HTMLResponse)
def admin_book(
    _: None = Depends(_require_admin),
    event_type_id: int  = Form(...),
    first_name: str     = Form(...),
    last_name: str      = Form(...),
    email: str          = Form(...),
    phone: str          = Form(""),
    address: str        = Form(...),
    start: str          = Form(...),   # datetime-local: "2026-05-21T10:00"
    notes: str          = Form(""),
):
    try:
        dt_ct  = datetime.fromisoformat(start).replace(tzinfo=_TZ_CT)
        dt_utc = dt_ct.astimezone(ZoneInfo("UTC")).isoformat()
        calcom_client.create_booking(
            event_type_id=event_type_id,
            start_utc_iso=dt_utc,
            customer_name=f"{first_name} {last_name}".strip(),
            customer_email=email,
            customer_phone=phone,
            service_address=address,
            notes=notes,
        )
        msg = f'<div class="msg ok">Booking created for {first_name} {last_name} — draft email will appear in Gmail within 60 seconds.</div>'
    except Exception as exc:
        log.error("Admin booking error: %s", exc)
        msg = f'<div class="msg err">Booking failed: {exc}</div>'

    return _ADMIN_HTML.format(msg=msg, options=_event_type_options())


# ── Health + debug ────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/debug/webhooks")
def debug_webhooks():
    return JSONResponse(db.get_raw_webhooks())
