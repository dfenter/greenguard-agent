"""
post_appointment.py — Send a thank-you email after each completed appointment.

Fetches all appointments from Google Calendar for yesterday, and sends
a branded thank-you + review request to each customer.

Run daily at 8 AM CT via launchd (com.greenguard.postappointment.plist).
"""

import base64
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv
from googleapiclient.discovery import build

import sms_client
from cloud_state import is_post_sent, mark_post_sent

load_dotenv()

_DIR = os.path.dirname(os.path.abspath(__file__))
TZ   = timezone(timedelta(hours=-5))   # America/Chicago CDT

REVIEW_URL = "https://g.page/r/CW33u4YWYh17EAE/review"


# ── Field extraction ──────────────────────────────────────────────────────────

def _extract_email(desc: str) -> str | None:
    m = re.search(r"Email:\s*(\S+@\S+)", desc)
    return m.group(1).strip().lower() if m else None


def _extract_phone(desc: str) -> str | None:
    m = re.search(r"(?:Phone|Mobile|Cell)[\s:]*([+\d\s\(\)\-\.]{7,20})", desc, re.IGNORECASE)
    return m.group(1).strip() if m else None




# ── Email builder ─────────────────────────────────────────────────────────────

def _build_email(name: str) -> tuple[str, str]:
    first = name.split()[0] if name else "there"
    subject = "Thank you for choosing GreenGuard USA"

    plain = f"""Hi {first},

Thank you for choosing GreenGuard USA, and for choosing not to use pesticides.

You're taking a smarter, science-based approach to mosquito control that protects your family, your yard, and the environment.

If you have any questions or concerns, just reply to this email and we'll take care of it.

If you've had a good experience so far, we'd really appreciate a quick review:

{REVIEW_URL}

It helps more homeowners discover a better way to control mosquitoes without pesticides.

We appreciate you trusting GreenGuard USA.

Dan Fenter
GreenGuard USA
512-560-4129
Smart. Safe. Effective."""

    html = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#ffffff;font-family:Arial,sans-serif">
<div style="max-width:520px;margin:0 auto;padding:32px 24px;color:#222222;font-size:15px;line-height:1.7">
  <p>Hi {first},</p>
  <p>Thank you for choosing GreenGuard USA, and for choosing not to use pesticides.</p>
  <p>You're taking a smarter, science-based approach to mosquito control that protects your family, your yard, and the environment.</p>
  <p>If you have any questions or concerns, just reply to this email and we'll take care of it.</p>
  <p>If you've had a good experience so far, we'd really appreciate a quick review:</p>
  <p><a href="{REVIEW_URL}">{REVIEW_URL}</a></p>
  <p>It helps more homeowners discover a better way to control mosquitoes without pesticides.</p>
  <p>We appreciate you trusting GreenGuard USA.</p>
  <p>Dan Fenter<br>
  GreenGuard USA<br>
  512-560-4129<br>
  <em>Smart. Safe. Effective.</em></p>
</div>
</body>
</html>"""

    return subject, html, plain


# ── Send ──────────────────────────────────────────────────────────────────────

def _send(gmail_service, to: str, subject: str, html: str, plain: str):
    msg = MIMEMultipart("alternative")
    msg["To"]      = to
    msg["From"]    = "admin@greenguard-usa.com"
    msg["Subject"] = subject
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail_service.users().messages().send(userId="me", body={"raw": raw}).execute()


# ── Main ──────────────────────────────────────────────────────────────────────

def run(target_date: datetime | None = None):
    from gmail_client import authenticate

    # Default: appointments that ended 24 hours ago (±30 min window)
    # Script runs hourly so each appointment gets caught once
    if target_date is None:
        now       = datetime.now(TZ)
        day_start = now - timedelta(hours=24, minutes=30)
        day_end   = now - timedelta(hours=23, minutes=30)
        label     = f"24h window ending {now.strftime('%-I:%M %p')}"
    else:
        day_start = target_date.replace(hour=0,  minute=0,  second=0,  microsecond=0)
        day_end   = target_date.replace(hour=23, minute=59, second=59, microsecond=0)
        label     = target_date.strftime("%A %b %-d")

    print(f"\nPost-appointment emails — {label}")

    gmail_service, creds = authenticate()
    cal = build("calendar", "v3", credentials=creds)

    resp = cal.events().list(
        calendarId="primary",
        timeMin=day_start.isoformat(),
        timeMax=day_end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        fields="items(id,summary,start,description)",
    ).execute()

    events = [e for e in resp.get("items", []) if e["start"].get("dateTime")]
    print(f"{len(events)} appointment(s) found\n")

    sent = skipped_dup = skipped_no_email = 0

    for ev in events:
        ev_id = ev["id"]
        desc  = ev.get("description", "") or ""
        name  = ev.get("summary", "").split(":")[0].strip()
        email = _extract_email(desc)
        phone = _extract_phone(desc)

        if not email:
            print(f"  SKIP  {name:<28} no email in event")
            skipped_no_email += 1
            continue

        if is_post_sent(ev_id):
            print(f"  DUP   {name:<28} already sent")
            skipped_dup += 1
            continue

        subject, html, plain = _build_email(name)
        _send(gmail_service, email, subject, html, plain)
        mark_post_sent(ev_id)

        sms_sent = False
        if phone:
            first = name.split()[0] if name else "there"
            sms_body = (
                f"Hi {first}, thanks for choosing GreenGuard USA! "
                f"If you have a moment, a quick Google review helps others find us: "
                f"{REVIEW_URL}"
            )
            sms_sent = sms_client.send_sms(phone, sms_body)

        sms_note = " + SMS" if sms_sent else ""
        print(f"  ✓     {name:<28} → {email}{sms_note}")
        sent += 1
    print(f"\n  Sent: {sent}  |  Already sent: {skipped_dup}  |  No email: {skipped_no_email}\n")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        d = datetime.strptime(sys.argv[1], "%Y-%m-%d").replace(tzinfo=TZ)
        run(target_date=d)
    else:
        run()
