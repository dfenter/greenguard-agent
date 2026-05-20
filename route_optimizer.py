"""
Greenguard USA — Route Optimizer
Run: python route_optimizer.py [YYYY-MM-DD] [YYYY-MM-DD]
     (defaults to current Mon–Sat week)

Design:
- Persistent cache (.dist_cache.json) with 30-day TTL — re-runs skip API calls
- Single batched matrix across ALL days — minimum API requests per run
- Brute-force ≤8 stops, nearest-neighbor above (never O(n!))
- Traffic-aware times via departure_time (bypasses cache, today-only)
- Google Maps URL per day — one tap opens full route on phone
- Notes extraction — shows service notes per stop
- Address validation — flags missing/bad addresses before day starts
- Load-balance summary — stops + miles per day at a glance
"""

import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta
from itertools import permutations
from pathlib import Path
from zoneinfo import ZoneInfo

import googlemaps
from dotenv import load_dotenv
import calcom_client

load_dotenv()

DEPOT        = os.getenv("DEPOT_ADDRESS", "1519 Parkway Austin TX 78703")
MAPS_KEY     = os.getenv("GOOGLE_MAPS_API_KEY", "")
TZ_NAME      = os.getenv("CALENDAR_TIMEZONE", "America/Chicago")
TZ           = ZoneInfo(TZ_NAME)
CACHE_FILE   = Path(__file__).parent / ".dist_cache.json"
CACHE_TTL    = 30 * 86_400   # 30 days in seconds
BRUTE_LIMIT  = 8             # brute-force up to this many stops
RUSH_HOURS   = {(7, 9), (16, 18)}  # CDT morning + evening rush windows


# ── Cache (TTL-aware) ─────────────────────────────────────────────────────────

def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            return {}
    return {}

def _save_cache(cache: dict):
    CACHE_FILE.write_text(json.dumps(cache))

def _prune_cache(cache: dict) -> int:
    """Remove entries older than CACHE_TTL. Returns count removed."""
    cutoff = time.time() - CACHE_TTL
    stale = [k for k, v in cache.items() if isinstance(v, dict) and v.get("ts", 0) < cutoff]
    for k in stale:
        del cache[k]
    return len(stale)

def _norm(addr: str) -> str:
    return " ".join(addr.lower().split())

def _key(a: str, b: str) -> str:
    return f"{_norm(a)}|||{_norm(b)}"

def _get(cache: dict, k: str) -> tuple[int, int] | None:
    v = cache.get(k)
    if v is None:
        return None
    if isinstance(v, dict):
        return v["d"], v["s"]
    if isinstance(v, (list, tuple)) and len(v) == 2:  # legacy format
        return v[0], v[1]
    return None

def _put(cache: dict, k: str, d: int, s: int):
    cache[k] = {"d": d, "s": s, "ts": int(time.time())}


# ── Google Maps ───────────────────────────────────────────────────────────────

def _gmaps() -> googlemaps.Client:
    if not MAPS_KEY:
        raise RuntimeError("GOOGLE_MAPS_API_KEY not set in .env")
    return googlemaps.Client(key=MAPS_KEY)

