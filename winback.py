"""
winback.py — Re-engage lapsed recurring customers.

Finds customers whose last appointment was 45–120 days ago and who have
no future appointment scheduled. Uses Claude to draft a personalized
re-engagement email and puts it in Gmail Drafts for human review.

Safe to run weekly — tracks drafts created in winback_log.json to avoid
duplicate outreach to the same customer.

Usage:
    python3 winback.py              # check last 45–120 days (default)
    python3 winback.py --dry-run    # print candidates, create no drafts
    python3 winback.py --days 60    # change the lapsed threshold
"""

import argparse
import base64
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import anthropic
from dotenv import load_dotenv
from googleapiclient.discovery import build

from cloud_state import is_winback_sent, mark_winback_sent

load_dotenv()

_DIR       = os.path.dirname(os.path.abspath(__file__))
TZ         = timezone(timedelta(hours=-5))   # America/Chicago CDT
REVIEW_URL = "https://g.page/r/CW33u4YWYh17EAE/review"

_client = anthropic.Anthropic()


# ── Extraction helpers ────────────────────────────────────────────────────────

def _extract_email(desc: str) -> str | None:
    m = re.search(r"Email:\s*(\S+@\S+)", desc)
    return m.group(1).strip().lower() if m else None


def _extract_address(event: dict) -> str | None:
    desc = event.get("description", "") or ""
    m = re.search(
        r"Address\n={4,}\nPlease enter the address for the service to be performed::\s*(.+?)(?:\n\n|\Z)",
        desc, re.DOTALL,
    )
    if m:
        addr = m.group(1).strip().splitlines()[0].strip()
        if addr and re.search(r"\d", addr) and "http" not in addr:
            return addr
    loc = (event.get("location") or "").strip()
    if loc and re.search(r"\d", loc) and "http" not in loc:
        return loc
    return None


def _extract_service_type(event: dict) -> str:
    summary = event.get("summary", "")
    parts   = summary.split(":", 1)
    return parts[1].strip() if len(parts) > 1 else summary.strip()




# ── Calendar scan ─────────────────────────────────────────────────────────────

def _scan_calendar(cal, lapsed_days: int) -> list[dict]:
    """
    Return list of lapsed-customer dicts:
      {name, email, last_date, last_service, address, appointment_count}

    Logic:
      - Look back 'lapsed_days + 60' days to find all appointments
      - Look forward 90 days for upcoming appointments
      - Keep customers whose last past appointment is >= lapsed_days ago
        AND who have no upcoming appointment
    """
    now      = datetime.now(TZ)
    look_back_start = (now - timedelta(days=lapsed_days + 60)).isoformat()
    look_back_end   = now.isoformat()
    look_fwd_end    = (now + timedelta(days=90)).isoformat()

    # Past appointments
    past_resp = cal.events().list(
        calendarId="primary",
        timeMin=look_back_start,
        timeMax=look_back_end,
        singleEvents=True,
        orderBy="startTime",
        fields="items(id,summary,start,description,location)",
    ).execute()
    past_events = [e for e in past_resp.get("items", []) if e["start"].get("dateTime")]

    # Future appointments
    fwd_resp = cal.events().list(
        calendarId="primary",
        timeMin=now.isoformat(),
        timeMax=look_fwd_end,
        singleEvents=True,
        fields="items(id,description,start)",
    ).execute()
    fwd_emails = set()
    for e in fwd_resp.get("items", []):
        if e.get("start", {}).get("dateTime"):
            em = _extract_email(e.get("description", "") or "")
            if em:
                fwd_emails.add(em)

    # Group past events by customer email
    by_email: dict[str, list] = {}
    for ev in past_events:
        desc  = ev.get("description", "") or ""
        email = _extract_email(desc)
        if not email:
            continue
        by_email.setdefault(email, []).append(ev)

    cutoff_dt = now - timedelta(days=lapsed_days)
    candidates = []

    for email, events in by_email.items():
        if email in fwd_emails:
            continue  # already has an upcoming appointment

        # Sort descending by start time
        events.sort(key=lambda e: e["start"]["dateTime"], reverse=True)
        last_ev = events[0]
        last_dt = datetime.fromisoformat(last_ev["start"]["dateTime"]).astimezone(TZ)

        if last_dt >= cutoff_dt:
            continue  # too recent

        name         = last_ev.get("summary", "").split(":")[0].strip()
        service_type = _extract_service_type(last_ev)
        address      = _extract_address(last_ev)

        candidates.append({
            "name":              name,
            "email":             email,
            "last_date":         last_dt.strftime("%B %-d, %Y"),
            "last_service":      service_type,
            "address":           address or "",
            "appointment_count": len(events),
        })

    # Sort by last_date ascending (longest-lapsed first)
    candidates.sort(key=lambda c: c["last_date"])
    return candidates


