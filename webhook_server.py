"""
GreenGuard USA — Stripe billing orchestrator + admin booking server.

Billing is delayed BILLING_DELAY_DAYS (default 5) days after appointment date:
  - Recurring: Stripe subscription with trial_end = appointment + delay
  - One-time:  Draft invoice created at booking, finalized by /billing/run daily job

Run locally:
    uvicorn webhook_server:app --host 0.0.0.0 --port 8000

Deploy to Render:
    Start command: uvicorn webhook_server:app --host 0.0.0.0 --port $PORT
"""

import json
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import calcom_client
import db
import stripe_client
import sms_client

load_dotenv()

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.greenguard-usa.com", "https://greenguard-usa.com"],
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type"],
)

db.init_db()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_prices() -> dict[str, str]:
    if not PRICES_FILE.exists():
        raise RuntimeError("stripe_prices.json not found — run stripe_setup.py first")
    return json.loads(PRICES_FILE.read_text())


# ── Daily billing runner ──────────────────────────────────────────────────────

@app.post("/billing/run")
async def billing_run(request: Request, _: None = Depends(_require_admin)):
    """
    Called daily by GitHub Actions at 6am CT.
    Finalizes and sends all draft Stripe invoices where billing_date <= today.
    Requires HTTP Basic auth (ADMIN_PASSWORD env var).
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

    if alerts and sms_client.ADMIN_SMS:
        lines = [f"GreenGuard billing alert — {len(alerts)} issue(s):"]
        for a in alerts[:3]:
            lines.append(f"• {a.get('email','?')} {a.get('amount','')}: {a.get('alert','')[:80]}")
        sms_client.send_sms(sms_client.ADMIN_SMS, "\n".join(lines))
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


# ── Store cart + checkout ─────────────────────────────────────────────────────

STORE_URL   = "https://www.greenguard-usa.com/store"
SUCCESS_URL = "https://www.greenguard-usa.com/checkout-success"


@app.post("/create-checkout")
async def create_checkout(request: Request):
    """
    Create a Stripe Checkout Session for multiple cart items.
    Body: {items: [{price_id, qty, name}], email?: str}
    Returns: {url: stripe_checkout_url}
    """
    import stripe as _stripe
    _stripe.api_key = os.getenv("STRIPE_LIVE_KEY") or os.getenv("STRIPE_SECRET_KEY", "")

    body = await request.json()
    items = body.get("items", [])
    email = body.get("email")

    if not items:
        raise HTTPException(status_code=400, detail="No items in cart")

    line_items = [{"price": item["price_id"], "quantity": item.get("qty", 1)} for item in items]

    kwargs = dict(
        line_items=line_items,
        mode="payment",
        success_url=SUCCESS_URL + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=STORE_URL,
        automatic_tax={"enabled": False},
        billing_address_collection="required",
        phone_number_collection={"enabled": True},
    )
    if email:
        kwargs["customer_email"] = email

    session = _stripe.checkout.Session.create(**kwargs)
    log.info(f"Checkout session created: {session.id} ({len(items)} items)")
    return JSONResponse({"url": session.url, "session_id": session.id})


@app.post("/cart/save")
async def cart_save(request: Request):
    """Save cart state after checkout initiated for abandoned cart recovery."""
    body = await request.json()
    session_id = body.get("session_id")
    email      = body.get("email", "")
    items      = body.get("items", [])

    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")

    db.save_abandoned_cart(session_id, email, json.dumps(items))
    return JSONResponse({"status": "saved"})


@app.post("/cart/recover")
async def cart_recover(request: Request):
    """
    Check all unrecovered abandoned carts older than 1 hour.
    If Stripe session is not complete, send a recovery email.
    Called by GitHub Actions cron daily.
    """
    import stripe as _stripe
    _stripe.api_key = os.getenv("STRIPE_LIVE_KEY") or os.getenv("STRIPE_SECRET_KEY", "")

    carts   = db.get_abandoned_carts(min_age_minutes=60)
    sent    = 0
    skipped = 0

    for cart in carts:
        session_id = cart["session_id"]
        email      = cart["email"]
        items      = json.loads(cart["items_json"] or "[]")

        if not email:
            skipped += 1
            continue

        try:
            session = _stripe.checkout.Session.retrieve(session_id)
            if session.status == "complete":
                db.mark_cart_recovered(session_id)
                skipped += 1
                continue
        except Exception:
            pass  # session expired — send recovery with new link

        # Build a fresh checkout session for the same items
        try:
            line_items = [{"price": i["price_id"], "quantity": i.get("qty", 1)} for i in items if i.get("price_id")]
            if not line_items:
                skipped += 1
                continue
            new_session = _stripe.checkout.Session.create(
                line_items=line_items,
                mode="payment",
                success_url=SUCCESS_URL + "?session_id={CHECKOUT_SESSION_ID}",
                cancel_url=STORE_URL,
                customer_email=email,
            )
            _send_recovery_email(email, items, new_session.url)
            db.mark_cart_recovered(session_id)
            sent += 1
            log.info(f"Recovery email sent to {email}")
        except Exception as e:
            log.error(f"Recovery failed for {session_id}: {e}")
            skipped += 1

    return JSONResponse({"sent": sent, "skipped": skipped})


def _send_recovery_email(email: str, items: list, checkout_url: str):
    """Send a branded abandoned cart recovery email via Gmail."""
    import base64
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from gmail_client import authenticate

    gmail_service, _ = authenticate()

    item_rows = "".join(
        f'<tr><td style="padding:8px 0;color:#d4e6ca;font-size:14px;border-bottom:1px solid rgba(122,171,130,0.1)">'
        f'{i.get("name","Item")}</td>'
        f'<td style="padding:8px 0;color:#c9a84c;font-weight:800;font-size:14px;text-align:right;border-bottom:1px solid rgba(122,171,130,0.1)">'
        f'×{i.get("qty",1)}</td></tr>'
        for i in items
    )

    html = f"""<!DOCTYPE html>
