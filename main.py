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
import time
import logging
from dotenv import load_dotenv

from gmail_client import (
    authenticate,
    get_or_create_label,
    get_unread_emails,
    get_thread_context,
    create_draft_reply,
    mark_processed,
    send_email,
)
from calendar_client import get_calendar_service, get_available_slots, get_route_distances
from agent import analyze_and_draft, looks_like_scheduling
from spam_filter import is_spam
import db
from digest import should_send_digest, build_digest

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

    # Item 1 — spam filter: skip auto-replies, OOO, delivery notices before touching Claude
    if is_spam(sender, subject, email["body"]):
        log.info("  → Spam/auto-reply — skipped")
        mark_processed(gmail_service, email_id, [processed_label_id])
        return

    # Item 5 — crash-safety: skip if already recorded in SQLite
    if db.is_processed(email_id):
        log.info("  → Already processed (DB record found) — skipping")
        mark_processed(gmail_service, email_id, [processed_label_id])
        return

    # Feature 6 — thread context: fetch prior messages in this thread
    thread_history = get_thread_context(
        gmail_service, email["thread_id"], email_id, limit=2
    )
    if thread_history:
        log.info("  → Thread context: %d prior message(s)", len(thread_history))

    # Calendar slots only when email looks scheduling-related
    slots: list[str] = []
    if calendar_service and looks_like_scheduling(subject, email["body"]):
        try:
            slots = _get_slots(calendar_service)
        except Exception as exc:
            log.warning("  → Could not fetch calendar slots: %s", exc)

    draft = analyze_and_draft(
        email_from=sender,
        email_subject=subject,
        email_body=email["body"],
        available_slots=slots or None,
        thread_history=thread_history or None,
    )

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
