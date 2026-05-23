"""
appointment_reminder.py — Send 48-hour appointment reminders to customers.

Fetches all appointments from Google Calendar for the date 2 days from now
and emails each customer a branded reminder with their appointment details.

Run daily at 8 AM CT via launchd (com.greenguard.reminder.plist).
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
from cloud_state import is_reminded, mark_reminded

load_dotenv()

_DIR = os.path.dirname(os.path.abspath(__file__))
TZ   = timezone(timedelta(hours=-5))   # America/Chicago CDT


# ── Reused extraction helpers (mirrors route_optimizer.py) ───────────────────

def _extract_email(desc: str) -> str | None:
    m = re.search(r"Email:\s*(\S+@\S+)", desc)
    return m.group(1).strip().lower() if m else None


def _extract_phone(desc: str) -> str | None:
    m = re.search(r"(?:Phone|Mobile|Cell)[\s:]*([+\d\s\(\)\-\.]{7,20})", desc, re.IGNORECASE)
    return m.group(1).strip() if m else None


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




# ── Email builder ─────────────────────────────────────────────────────────────

def _build_email(name: str, service_type: str, dt: datetime, address: str | None) -> tuple[str, str]:
    day_str  = dt.strftime("%A, %B %-d")
    time_str = dt.strftime("%-I:%M %p")

    subject = f"Your GreenGuard appointment is in 2 days — {day_str}"

    addr_row = f"""
    <tr>
      <td style="padding:0 24px 20px 24px">
        <table cellpadding="0" cellspacing="0">
          <tr>
            <td style="color:#c9a84c;font-size:11px;font-weight:800;letter-spacing:0.1em;text-transform:uppercase;padding-right:10px;vertical-align:top;padding-top:2px">Location</td>
            <td style="color:rgba(212,230,202,0.75);font-size:14px">{address}</td>
          </tr>
        </table>
      </td>
    </tr>""" if address else ""

    html = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#0d1a10;font-family:'Helvetica Neue',Arial,sans-serif">
<div style="max-width:520px;margin:0 auto;padding:24px 16px">

  <!-- Header card -->
  <table width="100%" cellpadding="0" cellspacing="0" style="background:linear-gradient(135deg,#0d1a10 0%,#1a2e1f 100%);border:1px solid rgba(122,171,130,0.25);border-radius:12px;margin-bottom:12px">
    <tr>
      <td style="padding:22px 24px 10px 24px">
        <div style="color:#c9a84c;font-size:10px;font-weight:800;letter-spacing:0.15em;text-transform:uppercase">GreenGuard USA &nbsp;&middot;&nbsp; Appointment Reminder</div>
        <div style="color:#ffffff;font-size:26px;font-weight:900;margin-top:8px;letter-spacing:-0.02em">See you {day_str}!</div>
        <div style="color:rgba(212,230,202,0.6);font-size:14px;margin-top:4px">This is a reminder for your upcoming service.</div>
      </td>
    </tr>

    <!-- Appointment details -->
    <tr>
      <td style="padding:16px 24px">
        <table width="100%" cellpadding="0" cellspacing="0" style="background:rgba(0,0,0,0.2);border:1px solid rgba(122,171,130,0.2);border-radius:8px">
          <tr>
            <td style="padding:16px 20px;border-bottom:1px solid rgba(122,171,130,0.15)">
              <div style="color:#c9a84c;font-size:10px;font-weight:800;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:4px">Date &amp; Time</div>
              <div style="color:#ffffff;font-size:18px;font-weight:800">{day_str} &nbsp;at&nbsp; {time_str}</div>
            </td>
          </tr>
          <tr>
            <td style="padding:16px 20px">
              <div style="color:#c9a84c;font-size:10px;font-weight:800;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:6px">Service</div>
              <span style="background:rgba(201,168,76,0.12);border:1px solid rgba(201,168,76,0.35);border-radius:100px;padding:5px 14px;color:#c9a84c;font-size:13px;font-weight:800;display:inline-block">{service_type}</span>
            </td>
          </tr>
        </table>
      </td>
    </tr>

    {addr_row}
  </table>

  <!-- Footer note -->
  <table width="100%" cellpadding="0" cellspacing="0">
    <tr>
      <td style="padding:14px 4px;text-align:center;color:rgba(122,171,130,0.5);font-size:12px;line-height:1.6">
        Questions? Reply to this email or call us.<br>
        <span style="color:rgba(122,171,130,0.25);font-size:11px;letter-spacing:0.05em">GREENGUARD USA &nbsp;&middot;&nbsp; 1519 PARKWAY, AUSTIN TX 78703</span>
      </td>
    </tr>
  </table>

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

    if target_date is None:
        today       = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        target_date = today + timedelta(days=2)

    day_start = target_date.replace(hour=0,  minute=0,  second=0,  microsecond=0)
    day_end   = target_date.replace(hour=23, minute=59, second=59, microsecond=0)

    print(f"\nAppointment Reminders — sending for {target_date.strftime('%A %b %-d')}")

    gmail_service, creds = authenticate()
    cal = build("calendar", "v3", credentials=creds)

    resp = cal.events().list(
        calendarId="primary",
        timeMin=day_start.isoformat(),
        timeMax=day_end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        fields="items(id,summary,start,description,location)",
    ).execute()

    events = [e for e in resp.get("items", []) if e["start"].get("dateTime")]
    print(f"{len(events)} appointment(s) found\n")

    sent = skipped_dup = skipped_no_email = 0

    for ev in events:
        ev_id   = ev["id"]
        summary = ev.get("summary", "")
        desc    = ev.get("description", "") or ""
        parts   = summary.split(":", 1)
        name    = parts[0].strip()
        service = parts[1].strip() if len(parts) > 1 else summary.strip()

        email   = _extract_email(desc)
        phone   = _extract_phone(desc)
        address = _extract_address(ev)
        dt      = datetime.fromisoformat(ev["start"]["dateTime"]).astimezone(TZ)

        if not email:
            print(f"  SKIP  {name:<28} no email in event")
            skipped_no_email += 1
            continue

        if is_reminded(ev_id):
            print(f"  DUP   {name:<28} already sent")
            skipped_dup += 1
            continue

        subject, html = _build_email(name, service, dt, address)
        _send(gmail_service, email, subject, html)
        mark_reminded(ev_id)

        sms_sent = False
        if phone:
            first = name.split()[0] if name else "there"
            day_str  = dt.strftime("%A, %B %-d")
            time_str = dt.strftime("%-I:%M %p")
            sms_body = (
                f"Hi {first}, this is GreenGuard USA — reminder your appointment "
                f"is {day_str} at {time_str}. Questions? Reply or call 512-560-4129."
            )
            sms_sent = sms_client.send_sms(phone, sms_body)

        sms_note = " + SMS" if sms_sent else ""
        print(f"  ✓     {name:<28} → {email}{sms_note}")
        sent += 1

    print(f"\n  Sent: {sent}  |  Already sent: {skipped_dup}  |  No email: {skipped_no_email}\n")


if __name__ == "__main__":
    # Optional: pass a date as YYYY-MM-DD to test a specific day
    if len(sys.argv) > 1:
        d = datetime.strptime(sys.argv[1], "%Y-%m-%d").replace(tzinfo=TZ)
        run(target_date=d)
    else:
        run()