def build_matrix(
    locs: list[str],
    cache: dict,
    departure_time: datetime | None = None,
) -> tuple[list[list[int]], list[list[int]]]:
    """
    Full n×n distance matrix. Fetches only pairs absent from cache.
    Pass departure_time for traffic-aware estimates (skips cache — live only).
    Batches into 10×10 chunks (≤100 elements per request).
    """
    client = _gmaps()
    n = len(locs)
    live = departure_time is not None

    missing_origins: list[str] = []
    missing_dests:   list[str] = []
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            if live or _get(cache, _key(locs[i], locs[j])) is None:
                if locs[i] not in missing_origins:
                    missing_origins.append(locs[i])
                if locs[j] not in missing_dests:
                    missing_dests.append(locs[j])

    BATCH = 10
    for i0 in range(0, len(missing_origins), BATCH):
        orig_batch = missing_origins[i0:i0 + BATCH]
        for j0 in range(0, len(missing_dests), BATCH):
            dest_batch = missing_dests[j0:j0 + BATCH]
            kwargs = dict(origins=orig_batch, destinations=dest_batch, mode="driving")
            if live:
                kwargs["departure_time"] = departure_time
            result = client.distance_matrix(**kwargs)
            for ri, row in enumerate(result["rows"]):
                for rj, el in enumerate(row["elements"]):
                    k = _key(orig_batch[ri], dest_batch[rj])
                    if el["status"] == "OK":
                        d = el["distance"]["value"]
                        # Use duration_in_traffic when available (live mode)
                        s = el.get("duration_in_traffic", el["duration"])["value"]
                        if not live:
                            _put(cache, k, d, s)
                        else:
                            # Store live result temporarily (not persisted)
                            cache[k] = {"d": d, "s": s, "ts": int(time.time())}
                    else:
                        if not live:
                            _put(cache, k, 999_999_999, 999_999)

    if not live:
        _save_cache(cache)

    dist = [[0] * n for _ in range(n)]
    mins = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i != j:
                val = _get(cache, _key(locs[i], locs[j])) or (0, 0)
                dist[i][j], mins[i][j] = val
    return dist, mins


# ── Routing ───────────────────────────────────────────────────────────────────

def _nearest_neighbor(n: int, dist: list) -> list[int]:
    unvisited = set(range(1, n + 1))
    route, cur = [], 0
    while unvisited:
        nxt = min(unvisited, key=lambda j: dist[cur][j])
        route.append(nxt)
        cur = nxt
        unvisited.remove(nxt)
    return route

def _brute_force(n: int, dist: list, mins: list) -> tuple[list[int], int, int]:
    best_d, best_m, best_p = float("inf"), 0, None
    for perm in permutations(range(1, n + 1)):
        route = (0,) + perm + (0,)
        d = sum(dist[route[k]][route[k + 1]] for k in range(len(route) - 1))
        if d < best_d:
            best_d = d
            best_m = sum(mins[route[k]][route[k + 1]] for k in range(len(route) - 1))
            best_p = list(perm)
    return best_p, best_d, best_m

def optimize(n: int, dist: list, mins: list) -> tuple[list[int], int, int]:
    if n == 0:
        return [], 0, 0
    if n == 1:
        return [1], dist[0][1] + dist[1][0], mins[0][1] + mins[1][0]
    if n <= BRUTE_LIMIT:
        return _brute_force(n, dist, mins)
    perm  = _nearest_neighbor(n, dist)
    route = [0] + perm + [0]
    d = sum(dist[route[k]][route[k + 1]] for k in range(len(route) - 1))
    m = sum(mins[route[k]][route[k + 1]] for k in range(len(route) - 1))
    return perm, d, m


# ── Address & notes extraction ────────────────────────────────────────────────

def _extract_acuity_link(desc: str) -> str | None:
    m = re.search(r"Change Appointment:\s*(https://\S+)", desc)
    return m.group(1) if m else None

def _extract_acuity_id(desc: str) -> str | None:
    m = re.search(r"AcuityID=(\d+)", desc)
    return m.group(1) if m else None

def _extract_email(desc: str) -> str | None:
    m = re.search(r"Email:\s*(\S+@\S+)", desc)
    return m.group(1).strip() if m else None


def extract_address(event: dict) -> str | None:
    desc = event.get("description", "") or ""
    m = re.search(
        r"Address\n={4,}\nPlease enter the address for the service to be performed::\s*(.+?)(?:\n\n|\Z)",
        desc, re.DOTALL,
    )
    if m:
        addr = m.group(1).strip()
        if addr and re.search(r"\d", addr):
            return addr
    loc = (event.get("location") or "").strip()
    if loc and re.search(r"\d", loc):
        return loc
    return None

