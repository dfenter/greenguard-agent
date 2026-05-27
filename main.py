"""
Greenguard USA — Gmail Draft Agent
Polls Gmail (or uses Pub/Sub push) for unread emails, runs each through Claude,
and creates draft replies. A human reviews and sends each draft.

Setup:
  1. Copy .env.example → .env and fill in ANTHROPIC_API_KEY
  2. Download credentials.json from Google Cloud Console (OAuth 2.0 desktop app)
  3. pip install -r requirements.txt
  4. python main.py   ← first run opens a browser to authorize Gmail access

Install as a background service (auto-starts on login, restarts on crash):
  cp greenguard-agent.plist ~/Library/LaunchAgents/com.greenguard.agent.plist
  launchctl load ~/Library/LaunchAgents/com.greenguard.agent.plist
"""

import os
import sys
import re
import time
import logging
from dotenv import load_dotenv

from gmail_client import (
    authenticate,
    get_or_create_label,
    get_unread_emails,
    create_draft_reply,
    mark_processed,
    send_email,
)
from calendar_client import get_calendar_service, get_route_distances
from agent import select_template, EmailDraft
from appointment_parser import parse_appointment_email, is_appointment_notification, AppointmentInfo
from address_lookup import lookup_property
from template_loader import get_all_templates
from spam_filter import is_spam
import db
from digest import should_send_digest, build_digest
import gemini_client

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
POLL_INTERVAL      = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
PROCESSED_LABEL    = os.getenv("PROCESSED_LABEL", "Greenguard-Processed")
CALENDAR_TIMEZONE  = os.getenv("CALENDAR_TIMEZONE", "America/Chicago")
SENDER_EMAIL       = "admin@greenguard-usa.com"
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
DEPOT_ADDRESS      = os.getenv("DEPOT_ADDRESS", "1519 Parkway Austin TX 78703")
PUBSUB_PROJECT_ID  = os.getenv("PUBSUB_PROJECT_ID", "")
PUBSUB_SUB_ID      = os.getenv("PUBSUB_SUBSCRIPTION_ID", "")
PUBSUB_TOPIC       = os.getenv("PUBSUB_TOPIC_NAME", "")  # projects/ID/topics/NAME

# Richer classification labels — created automatically if they don't exist
_CLASS_LABEL_NAMES = {
    "scheduling":   "Greenguard/Scheduling",
    "question":     "Greenguard/Question",
    "complaint":    "Greenguard/Complaint",
    "appointment_notification": "Greenguard/Appointment",
    "other":        "Greenguard/Other",
}

# ---------------------------------------------------------------------------
# In-memory cache for calendar slots (one Calendar API call per poll cycle)
# ---------------------------------------------------------------------------
_slots_cache: list[str] = []
_slots_cache_ts: float = 0.0
SLOTS_CACHE_TTL = POLL_INTERVAL - 30


def _get_slots(calendar_service) -> list[str]:
    global _slots_cache, _slots_cache_ts
    if time.time() - _slots_cache_ts < SLOTS_CACHE_TTL and _slots_cache:
        return _slots_cache
    _slots_cache = get_available_slots(calendar_service, timezone=CALENDAR_TIMEZONE)
    _slots_cache_ts = time.time()
    return _slots_cache


