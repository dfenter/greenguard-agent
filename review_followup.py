"""
review_followup.py — 7-day follow-up asking customers to leave a Google review.

Finds appointments that ended 7–8 days ago and sends a gentle second-touch
email (and SMS if phone available) to customers who haven't yet received one.
Idempotent — tracks sent messages in review_followup_log.json.

Run daily at 9 AM CT via launchd (com.greenguard.reviewfollowup.plist).
"""

import base64
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv
from googleapiclient.discovery import build

import sms_client

load_dotenv()

_DIR     = os.path.dirname(os.path.abspath(__file__))
TZ       = timezone(timedelta(hours=-5))   # America/Chicago CDT
LOG_FILE = Path(_DIR) / "review_followup_log.json"

REVIEW_URL = "https://g.page/r/CW33u4YWYh17EAE/review"


# ── Extraction helpers ────────────────────────────────────────────────────────

def _extract_email(desc: str) -> str | None:
    m = re.search(r"Email:\s*(\S+@\S+)", desc)
    return m.group(1).strip().lower() if m else None


def _extract_phone(desc: str) -> str | None:
    m = re.search(r"(?:Phone|Mobile|Cell)[\s:]*([+\d\s\(\)\-\.]{7,20})", desc, re.IGNORECASE)
    return m.group(1).strip() if m else None


# ── Idempotency log ───────────────────────────────────────────────────────────

def _load_log() -> dict:
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_log(log: dict):
    cutoff = (datetime.now(TZ) - timedelta(days=60)).date().isoformat()
    pruned = {k: v for k, v in log.items() if v >= cutoff}
    LOG_FILE.write_text(json.dumps(pruned, indent=2))


# ── Email builder ─────────────────────────────────────────────────────────────

def _build_email(name: str) -> tuple[str, str]:
    first = name.split()[0] if name else "there"
    subject = "How did your GreenGuard service go?"

    html = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#0d1a10;font-family:'Helvetica Neue',Arial,sans-serif">
<div style="max-width:520px;margin:0 auto;padding:24px 16px">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:linear-gradient(135deg,#0d1a10 0%,#1a2e1f 100%);border:1px solid rgba(122,171,130,0.25);border-radius:12px;margin-bottom:12px">
    <tr>
      <td style="padding:28px 28px 20px">
        <div style="color:#c9a84c;font-size:10px;font-weight:800;letter-spacing:0.15em;text-transform:uppercase;margin-bottom:10px">GreenGuard USA</div>
        <div style="color:#ffffff;font-size:22px;font-weight:900;margin-bottom:12px">Hi {first} — quick check-in</div>
        <div style="color:rgba(212,230,202,0.8);font-size:15px;line-height:1.7">
          <p style="margin:0 0 14px">It's been about a week since your service. We hope the traps are already making a difference in your yard.</p>
          <p style="margin:0 0 14px">If you've had a good experience, a Google review makes a real difference for a small business like ours — it helps other homeowners find a safer, pesticide-free option.</p>
          <p style="margin:0 0 20px">Takes about 30 seconds:</p>
        </div>
        <div style="text-align:left;margin-bottom:20px">
          <a href="{REVIEW_URL}" style="display:inline-block;background:#c9a84c;color:#0a1a0d;font-weight:900;font-size:13px;padding:13px 28px;border-radius:6px;text-decoration:none;letter-spacing:0.06em;text-transform:uppercase">Leave a Review</a>
        </div>
        <div style="color:rgba(212,230,202,0.55);font-size:13px;line-height:1.6">
          <p style="margin:0">Any issues or questions? Just reply — we'll take care of it.</p>
          <p style="margin:12px 0 0">Dan Fenter<br>GreenGuard USA · 512-560-4129</p>
        </div>
      </td>
    </tr>
  </table>
  <div style="text-align:center;color:rgba(122,171,130,0.2);font-size:10px;letter-spacing:0.08em;text-transform:uppercase">GreenGuard USA · 1519 Parkway, Austin TX 78703</div>
</div>
</body>
</html>"""

    return subject, html


# ── Send ──────────────────────────────────────────────────────────────────────

def _send(gmail_service, to: str, subject: str, html: str):
    msg = MIMEMultipart("alternative")
    msg["To"]      = to
    msg["From"]    = "admin@greenguard-usa.com"
    msg["Subject"] = subject
    msg.attach(MIMEText(html, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail_service.users().messages().send(userId="me", body={"raw": raw}).execute()


# ── Main ──────────────────────────────────────────────────────────────────────

def run(target_date: datetime | None = None):
    from gmail_client import authenticate

    now = datetime.now(TZ)
    if target_date is None:
        # Appointments that ended ~7 days ago (±12 hour window around the 7-day mark)
        day_start = now - timedelta(days=7, hours=12)
        day_end   = now - timedelta(days=6, hours=12)
        label     = f"7-day window ending {now.strftime('%-I:%M %p')}"
    else:
        day_start = target_date.replace(hour=0,  minute=0,  second=0,  microsecond=0)
        day_end   = target_date.replace(hour=23, minute=59, second=59, microsecond=0)
        label     = target_date.strftime("%A %b %-d")

    print(f"\nReview Follow-up — targeting appointments from {label}")

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
    print(f"{len(events)} appointment(s) in window\n")

    log  = _load_log()
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

        log_key = f"followup_{ev_id}"
        if log_key in log:
            print(f"  DUP   {name:<28} already sent {log[log_key]}")
            skipped_dup += 1
            continue

        subject, html = _build_email(name)
        _send(gmail_service, email, subject, html)
        log[log_key] = now.date().isoformat()

        sms_sent = False
        if phone:
            first = name.split()[0] if name else "there"
            sms_body = (
                f"Hi {first}, GreenGuard here — hope the traps are working well! "
                f"A quick Google review helps others find us: {REVIEW_URL}"
            )
            sms_sent = sms_client.send_sms(phone, sms_body)

        sms_note = " + SMS" if sms_sent else ""
        print(f"  ✓     {name:<28} → {email}{sms_note}")
        sent += 1

    _save_log(log)
    print(f"\n  Sent: {sent}  |  Already sent: {skipped_dup}  |  No email: {skipped_no_email}\n")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        d = datetime.strptime(sys.argv[1], "%Y-%m-%d").replace(tzinfo=TZ)
        run(target_date=d)
    else:
        run()