def extract_notes(event: dict) -> str | None:
    desc = event.get("description", "") or ""
    m = re.search(r"Notes:\s*\S[^\n]*(?:\n(?!Change Appointment)[^\n]+)*", desc)
    if not m:
        return None
    raw = m.group(0).replace("Notes:", "").strip()
    # Strip the address line if it duplicates the address field
    addr = extract_address(event) or ""
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip() and _norm(ln.strip()) != _norm(addr)]
    return " | ".join(lines) if lines else None


# ── Rush-hour flag ────────────────────────────────────────────────────────────

def _is_rush(dt: datetime) -> bool:
    h = dt.hour
    return any(lo <= h < hi for lo, hi in RUSH_HOURS)


# ── Google Maps URL ───────────────────────────────────────────────────────────

def maps_url(ordered_addrs: list[str]) -> str:
    """Return a Google Maps Directions URL for the full route including depot."""
    stops = [DEPOT] + ordered_addrs + [DEPOT]
    encoded = [urllib.parse.quote_plus(s) for s in stops]
    origin      = encoded[0]
    destination = encoded[-1]
    waypoints   = "|".join(encoded[1:-1])
    url = f"https://www.google.com/maps/dir/{origin}/{destination}"
    if waypoints:
        url += f"?waypoints={waypoints}"
    return url


# ── Calendar fetch ────────────────────────────────────────────────────────────

def fetch_week(calendar_service, start: datetime, end: datetime) -> dict[str, list[dict]]:
    result = (
        calendar_service.events()
        .list(
            calendarId="primary",
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            fields="items(id,summary,start,end,description,location)",
        )
        .execute()
    )

    days: dict[str, list[dict]] = {}
    for ev in result.get("items", []):
        raw_start = ev["start"].get("dateTime")
        if not raw_start:
            continue
        desc   = ev.get("description", "") or ""
        dt     = datetime.fromisoformat(raw_start).astimezone(TZ)
        dt_end = datetime.fromisoformat(ev["end"]["dateTime"]).astimezone(TZ)
        duration_mins = int((dt_end - dt).total_seconds() / 60)
        day_key = dt.strftime("%a %b %-d")
        name    = ev.get("summary", "").split(":")[0].strip()
        acuity_id = _extract_acuity_id(desc)
        days.setdefault(day_key, []).append({
            "name":          name,
            "sched":         dt.strftime("%-I:%M%p").lower(),
            "dt":            dt,
            "dt_end":        dt_end,
            "duration_mins": duration_mins,
            "address":       extract_address(ev),
            "notes":         extract_notes(ev),
            "email":         _extract_email(desc),
            "is_acuity":     acuity_id is not None,
            "acuity_link":   _extract_acuity_link(desc),
            "gcal_id":       ev.get("id"),
            "calcom_uid":    None,
        })
    return days


def _reschedule_gcal_event(
    calendar_service,
    gcal_id: str,
    new_start: datetime,
    duration_mins: int,
) -> tuple[bool, str]:
    """Update a Google Calendar event's start/end time directly."""
    new_end = new_start + timedelta(minutes=duration_mins)
    try:
        calendar_service.events().patch(
            calendarId="primary",
            eventId=gcal_id,
            body={
                "start": {"dateTime": new_start.isoformat(), "timeZone": TZ_NAME},
                "end":   {"dateTime": new_end.isoformat(),   "timeZone": TZ_NAME},
            },
        ).execute()
        return True, "OK"
    except Exception as e:
        return False, str(e)


# ── Cross-day analysis ───────────────────────────────────────────────────────

def _best_round_trip(addrs: list[str], all_locs: list[str],
                     dist_all: list, mins_all: list) -> int:
    """Optimal round-trip distance (meters) for a set of addresses."""
    if not addrs:
        return 0
    idxs = [all_locs.index(DEPOT)] + [all_locs.index(a) for a in addrs if a in all_locs]
    n = len(idxs) - 1
    if n == 0:
        return 0
    if n == 1:
        return dist_all[idxs[0]][idxs[1]] + dist_all[idxs[1]][idxs[0]]
    best = float("inf")
    for perm in permutations(range(1, n + 1)):
        route = (0,) + perm + (0,)
        d = sum(dist_all[idxs[route[k]]][idxs[route[k + 1]]] for k in range(len(route) - 1))
        if d < best:
            best = d
    return best