# ── Claude draft writer ───────────────────────────────────────────────────────

_WINBACK_SYSTEM = """You write warm, genuine win-back emails for GreenGuard USA, a CO2 mosquito
control company in Austin, TX. The email goes out when a customer hasn't
scheduled a follow-up appointment in 45+ days.

TONE: Personal, low-pressure, helpful. Not sales-y. More like a note from a
neighbor. First-name basis. Keep it under 130 words body text.

STRUCTURE:
1. Open with their first name + brief personalized observation about the
   season or their property situation
2. One sentence noting it's been a while and we want to make sure they're
   still protected
3. One sentence on the current mosquito risk in Austin (seasonal context)
4. Clear CTA: one link to book (https://cal.com/greenguard-usa) and
   mention we're happy to check on the existing traps too
5. Soft close — reply anytime with questions
6. Sign: "Dan Fenter\\nGreenGuard USA\\n512-560-4129"

DO NOT: mention the gap in months/days explicitly, mention pricing, sound
automated, start with "I hope this email finds you well", use "valued customer".

Output ONLY the email body text (no subject line, no HTML)."""


def _draft_winback_body(customer: dict) -> str:
    season = _current_season()
    prompt = (
        f"Customer: {customer['name']}\n"
        f"Last service: {customer['last_date']} ({customer['last_service']})\n"
        f"Address: {customer['address'] or 'Austin, TX'}\n"
        f"Total past appointments: {customer['appointment_count']}\n"
        f"Current season: {season}\n\n"
        "Write the win-back email body."
    )
    resp = _client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=350,
        system=_WINBACK_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def _current_season() -> str:
    month = datetime.now(TZ).month
    if month in (3, 4, 5):
        return "Spring — peak mosquito breeding season starting"
    if month in (6, 7, 8):
        return "Summer — peak mosquito season in Austin"
    if month in (9, 10):
        return "Early fall — second surge of mosquito activity"
    return "Off-season — good time to service and maintain traps"


# ── Gmail draft creator ───────────────────────────────────────────────────────

def _create_draft(gmail_service, customer: dict, body: str) -> str:
    first   = customer["name"].split()[0] if customer["name"] else "there"
    subject = f"Checking in — GreenGuard USA ({first})"

    html_body = body.replace("\n\n", "</p><p>").replace("\n", "<br>")
    html = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#ffffff;font-family:Arial,sans-serif">
<div style="max-width:520px;margin:0 auto;padding:32px 24px;color:#222222;font-size:15px;line-height:1.7">
  <p>{html_body}</p>
</div>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["To"]      = customer["email"]
    msg["From"]    = "admin@greenguard-usa.com"
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    msg.attach(MIMEText(html, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    draft = gmail_service.users().drafts().create(
        userId="me", body={"message": {"raw": raw}}
    ).execute()
    return draft["id"]


# ── Main ──────────────────────────────────────────────────────────────────────

def run(lapsed_days: int = 45, dry_run: bool = False):
    from gmail_client import authenticate

    print(f"\nWin-Back Campaign — customers with no appointment in {lapsed_days}+ days")
    print(f"{'DRY RUN — no drafts will be created' if dry_run else 'Live run — drafts go to Gmail'}\n")

    _, creds = authenticate()
    gmail_service, _ = authenticate()
    cal = build("calendar", "v3", credentials=creds)

    candidates = _scan_calendar(cal, lapsed_days)
    print(f"Found {len(candidates)} lapsed customer(s)\n")

    if not candidates:
        print("  Nothing to do.\n")
        return

    drafted = skipped = 0

    for c in candidates:
        if is_winback_sent(c['email']):
            print(f"  DUP   {c['name']:<28} already drafted")
            skipped += 1
            continue

        print(f"  {c['name']:<28} last: {c['last_date']} ({c['last_service']})")

        if dry_run:
            drafted += 1
            continue

        body = _draft_winback_body(c)
        draft_id = _create_draft(gmail_service, c, body)
        mark_winback_sent(c['email'])
        print(f"    → Draft created: {draft_id}")
        drafted += 1
        time.sleep(0.5)  # avoid rate-limiting Claude

    action = "candidates found" if dry_run else "draft(s) created"
    print(f"\n  {drafted} {action}  |  {skipped} skipped (already drafted)\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",  action="store_true", help="List candidates, don't create drafts")
    parser.add_argument("--days",     type=int, default=45, help="Days since last appointment to consider lapsed (default: 45)")
    args = parser.parse_args()
    run(lapsed_days=args.days, dry_run=args.dry_run)