# ---------------------------------------------------------------------------
# Daily route log
# ---------------------------------------------------------------------------
def log_daily_route(calendar_service) -> list[dict]:
    if not GOOGLE_MAPS_API_KEY:
        log.warning("GOOGLE_MAPS_API_KEY not set — skipping route distances")
        return []
    try:
        route = get_route_distances(
            calendar_service,
            maps_api_key=GOOGLE_MAPS_API_KEY,
            origin=DEPOT_ADDRESS,
            timezone=CALENDAR_TIMEZONE,
        )
        if not route:
            log.info("No appointments today.")
            return []
        log.info("=== Today's Route (from %s) ===", DEPOT_ADDRESS)
        total_miles = 0.0
        for appt in route:
            if appt["distance_miles"] is not None:
                total_miles += appt["distance_miles"]
                log.info(
                    "  %s  |  %s  |  %.1f mi / ~%d min drive",
                    appt["start"].strftime("%I:%M%p"),
                    appt["summary"][:60],
                    appt["distance_miles"],
                    appt["duration_minutes"],
                )
            else:
                log.info(
                    "  %s  |  %s  |  no address (%s)",
                    appt["start"].strftime("%I:%M%p"),
                    appt["summary"][:60],
                    appt.get("address") or "unknown",
                )
        log.info("  Total driving: %.1f miles", total_miles)
        return route
    except Exception as exc:
        log.warning("Could not calculate route distances: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Daily digest
# ---------------------------------------------------------------------------
def maybe_send_digest(gmail_service, calendar_service) -> None:
    last_sent = float(db.get_state("last_digest_sent", "0"))
    if not should_send_digest(last_sent, CALENDAR_TIMEZONE):
        return

    since_ts = time.time() - 86400
    stats = db.get_stats(since_ts)
    high_urgency = db.get_high_urgency_emails(since_ts)
    route = log_daily_route(calendar_service)

    subject, body = build_digest(stats, high_urgency, route, CALENDAR_TIMEZONE)
    try:
        send_email(gmail_service, SENDER_EMAIL, subject, body)
        db.set_state("last_digest_sent", str(time.time()))
        log.info("Daily digest sent: %s", subject)
    except Exception as exc:
        log.warning("Could not send digest: %s", exc)


# ---------------------------------------------------------------------------
# Draft helpers
# ---------------------------------------------------------------------------
_templates_cache: dict[str, str] = {}
_templates_cache_ts: float = 0.0
_TEMPLATES_CACHE_TTL = 3600  # refresh Gmail templates once per hour


def _get_templates(gmail_service) -> dict[str, str]:
    global _templates_cache, _templates_cache_ts
    if time.time() - _templates_cache_ts < _TEMPLATES_CACHE_TTL and _templates_cache:
        return _templates_cache
    try:
        _templates_cache = get_all_templates(gmail_service)
        _templates_cache_ts = time.time()
        log.info("Loaded %d Gmail templates", len(_templates_cache))
    except Exception as exc:
        log.warning("Could not load Gmail templates: %s", exc)
    return _templates_cache


def _draft_assessment(gmail_service, subject: str, body: str) -> EmailDraft:
    """Parse Cal.com booking → geocode → pick Gmail template → build draft."""
    appt = parse_appointment_email(subject, body)
    log.info(
        "  → Appointment: %s | %s | addr=%r",
        appt.customer_name, appt.service_date, appt.service_address,
    )

    if not appt.service_address:
        return EmailDraft(
            classification="appointment_notification",
            urgency="medium",
            missing_info=["service address"],
            draft_subject=f"Re: {subject}",
            draft_body=(
                f"Hi {appt.customer_name or 'there'},\n\n"
                "Thank you for booking a Free Property Assessment with Greenguard USA! "
                "Could you reply with your service address so we can prepare for your visit?\n\n"
                "Best regards,\nGreenguard USA Customer Service\nadmin@greenguard-usa.com"
            ),
        )

    prop = lookup_property(appt.service_address, GOOGLE_MAPS_API_KEY)
    templates = _get_templates(gmail_service)

    template_name, lot_size, risk_level = select_template(appt, prop, templates)
    log.info("  → Template: %r  lot=%s  risk=%s", template_name, lot_size, risk_level)

    template_body = templates.get(template_name, "")

    if template_body:
        draft_body = _personalize(template_body, appt)
    else:
        # No matching template — use a plain confirmation
        first = appt.customer_name.split()[0] if appt.customer_name else "there"
        draft_body = (
            f"Hi {first},\n\n"
            "Thank you for booking a Free Property Assessment with Greenguard USA! "
            "We're looking forward to visiting your property"
            + (f" on {appt.service_date}" if appt.service_date else "")
            + ". Our team will be in touch to confirm the details.\n\n"
            "Best regards,\nGreenguard USA Customer Service\nadmin@greenguard-usa.com"
        )

    draft_subject = (
        f"Your Free Property Assessment"
        + (f" – {appt.service_date}" if appt.service_date else "")
    )

    return EmailDraft(
        classification="appointment_notification",
        urgency="medium",
        missing_info=[],
        draft_subject=draft_subject,
        draft_body=draft_body,
    )


def _personalize(template_body: str, appt: AppointmentInfo) -> str:
    """Insert customer first name and date into a template if placeholders exist,
    otherwise prepend a greeting line."""
    first = appt.customer_name.split()[0] if appt.customer_name else "there"
    body = template_body

    # Replace common placeholder patterns
    body = re.sub(r"\[(?:Customer\s*)?Name\]", first, body, flags=re.IGNORECASE)
    body = re.sub(r"\[(?:Appointment\s*)?Date\]", appt.service_date or "your upcoming appointment", body, flags=re.IGNORECASE)
    body = re.sub(r"\[(?:Service\s*)?Address\]", appt.service_address or "", body, flags=re.IGNORECASE)

    # If no greeting line found, prepend one
    if not re.match(r"^(hi|hello|dear)\b", body.strip(), re.IGNORECASE):
        body = f"Hi {first},\n\n{body}"

    return body




# ---------------------------------------------------------------------------
# Biogents shipping email detection + customer notification
# ---------------------------------------------------------------------------

_BIOGENTS_SENDERS = {"biogentspro.com", "biogents.com"}
_SHIPPING_KEYWORDS = {"shipped", "tracking", "on its way", "has been shipped", "order shipped"}


def _is_biogents_shipping_email(sender: str, subject: str) -> bool:
    sender_lower = sender.lower()
    subject_lower = subject.lower()
    from_biogents = any(domain in sender_lower for domain in _BIOGENTS_SENDERS)
    has_shipping_keyword = any(kw in subject_lower for kw in _SHIPPING_KEYWORDS)
    return from_biogents or (has_shipping_keyword and "biogents" in subject_lower)


def _parse_shipping_email(subject: str, body: str) -> dict:
    """Extract order ID, tracking number, carrier, and product name from a Biogents shipping email."""
    import re
    result = {"order_id": "", "tracking_number": "", "carrier": "", "product_name": ""}

    # Order ID
    m = re.search(r"(?:order|#)\s*(?:number|#|id)?\s*:?\s*(\d{4,10})", body + " " + subject, re.IGNORECASE)
    if m:
        result["order_id"] = m.group(1)

    # Tracking number — UPS (1Z...), USPS (9400...), FedEx (numeric 12+), generic
    patterns = [
        (r"\b(1Z[A-Z0-9]{16})\b", "UPS"),
        (r"\b(9[24][0-9]{20})\b", "USPS"),
        (r"\b([0-9]{12,22})\b", "FedEx"),
    ]
    for pattern, carrier in patterns:
        m = re.search(pattern, body)
        if m:
            result["tracking_number"] = m.group(1)
            result["carrier"] = carrier
            break

    # Generic tracking label fallback
    if not result["tracking_number"]:
        m = re.search(r"tracking\s*(?:number|#|id)?\s*:?\s*([A-Z0-9\-]{8,30})", body, re.IGNORECASE)
        if m:
            result["tracking_number"] = m.group(1).strip()

    # Product name — look for "BG-Pro" or "BG-" pattern common to Biogents products
    m = re.search(r"(BG-[A-Za-z0-9\-]+|Biogents\s+\w+(?:\s+\w+)?)", body + " " + subject, re.IGNORECASE)
    if m:
        result["product_name"] = m.group(1)

    return result


def _handle_biogents_shipping_email(gmail_service, email: dict, processed_label_id: str) -> None:
    """Detect which customer this order belongs to, then send them a shipping notification."""
    email_id = email["id"]
    subject  = email["subject"]
    body     = email.get("body", "")

    info = _parse_shipping_email(subject, body)
    log.info(
        "  → Biogents shipping email: order=%s tracking=%s carrier=%s",
        info["order_id"], info["tracking_number"], info["carrier"],
    )

    # Look up the customer from the equipment_orders table
    order_record = None
    if info["order_id"]:
        order_record = db.get_order_by_biogents_id(info["order_id"])

    if not order_record:
        log.warning("  → No matching equipment order for Biogents order #%s — skipping notification", info["order_id"])
        mark_processed(gmail_service, email_id, [processed_label_id])
        return

    # Record the shipment
    if info["tracking_number"]:
        db.mark_order_shipped(info["order_id"], info["tracking_number"], info["carrier"])

    customer_email = order_record.get("customer_email", "")
    customer_name  = order_record.get("customer_name", "")
    sku            = order_record.get("sku", "")
    product_name   = info["product_name"] or sku

    if not customer_email:
        log.warning("  → No customer email for order #%s", info["order_id"])
        mark_processed(gmail_service, email_id, [processed_label_id])
        return

    # Draft customer notification via Gemini
    try:
        notification_body = gemini_client.draft_shipping_notification(
            customer_name=customer_name,
            product_name=product_name,
            tracking_number=info["tracking_number"],
            carrier=info["carrier"],
            order_id=info["order_id"],
        )
    except Exception as exc:
        log.error("  → Gemini draft failed: %s", exc)
        # Fallback: plain notification
        first = customer_name.split()[0] if customer_name else "there"
        notification_body = (
            f"Hi {first},\n\n"
            f"Great news! Your {product_name or 'equipment'} has shipped"
            + (f" via {info['carrier']}" if info["carrier"] else "")
            + (f" with tracking number {info['tracking_number']}" if info["tracking_number"] else "")
            + ".\n\nThank you for your order. Please reach out if you have any questions.\n\n"
            "Best regards,\nGreenguard USA\nadmin@greenguard-usa.com"
        )

    notification_subject = f"Your Greenguard Equipment Has Shipped!"
    if info["tracking_number"]:
        notification_subject += f" Tracking: {info['tracking_number']}"

    try:
        send_email(
            gmail_service,
            to=customer_email,
            subject=notification_subject,
            body=notification_body,
        )
        db.mark_order_notified(info["order_id"])
        log.info("  → Shipping notification sent to %s", customer_email)
    except Exception as exc:
        log.error("  → Failed to send shipping notification to %s: %s", customer_email, exc)

    mark_processed(gmail_service, email_id, [processed_label_id])


# ---------------------------------------------------------------------------
# Core email processing
# ---------------------------------------------------------------------------
def process_email(
    gmail_service,
    calendar_service,
    email: dict,
    processed_label_id: str,
    class_label_ids: dict[str, str],
) -> None:
    email_id = email["id"]
    subject  = email["subject"]
    sender   = email["from"]

    log.info("Processing id=%s subject=%r from=%s", email_id, subject, sender)

    # Biogents shipping notifications — handle before the appointment filter
    if _is_biogents_shipping_email(sender, subject):
        log.info("  → Biogents shipping email detected")
        if not db.is_processed(email_id):
            _handle_biogents_shipping_email(gmail_service, email, processed_label_id)
        else:
            log.info("  → Already processed — skipping")
            mark_processed(gmail_service, email_id, [processed_label_id])
        return

    # Only process property assessment booking notifications — skip everything else
    if not is_appointment_notification(subject):
        log.info("  → Not an appointment notification — skipped")
        mark_processed(gmail_service, email_id, [processed_label_id])
        return

    # Crash-safety: skip if already recorded in SQLite
    if db.is_processed(email_id):
        log.info("  → Already processed (DB record found) — skipping")
        mark_processed(gmail_service, email_id, [processed_label_id])
        return

    draft = _draft_assessment(gmail_service, subject, email["body"])

    log.info(
        "  → classification=%s  urgency=%s  missing=%s",
        draft.classification, draft.urgency, draft.missing_info or "none",
    )

    draft_id = create_draft_reply(
        service=gmail_service,
        original=email,
        draft_subject=draft.draft_subject,
        draft_body=draft.draft_body,
        sender_email=SENDER_EMAIL,
    )
    log.info("  → Draft created: %s", draft_id)

    # Feature 5 — record in SQLite before applying labels (crash-safe ordering)
    db.record_email(email_id, subject, sender, draft.classification, draft.urgency, draft_id)

    # Feature 4 — richer labels: processed + classification sub-label
    label_ids = [processed_label_id]
    cls_label = class_label_ids.get(draft.classification)
    if cls_label:
        label_ids.append(cls_label)
    mark_processed(gmail_service, email_id, label_ids)

    if draft.urgency == "high":
        log.warning("  ⚠  HIGH URGENCY — review this draft promptly")


# ---------------------------------------------------------------------------
# Poll cycle
# ---------------------------------------------------------------------------
def run_once(
    gmail_service,
    calendar_service,
    processed_label_id: str,
    class_label_ids: dict[str, str],
) -> int:
    emails = get_unread_emails(gmail_service, exclude_label_id=processed_label_id)
    if not emails:
        log.info("No new emails.")
        return 0

    log.info("Found %d unread email(s) to process.", len(emails))
    for email in emails:
        try:
            process_email(
                gmail_service, calendar_service, email,
                processed_label_id, class_label_ids,
            )
        except Exception as exc:
            log.error("Failed id=%s: %s", email["id"], exc, exc_info=True)

    return len(emails)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    log.info("Starting Greenguard Gmail Draft Agent")

    # Init SQLite
    db.init_db()

    # Authenticate once — reuse credentials for Gmail + Calendar
    gmail_service, creds = authenticate()
    calendar_service = get_calendar_service(creds)

    # Ensure labels exist (richer labels + processed label)
    processed_label_id = get_or_create_label(gmail_service, PROCESSED_LABEL)
    class_label_ids = {
        cls: get_or_create_label(gmail_service, name)
        for cls, name in _CLASS_LABEL_NAMES.items()
    }
    log.info("Labels ready: %s", list(_CLASS_LABEL_NAMES.values()))

    # Feature 1 — Gmail push via Pub/Sub (real-time, replaces polling loop)
    if PUBSUB_PROJECT_ID and PUBSUB_SUB_ID:
        from push import setup_watch, run_push_loop

        if PUBSUB_TOPIC:
            setup_watch(gmail_service, PUBSUB_TOPIC, db)

        log.info("Push mode: Pub/Sub project=%s sub=%s", PUBSUB_PROJECT_ID, PUBSUB_SUB_ID)
        maybe_send_digest(gmail_service, calendar_service)

        def on_push():
            run_once(gmail_service, calendar_service, processed_label_id, class_label_ids)
            maybe_send_digest(gmail_service, calendar_service)

        run_push_loop(PUBSUB_PROJECT_ID, PUBSUB_SUB_ID, on_push)
        return  # run_push_loop blocks until interrupted

    # Feature 7 — polling fallback (default when Pub/Sub not configured)
    log.info("Poll mode: interval=%ds | timezone=%s", POLL_INTERVAL, CALENDAR_TIMEZONE)
    log_daily_route(calendar_service)

    # Check for --once mode (GitHub Actions)
    once = "--once" in sys.argv or os.getenv("RUN_ONCE") == "1"
    if once:
        try:
            run_once(gmail_service, calendar_service, processed_label_id, class_label_ids)
            maybe_send_digest(gmail_service, calendar_service)
            log.info("--once mode: completed single poll cycle")
        except Exception as exc:
            log.error("Poll cycle error: %s", exc, exc_info=True)
        return

    # Standard polling loop
    while True:
        try:
            run_once(gmail_service, calendar_service, processed_label_id, class_label_ids)
            # Feature 8 — daily digest check on every poll cycle
            maybe_send_digest(gmail_service, calendar_service)
        except Exception as exc:
            log.error("Poll cycle error: %s", exc, exc_info=True)

        log.info("Sleeping %ds until next poll…", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