def suggest_cross_day_moves(
    days: dict,
    all_locs: list[str],
    dist_all: list,
    mins_all: list,
    min_savings_mi: float = 2.0,
    max_stops: int = 9,
) -> list[dict]:
    """
    Try moving each appointment to every other day.
    Returns list of suggestions sorted by savings desc.
    Each: {name, src_day, dst_day, address, savings_mi, appt}
    """
    day_names  = list(days.keys())
    day_addrs  = {d: [a["address"] for a in appts if a["address"]] for d, appts in days.items()}
    baseline   = {d: _best_round_trip(day_addrs[d], all_locs, dist_all, mins_all) for d in day_names}

    suggestions = []
    for src in day_names:
        src_appts = [a for a in days[src] if a["address"]]
        for appt in src_appts:
            addr = appt["address"]
            new_src = [a for a in day_addrs[src] if a != addr]
            src_new = _best_round_trip(new_src, all_locs, dist_all, mins_all)
            for dst in day_names:
                if dst == src or len(day_addrs[dst]) >= max_stops:
                    continue
                dst_new = _best_round_trip(day_addrs[dst] + [addr], all_locs, dist_all, mins_all)
                savings_m  = (baseline[src] + baseline[dst]) - (src_new + dst_new)
                savings_mi = round(savings_m / 1609.34, 1)
                if savings_mi >= min_savings_mi:
                    suggestions.append({
                        "name":       appt["name"],
                        "src_day":    src,
                        "dst_day":    dst,
                        "address":    addr,
                        "savings_mi": savings_mi,
                        "appt":       appt,
                    })

    # Deduplicate: keep best dst per (name, src)
    seen: dict[tuple, dict] = {}
    for s in sorted(suggestions, key=lambda x: -x["savings_mi"]):
        key = (s["name"], s["src_day"])
        if key not in seen:
            seen[key] = s
    return list(seen.values())


# ── Interactive rescheduling ──────────────────────────────────────────────────

