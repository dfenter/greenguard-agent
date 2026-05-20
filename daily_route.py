"""
daily_route.py — Email today's optimized route to admin@greenguard-usa.com

Run manually or via launchd at 7:30am CT Mon-Sat.
Skips silently if no appointments today.

Usage:
    python3 daily_route.py           # today
    python3 daily_route.py 2026-05-26  # specific date
"""

import base64
import os
import sys
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from googleapiclient.discovery import build

import route_optimizer as ro
from gmail_client import authenticate

load_dotenv()

TZ         = ZoneInfo(os.getenv("CALENDAR_TIMEZONE", "America/Chicago"))
ROUTE_TO   = os.getenv("ROUTE_EMAIL", "admin@greenguard-usa.com")


def _build_email(day_label: str, stops: list[dict], day_d: float, day_m: int,
                 day_tanks: int, maps_link: str) -> tuple[str, str]:
    """Return (subject, html_body)."""
    subject = f"GreenGuard Routes — {day_label}  |  {len(stops)} stops  |  {day_d} mi"
    if day_tanks:
        subject += f"  |  {day_tanks} tanks"

    rows = ""
    for i, s in enumerate(stops, 1):
        tanks_badge = f' <span style="color:#c9a84c;font-weight:700">[{s["tanks"]}T]</span>' if s.get("tanks") else ""
        note = f'<br><span style="color:#888;font-size:12px">Note: {s["notes"]}</span>' if s.get("notes") else ""
        rows += f"""
        <tr>
          <td style="padding:10px 8px;border-bottom:1px solid #2d4a32;color:#c9a84c;font-weight:700;width:24px">{i}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #2d4a32">
            <strong style="color:#fff">{s["name"]}</strong>{tanks_badge}
            <span style="color:#7aab82;margin-left:8px">{s["sched"]}</span>
            <br><span style="color:#a8edc0;font-size:13px">{s["address"]}</span>
            {note}
          </td>
          <td style="padding:10px 8px;border-bottom:1px solid #2d4a32;color:#d4e6ca;font-size:13px;white-space:nowrap">{s["drive"]}</td>
        </tr>"""

    tank_line = f"&nbsp;&nbsp;·&nbsp;&nbsp;<strong>{day_tanks} tanks</strong>" if day_tanks else ""

    html = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#0d1a10;font-family:'Helvetica Neue',Arial,sans-serif">
<div style="max-width:600px;margin:0 auto;padding:24px 16px">

  <div style="background:linear-gradient(135deg,#0d1a10,#1a2e1f);border:1px solid rgba(201,168,76,0.3);
              border-radius:10px;padding:20px 24px;margin-bottom:20px">
    <div style="color:#c9a84c;font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:6px">
      GreenGuard USA · Daily Route
    </div>
    <div style="color:#fff;font-size:22px;font-weight:900">{day_label}</div>
    <div style="color:#a8edc0;margin-top:6px;font-size:14px">
      <strong>{len(stops)} stops</strong>&nbsp;&nbsp;·&nbsp;&nbsp;<strong>{day_d} mi / ~{day_m} min</strong>{tank_line}
    </div>
  </div>

  <table style="width:100%;border-collapse:collapse;background:#111c13;border-radius:10px;overflow:hidden;
                border:1px solid rgba(122,171,130,0.2)">
    {rows}
  </table>

  <div style="margin-top:16px;text-align:center">
    <a href="{maps_link}" style="display:inline-block;background:#c9a84c;color:#0d1a10;font-weight:800;
       font-size:14px;padding:12px 28px;border-radius:4px;text-decoration:none">
      Open Full Route in Google Maps
    </a>
  </div>

  <div style="margin-top:20px;text-align:center;color:rgba(255,255,255,0.2);font-size:11px">
    GreenGuard USA · 1519 Parkway, Austin TX 78703
  </div>

</div>
</body>
</html>"""
    return subject, html


def send_route_email(gmail_service, subject: str, html: str):
    msg = MIMEMultipart("alternative")
    msg["To"]      = ROUTE_TO
    msg["From"]    = "admin@greenguard-usa.com"
    msg["Subject"] = subject
    msg.attach(MIMEText(html, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail_service.users().messages().send(userId="me", body={"raw": raw}).execute()


def run(target_date: datetime | None = None):
    if target_date is None:
        # Default: tomorrow — script runs at midnight for the next day's route
        today = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        target_date = today + timedelta(days=1)

    day_start = target_date
    day_end   = target_date.replace(hour=23, minute=59, second=59)

    gmail_service, creds = authenticate()
    cal_service = build("calendar", "v3", credentials=creds)

    # Fetch today's events
    days = ro.fetch_week(cal_service, day_start, day_end)
    if not days:
        print(f"No appointments on {target_date.strftime('%a %b %-d')} — no email sent.")
        return

    day_label = list(days.keys())[0]
    appts     = list(days.values())[0]
    valid     = [a for a in appts if a["address"]]

    if not valid:
        print(f"No appointments with addresses on {day_label} — no email sent.")
        return

    # Build distance matrix and optimize
    customer_tanks = ro._load_customer_tanks()
    cache          = ro._load_cache()
    all_locs       = [ro.DEPOT] + [a["address"] for a in valid]
    addr_idx       = {a["address"]: i + 1 for i, a in enumerate(valid)}
    dist_all, mins_all = ro.build_matrix(all_locs, cache)

    n     = len(valid)
    idxs  = [0] + list(range(1, n + 1))
    sub_n = len(idxs)
    dist  = [[dist_all[idxs[i]][idxs[j]] for j in range(sub_n)] for i in range(sub_n)]
    mins  = [[mins_all[idxs[i]][idxs[j]] for j in range(sub_n)] for i in range(sub_n)]

    perm, day_d_m, day_m_s = ro.optimize(n, dist, mins)
    day_d = round(day_d_m / 1609.34, 1)
    day_m = round(day_m_s / 60)

    stops     = []
    day_tanks = 0
    ordered   = []
    prev      = 0
    for rank, stop_idx in enumerate(perm, 1):
        ap     = valid[stop_idx - 1]
        d_mi   = round(dist[prev][stop_idx] / 1609.34, 1)
        d_mn   = round(mins[prev][stop_idx] / 60)
        email  = (ap.get("email") or "").lower()
        tanks  = customer_tanks.get(email, 0)
        day_tanks += tanks
        stops.append({
            "name":    ap["name"],
            "sched":   ap["sched"],
            "address": ap["address"],
            "notes":   ap.get("notes"),
            "drive":   f"{d_mi} mi / ~{d_mn} min",
            "tanks":   tanks,
        })
        ordered.append(ap["address"])
        prev = stop_idx

    maps_link = ro.maps_url(ordered)
    subject, html = _build_email(day_label, stops, day_d, day_m, day_tanks, maps_link)

    send_route_email(gmail_service, subject, html)
    print(f"Route email sent to {ROUTE_TO}  |  {day_label}  |  {len(stops)} stops  |  {day_d} mi")


if __name__ == "__main__":
    target = None
    if len(sys.argv) > 1:
        target = datetime.fromisoformat(sys.argv[1]).replace(tzinfo=TZ)
    run(target)
