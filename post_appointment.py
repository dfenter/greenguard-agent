"""
post_appointment.py — Send a thank-you email after each completed appointment.

Fetches all appointments from Google Calendar for yesterday, and sends
a branded thank-you + review request to each customer.

Run daily at 8 AM CT via launchd (com.greenguard.postappointment.plist).
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

load_dotenv()

_DIR     = os.path.dirname(os.path.abspath(__file__))
TZ       = timezone(timedelta(hours=-5))   # America/Chicago CDT
LOG_FILE = Path(_DIR) / "post_appointment_log.json"

REVIEW_URL = "https://g.page/r/CW33u4YWYh17EAE/review"


# ── Field extraction ──────────────────────────────────────────────────────────

def _extract_email(desc: str) -> str | None:
    m = re.search(r"Email:\s*(\S+@\S+)", desc)
    return m.group(1).strip().lower() if m else None


# ── Idempotency log ───────────────────────────────────────────────────────────

def _load_log() -> dict:
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_log(log: dict):
    cutoff = (datetime.now(TZ) - timedelta(days=30)).date().isoformat()
    pruned = {k: v for k, v in log.items() if v >= cutoff}
    LOG_FILE.write_text(json.dumps(pruned, indent=2))


# ── Email builder ─────────────────────────────────────────────────────────────

def _build_email(name: str) -> tuple[str, str]:
    first = name.split()[0] if name else "there"
    subject = "Thank you for choosing GreenGuard USA"

    html = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#0d1a10;font-family:'Helvetica Neue',Arial,sans-serif">
<div style="max-width:520px;margin:0 auto;padding:24px 16px">

  <!-- Header -->
  <table width="100%" cellpadding="0" cellspacing="0" style="background:linear-gradient(135deg,#0d1a10 0%,#1a2e1f 100%);border:1px solid rgba(122,171,130,0.25);border-radius:12px;margin-bottom:12px">
    <tr>
      <td style="padding:22px 24px 20px 24px">
        <div style="color:#c9a84c;font-size:10px;font-weight:800;letter-spacing:0.15em;text-transform:uppercase;margin-bottom:8px">GreenGuard USA &nbsp;&middot;&nbsp; Thank You</div>
        <div style="color:#ffffff;font-size:22px;font-weight:900;letter-spacing:-0.02em;line-height:1.2">Thank you for choosing<br>GreenGuard USA.</div>
      </td>
    </tr>
  </table>

  <!-- Body -->
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#111c13;border:1px solid rgba(122,171,130,0.18);border-radius:12px;margin-bottom:12px">
    <tr>
      <td style="padding:24px 24px 8px 24px;color:rgba(212,230,202,0.85);font-size:15px;line-height:1.75">
        <p style="margin:0 0 16px 0">You're taking a smarter, science-based approach to mosquito control that protects your family, your yard, and the environment.</p>
        <p style="margin:0 0 16px 0">If you have any questions or concerns, just reply to this email and we'll take care of it.</p>
        <p style="margin:0 0 20px 0">If you've had a good experience so far, we'd really appreciate a quick review — it helps more homeowners discover a better way to control mosquitoes without pesticides.</p>
      </td>
    </tr>
    <tr>
      <td style="padding:4px 24px 24px 24px;text-align:center">
        <a href="{REVIEW_URL}" style="display:inline-block;background:#c9a84c;color:#0d1a10;font-weight:900;font-size:13px;padding:14px 32px;border-radius:5px;text-decoration:none;letter-spacing:0.06em;text-transform:uppercase">Leave a Google Review</a>
      </td>
    </tr>
  </table>

  <!-- Signature -->
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#111c13;border:1px solid rgba(122,171,130,0.18);border-radius:12px">
    <tr>
      <td style="padding:20px 24px">
        <div style="color:rgba(212,230,202,0.85);font-size:14px;line-height:1.7">
          We appreciate you trusting GreenGuard USA.<br><br>
          <strong style="color:#ffffff">Dan Fenter</strong><br>
          <span style="color:#7aab82">GreenGuard USA</span><br>
          <span style="color:rgba(212,230,202,0.55)">512-560-4129</span><br>
          <span style="color:#c9a84c;font-size:12px;font-style:italic">Smart. Safe. Effective Mosquito Control.</span>
        </div>
      </td>
    </tr>
  </table>

  <!-- Footer -->
  <div style="margin-top:18px;text-align:center;color:rgba(122,171,130,0.25);font-size:11px;letter-spacing:0.05em">
    GREENGUARD USA &nbsp;&middot;&nbsp; 1519 PARKWAY, AUSTIN TX 78703
  </div>

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

    log  = _load_log()
    sent = skipped_dup = skipped_no_email = 0

    for ev in events:
        ev_id = ev["id"]
        desc  = ev.get("description", "") or ""
        name  = ev.get("summary", "").split(":")[0].strip()
        email = _extract_email(desc)

        if not email:
            print(f"  SKIP  {name:<28} no email in event")
            skipped_no_email += 1
            continue

        if ev_id in log:
            print(f"  DUP   {name:<28} already sent {log[ev_id]}")
            skipped_dup += 1
            continue

        subject, html = _build_email(name)
        _send(gmail_service, email, subject, html)
        log[ev_id] = datetime.now(TZ).date().isoformat()
        print(f"  ✓     {name:<28} → {email}")
        sent += 1

    _save_log(log)
    print(f"\n  Sent: {sent}  |  Already sent: {skipped_dup}  |  No email: {skipped_no_email}\n")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        d = datetime.strptime(sys.argv[1], "%Y-%m-%d").replace(tzinfo=TZ)
        run(target_date=d)
    else:
        run()