def interactive_reschedule(
    suggestions: list[dict],
    days: dict,
    calendar_service=None,
    notify: bool = False,
) -> None:
    """
    Walk through each suggestion one-by-one for manual approval.
    Approved moves are queued then applied in batch after final confirm.

    Priority:
      Cal.com bookings  → Cal.com API reschedule (notify=False by default)
      All other events  → Google Calendar patch (date/time update only)

    notify=True sends Cal.com reschedule emails to customers.
    """
    if not suggestions:
        print("  No cross-day moves suggested.")
        return

    print(f"\n{'='*58}")
    print(f"  RESCHEDULING — {len(suggestions)} suggested move(s)")
    print(f"{'='*58}\n")

    # Enrich suggestions with Cal.com UIDs if available
    try:
        start_dt = min(a["dt"] for appts in days.values() for a in appts)
        end_dt   = max(a["dt_end"] for appts in days.values() for a in appts)
        cal_bookings = calcom_client.list_bookings(start_dt, end_dt)
        uid_by_email = {b["attendee_email"].lower(): b["uid"] for b in cal_bookings}
    except Exception:
        uid_by_email = {}

    queue: list[dict] = []

    for i, s in enumerate(suggestions, 1):
        appt = s["appt"]
        print(f"  [{i}/{len(suggestions)}] {s['name']}")
        print(f"    From : {s['src_day']} {appt['sched']}")
        print(f"    To   : {s['dst_day']}")
        print(f"    Saves: {s['savings_mi']} mi  |  {appt['address']}")

        # Propose a time: first slot of destination day at 10:00am
        dst_day_appts = days.get(s["dst_day"], [])
        if dst_day_appts:
            last_end = max(a["dt_end"] for a in dst_day_appts)
            proposed_dt = last_end.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        else:
            proposed_dt = appt["dt"]
        proposed_str = proposed_dt.strftime("%Y-%m-%d %H:%M")

        raw = input(f"\n    Proposed time: {proposed_str}\n    New time (Enter to keep, or YYYY-MM-DD HH:MM): ").strip()
        if raw:
            try:
                proposed_dt = datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
                proposed_str = proposed_dt.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                print("    Invalid format — keeping proposed time.")

        answer = input(f"    Apply move to {proposed_str}? (y/n): ").strip().lower()
        if answer == "y":
            email = (appt.get("email") or "").lower()
            uid   = uid_by_email.get(email)
            queue.append({
                "name":          s["name"],
                "src_day":       s["src_day"],
                "dst_day":       s["dst_day"],
                "new_dt":        proposed_dt,
                "duration_mins": appt.get("duration_mins", 30),
                "is_acuity":     appt["is_acuity"],
                "gcal_id":       appt.get("gcal_id"),
                "calcom_uid":    uid,
            })
            print("    → Queued\n")
        else:
            print("    → Skipped\n")

    if not queue:
        print("  No moves queued.")
        return

    print(f"\n{'─'*58}")
    print(f"  Queued {len(queue)} move(s):")
    for m in queue:
        print(f"    • {m['name']}: {m['src_day']} → {m['dst_day']} @ {m['new_dt'].strftime('%Y-%m-%d %H:%M')}")
    print(f"{'─'*58}")

    confirm = input(f"\n  Apply {len(queue)} move(s)? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("  Cancelled — nothing applied.")
        return

    print()
    for m in queue:
        name     = m["name"]
        new_time = m["new_dt"].strftime("%Y-%m-%d %H:%M")

        if m["calcom_uid"]:
            # Cal.com-native booking — use API (fast, handles Cal.com calendar sync)
            ok, msg = calcom_client.reschedule_booking(m["calcom_uid"], m["new_dt"], notify=notify)
            status  = "✓" if ok else f"✗ {msg}"
            print(f"  {name} — Cal.com → {new_time}  {status}")

        elif m["gcal_id"] and calendar_service:
            # Acuity or other Google Calendar event — patch directly
            ok, msg = _reschedule_gcal_event(
                calendar_service, m["gcal_id"], m["new_dt"], m["duration_mins"]
            )
            status = "✓" if ok else f"✗ {msg}"
            src = "Acuity/GCal"
            print(f"  {name} — {src} → {new_time}  {status}")

        else:
            print(f"  {name} — no calendar ID found, reschedule manually")

    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def run(
    calendar_service,
    start: datetime | None = None,
    end:   datetime | None = None,
    live_traffic: bool = False,
    reschedule: bool = False,
    notify: bool = False,
):
    if start is None:
        today = datetime.now(TZ)
        start = (today - timedelta(days=today.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    if end is None:
        end = start + timedelta(days=5, hours=23, minutes=59)

    print(f"\nFetching {start.strftime('%b %-d')}–{end.strftime('%b %-d')} …")
    days = fetch_week(calendar_service, start, end)
    if not days:
        print("No appointments found.")
        return

    # ── Load-balance summary ──────────────────────────────────────────────────
    print(f"\n{'─'*44}")
    print(f"  {'Day':<14} {'Stops':>5}  Addresses")
    print(f"{'─'*44}")
    for day, appts in days.items():
        good  = sum(1 for a in appts if a["address"])
        bad   = sum(1 for a in appts if not a["address"])
        warn  = f"  ⚠ {bad} missing addr" if bad else ""
        print(f"  {day:<14} {len(appts):>5}{warn}")
    print(f"{'─'*44}\n")

    # ── Build single distance matrix for all addresses ────────────────────────
    cache     = _load_cache()
    pruned    = _prune_cache(cache)
    if pruned:
        print(f"  (pruned {pruned} stale cache entries)\n")

    all_addrs = list({a["address"] for appts in days.values() for a in appts if a["address"]})
    all_locs  = [DEPOT] + all_addrs
    addr_idx  = {a: i + 1 for i, a in enumerate(all_addrs)}

    depart = datetime.now(TZ) if live_traffic else None
    label  = "live traffic" if live_traffic else f"{len(all_locs)**2 - len(all_locs)} pairs, cache-backed"
    print(f"  Matrix: {len(all_locs)} locations ({label}) …\n")

    dist_all, mins_all = build_matrix(all_locs, cache, departure_time=depart)

    week_d = week_m = 0

    for day, appts in days.items():
        valid   = [a for a in appts if a["address"]]
        no_addr = [a for a in appts if not a["address"]]
        n       = len(valid)

        print(f"{'='*58}")
        print(f"  {day}  ({len(appts)} stop{'s' if len(appts) != 1 else ''})")
        print(f"{'='*58}\n")

        # Address validation warnings
        for a in no_addr:
            print(f"  ⚠  {a['name']} ({a['sched']}) — NO ADDRESS on file\n")

        if n == 0:
            continue

        # Slice sub-matrix
        idxs  = [0] + [addr_idx[a["address"]] for a in valid]
        sub_n = len(idxs)
        dist  = [[dist_all[idxs[i]][idxs[j]] for j in range(sub_n)] for i in range(sub_n)]
        mins  = [[mins_all[idxs[i]][idxs[j]] for j in range(sub_n)] for i in range(sub_n)]

        perm, day_d, day_m = optimize(n, dist, mins)

        ordered_addrs = []
        prev = 0
        for rank, stop_idx in enumerate(perm, 1):
            ap    = valid[stop_idx - 1]
            d_mi  = round(dist[prev][stop_idx] / 1609.34, 1)
            d_mn  = round(mins[prev][stop_idx] / 60)
            rush  = " ⚡rush hour" if _is_rush(ap["dt"]) else ""
            print(f"  {rank}. {ap['name']:<22} {ap['sched']}{rush}")
            print(f"     {ap['address']}")
            print(f"     Drive: {d_mi} mi / ~{d_mn} min")
            if ap["notes"]:
                print(f"     Note:  {ap['notes']}")
            print()
            ordered_addrs.append(ap["address"])
            prev = stop_idx

        ret_d = round(dist[prev][0] / 1609.34, 1)
        ret_m = round(mins[prev][0] / 60)
        print(f"  → Depot  {ret_d} mi / ~{ret_m} min")
        print(f"     {DEPOT}\n")
        print(f"  Total: {round(day_d/1609.34,1)} mi / ~{round(day_m/60)} min")
        print(f"  Maps:  {maps_url(ordered_addrs)}\n")

        week_d += day_d
        week_m += day_m

    print(f"{'='*58}")
    print(f"  WEEK TOTAL: {round(week_d/1609.34,1)} mi / ~{round(week_m/60)} min")
    print(f"{'='*58}\n")

    # ── Cross-day suggestions ─────────────────────────────────────────────────
    print("  Analyzing cross-day moves …")
    suggestions = suggest_cross_day_moves(days, all_locs, dist_all, mins_all)
    if suggestions:
        print(f"\n  Top moves (≥2 mi savings each):\n")
        for s in suggestions[:8]:
            print(f"    {s['name']:<22} {s['src_day']} → {s['dst_day']}  saves {s['savings_mi']} mi")
        print()
    else:
        print("  Schedule is well optimized — no cross-day moves suggested.\n")

    if reschedule:
        interactive_reschedule(suggestions, days, calendar_service=calendar_service, notify=notify)


if __name__ == "__main__":
    from gmail_client import authenticate
    from calendar_client import get_calendar_service

    live        = "--live"       in sys.argv
    reschedule  = "--reschedule" in sys.argv
    notify      = "--notify"     in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    _, creds = authenticate()
    cal = get_calendar_service(creds)

    start = end = None
    if len(args) >= 2:
        start = datetime.fromisoformat(args[0]).replace(tzinfo=TZ)
        end   = datetime.fromisoformat(args[1]).replace(hour=23, minute=59, tzinfo=TZ)

    run(cal, start, end, live_traffic=live, reschedule=reschedule, notify=notify)