<html><head><meta name="color-scheme" content="dark">
<link href="https://fonts.googleapis.com/css2?family=Nunito+Sans:wght@700;800;900&display=swap" rel="stylesheet">
</head>
<body style="margin:0;padding:0;background:#0a1a0d;font-family:'Nunito Sans','Helvetica Neue',Arial,sans-serif">
<div style="max-width:520px;margin:0 auto;padding:24px 16px">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:linear-gradient(135deg,#0d1a10,#1a2e1f);border:1px solid rgba(122,171,130,0.2);border-radius:12px;margin-bottom:12px">
    <tr><td style="padding:24px 24px 8px">
      <div style="color:#c9a84c;font-size:10px;font-weight:800;letter-spacing:0.15em;text-transform:uppercase">GreenGuard USA</div>
      <div style="color:#fff;font-size:22px;font-weight:900;margin-top:8px;letter-spacing:-0.02em">You left something behind</div>
      <div style="color:rgba(212,230,202,0.65);font-size:14px;margin-top:6px">Your cart is saved — pick up where you left off.</div>
    </td></tr>
    <tr><td style="padding:16px 24px">
      <table width="100%" cellpadding="0" cellspacing="0" style="background:rgba(0,0,0,0.2);border:1px solid rgba(122,171,130,0.15);border-radius:8px;padding:12px 16px">
        {item_rows}
      </table>
    </td></tr>
    <tr><td style="padding:8px 24px 24px;text-align:center">
      <a href="{checkout_url}" style="display:inline-block;background:#c9a84c;color:#0a1a0d;font-weight:900;font-size:13px;padding:14px 36px;border-radius:6px;text-decoration:none;letter-spacing:0.06em;text-transform:uppercase">Complete Your Purchase</a>
    </td></tr>
  </table>
  <div style="text-align:center;color:rgba(122,171,130,0.2);font-size:10px;letter-spacing:0.08em;text-transform:uppercase">GreenGuard USA · 1519 Parkway, Austin TX 78703</div>
</div></body></html>"""

    msg = MIMEMultipart("alternative")
    msg["To"]      = email
    msg["From"]    = "admin@greenguard-usa.com"
    msg["Subject"] = "Your GreenGuard cart is waiting"
    msg.attach(MIMEText(html, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail_service.users().messages().send(userId="me", body={"raw": raw}).execute()


# ── Health + debug ────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/debug/webhooks")
def debug_webhooks():
    return JSONResponse(db.get_raw_webhooks())
