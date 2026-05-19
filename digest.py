"""
Daily digest — formatted entirely in Python, no Claude needed.
Sent once per day at DIGEST_HOUR (default 8am) to SENDER_EMAIL.
"""

import time
from datetime import datetime
from zoneinfo import ZoneInfo


DIGEST_HOUR = 8  # 8 AM local time


def should_send_digest(last_sent_ts: float, timezone: str) -> bool:
    """True if it's past DIGEST_HOUR today and we haven't sent yet today."""
    tz = ZoneInfo(timezone)
    now = datetime.now(tz)
    if now.hour < DIGEST_HOUR:
        return False
    # last_sent_ts == 0 means never sent
    if last_sent_ts == 0:
        return True
    last_dt = datetime.fromtimestamp(last_sent_ts, tz)
    return last_dt.date() < now.date()


def build_digest(
    stats: dict,
    high_urgency_emails: list[dict],
    today_route: list[dict],
    timezone: str,
) -> tuple[str, str]:
    """Return (subject, plain-text body)."""
    tz = ZoneInfo(timezone)
    today = datetime.now(tz).strftime("%A, %b %-d")

    lines = [
        f"Good morning — here's your Greenguard daily summary for {today}.",
        "",
    ]

    # Email activity
    total = stats.get("total", 0)
    by_type = stats.get("by_type", {})
    urgency_count = stats.get("high_urgency_count", 0)

    lines.append("EMAIL ACTIVITY (last 24 hours)")
    lines.append(f"  Total processed: {total}")
    for cls, count in sorted(by_type.items()):
        lines.append(f"  {cls.capitalize()}: {count}")

    if urgency_count:
        lines.append(f"")
        lines.append(f"  ⚠ HIGH URGENCY ({urgency_count})")
        for item in high_urgency_emails:
            lines.append(f'    · "{item["subject"]}" from {item["sender"]}')

    # Today's route
    lines += ["", "TODAY'S APPOINTMENTS"]
    if today_route:
        total_miles = sum(a.get("distance_miles") or 0 for a in today_route)
        for appt in today_route:
            start_str = appt["start"].strftime("%-I:%M %p")
            name = appt.get("summary", "")[:55]
            dist = appt.get("distance_miles")
            dur = appt.get("duration_minutes")
            addr = appt.get("address", "")

            line = f"  {start_str}  {name}"
            if dist is not None:
                line += f"  •  {dist} mi / {dur} min"
            if addr:
                line += f"\n           {addr}"
            lines.append(line)

        lines.append(f"")
        lines.append(f"  Total driving: {total_miles:.1f} miles")
    else:
        lines.append("  No appointments scheduled today")

    lines += ["", "—", "Greenguard USA Agent"]

    subject = (
        f"Daily Summary · {today} · "
        f"{total} email{'s' if total != 1 else ''}, "
        f"{len(today_route or [])} appt{'s' if len(today_route or []) != 1 else ''}"
    )
    return subject, "\n".join(lines)
