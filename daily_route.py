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
ROUTE_CC   = ["bodhiyoga@gmail.com"]


def _build_email(day_label: str, stops: list[dict], day_d: float, day_m: int,
                 day_tanks: int, maps_link: str) -> tuple[str, str]:
    """Return (subject, html_body)."""
    tank_label = f'{day_tanks} {"Tank" if day_tanks == 1 else "Tanks"}'
    subject = f"GreenGuard Route — {day_label}  |  {len(stops)} stops  |  {day_d} mi"
    if day_tanks:
        subject += f"  |  {tank_label}"

    rows = ""
    for i, s in enumerate(stops, 1):
        is_last  = i == len(stops)
        border   = "" if is_last else "border-bottom:1px solid rgba(122,171,130,0.1);"
        _t       = s.get("tanks", 0)
        _tlabel  = f'{_t} {"Tank" if _t == 1 else "Tanks"}'
        tank_td  = (
            f'<td style="padding:16px 20px 16px 0;{border}vertical-align:middle;'
            f'white-space:nowrap;text-align:right">'
            f'<span style="background:rgba(201,168,76,0.12);border:1px solid rgba(201,168,76,0.4);'
            f'border-radius:4px;padding:4px 10px;color:#c9a84c;font-size:11px;font-weight:800;'
            f'letter-spacing:0.06em;text-transform:uppercase">{_tlabel}</span></td>'
            if _t else f'<td style="padding:16px 8px 16px 0;{border}"></td>'
        )
        note = (
            f'<div style="color:#7aab82;font-size:11px;margin-top:5px;'
            f'font-style:italic;line-height:1.5">{s["notes"]}</div>'
            if s.get("notes") else ""
        )
        rows += f"""
        <tr>
          <td style="padding:16px 8px 16px 20px;{border}vertical-align:middle;width:32px">
            <div style="width:26px;height:26px;line-height:26px;text-align:center;
                        background:rgba(201,168,76,0.15);border:1px solid rgba(201,168,76,0.3);
                        border-radius:6px;color:#c9a84c;font-size:12px;font-weight:900">{i}</div>
          </td>
          <td style="padding:16px 12px;{border}vertical-align:middle">
            <div style="color:#ffffff;font-weight:800;font-size:15px;letter-spacing:-0.02em;line-height:1.2">{s["name"]}</div>
            {"<div style='color:#c9a84c;font-size:11px;font-weight:700;margin-top:2px;letter-spacing:0.02em'>" + s["service"] + "</div>" if s.get("service") else ""}
            <div style="color:#7aab82;font-size:12px;font-weight:700;margin-top:3px;letter-spacing:0.01em">{s["sched"]} &nbsp;&middot;&nbsp; {s["drive"]}</div>
            <div style="color:rgba(212,230,202,0.45);font-size:12px;margin-top:2px">{s["address"]}</div>
            {note}
          </td>
          {tank_td}
        </tr>"""

    tank_pill = (
        f'<td style="padding:0 0 0 8px">'
        f'<span style="background:rgba(201,168,76,0.12);border:1px solid rgba(201,168,76,0.45);'
        f'border-radius:4px;padding:5px 12px;color:#c9a84c;font-size:11px;font-weight:800;'
        f'letter-spacing:0.06em;text-transform:uppercase;display:inline-block">{tank_label}</span></td>'
        if day_tanks else ""
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta name="color-scheme" content="dark">
<meta name="supported-color-schemes" content="dark">
<link href="https://fonts.googleapis.com/css2?family=Nunito+Sans:wght@400;600;700;800;900&display=swap" rel="stylesheet">
<style>
:root {{ color-scheme: dark; }}
body {{ background-color: #0a1a0d !important; color: #d4e6ca !important; }}
* {{ -webkit-text-size-adjust: none; }}
</style>
</head>
<body style="margin:0;padding:0;background:#0a1a0d;font-family:'Nunito Sans','Helvetica Neue',Arial,sans-serif;color:#d4e6ca">
<div style="max-width:560px;margin:0 auto;padding:20px 16px">

  <!-- Logo bar -->
  <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px">
    <tr>
      <td style="padding:0 4px">
        <span style="font-size:15px;font-weight:900;color:#ffffff;letter-spacing:-0.02em">Green<span style="color:#7dffaa">Guard</span> USA</span>
        <span style="color:rgba(122,171,130,0.4);font-size:13px;margin-left:10px">&middot; Daily Route</span>
      </td>
    </tr>
  </table>

  <!-- Header hero -->
  <table width="100%" cellpadding="0" cellspacing="0" style="background:linear-gradient(135deg,#0d1a10 0%,#1a2e1f 50%,#2d4a32 100%);border:1px solid rgba(122,171,130,0.2);border-radius:12px;margin-bottom:10px">
    <tr>
      <td style="padding:24px 24px 10px 24px">
        <div style="color:#c9a84c;font-size:10px;font-weight:800;letter-spacing:0.15em;text-transform:uppercase;margin-bottom:8px">Today&#39;s Schedule</div>
        <div style="color:#ffffff;font-size:28px;font-weight:900;letter-spacing:-0.03em;line-height:1.1">{day_label}</div>
      </td>
    </tr>
    <tr>
      <td style="padding:12px 24px 22px 24px">
        <table cellpadding="0" cellspacing="0">
          <tr>
            <td style="background:rgba(13,26,16,0.6);border:1px solid rgba(122,171,130,0.2);border-radius:6px;padding:6px 14px;color:#a8edc0;font-size:12px;font-weight:800;letter-spacing:0.04em">{len(stops)} Stops</td>
            <td style="padding:0 0 0 8px"><span style="background:rgba(13,26,16,0.6);border:1px solid rgba(122,171,130,0.2);border-radius:6px;padding:6px 14px;color:#a8edc0;font-size:12px;font-weight:800;letter-spacing:0.04em;display:inline-block">{day_d} mi &nbsp;/&nbsp; ~{day_m} min</span></td>
            {tank_pill}
          </tr>
        </table>
      </td>
    </tr>
  </table>

  <!-- Stop list -->
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#111c13;border:1px solid rgba(122,171,130,0.15);border-radius:12px;overflow:hidden;margin-bottom:12px">
    {rows}
  </table>

  <!-- Maps button -->
  <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:20px">
    <tr>
      <td style="text-align:center;padding:4px 0">
        <a href="{maps_link}" style="display:inline-block;background:#c9a84c;color:#0a1a0d;font-family:'Nunito Sans','Helvetica Neue',Arial,sans-serif;font-weight:900;font-size:13px;padding:14px 40px;border-radius:6px;text-decoration:none;letter-spacing:0.08em;text-transform:uppercase">Open Full Route in Maps</a>
      </td>
    </tr>
  </table>

  <!-- Footer -->
  <div style="text-align:center;color:rgba(122,171,130,0.2);font-size:10px;letter-spacing:0.08em;text-transform:uppercase">
    GreenGuard USA &nbsp;&middot;&nbsp; 1519 Parkway, Austin TX 78703
  </div>

</div>
</body>
</html>"""
    return subject, html


def send_route_email(gmail_service, subject: str, html: str):
    msg = MIMEMultipart("alternative")
    msg["To"]      = ROUTE_TO
    msg["Cc"]      = ", ".join(ROUTE_CC)
    msg["From"]    = "admin@greenguard-usa.com"
    msg["Subject"] = subject
    msg.attach(MIMEText(html, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail_service.users().messages().send(userId="me", body={"raw": raw}).execute()


def run(target_date: datetime | None = None):
    if target_date is None:
        # Runs at midnight — send today's route
        target_date = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)

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
    customer_tanks, customer_tanks_by_name = ro._load_customer_tanks()
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
        tanks  = customer_tanks.get(email) or customer_tanks_by_name.get(ap.get("name","").strip().split()[-1].lower(), 0)
        day_tanks += tanks
        stops.append({
            "name":    ap["name"],
            "service": ap.get("service", ""),
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
