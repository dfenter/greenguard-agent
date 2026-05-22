"""
monthly_optimizer.py — Optimize the full monthly appointment schedule.

Fetches all appointments for a given month from Google Calendar, then uses
iterative greedy improvement to find moves that reduce total driving distance.

Constraints:
  - No appointment moves more than ±5 calendar days from its original date
  - Mon–Sat only (no Sundays)
  - Min 2-mile savings per move

Each day is scheduled starting at 9 AM, appointments stacked consecutively
in optimized route order (drive + service duration), leaving afternoon buffer.

Usage:
    python3 monthly_optimizer.py              # current month
    python3 monthly_optimizer.py 2026-06      # specific month
    python3 monthly_optimizer.py 2026-06 --apply  # run approval + apply to GCal
"""

import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from itertools import permutations

from dotenv import load_dotenv
from googleapiclient.discovery import build

import route_optimizer as ro

load_dotenv()

MAX_DAYS        = 5       # ±5 calendar days constraint
MIN_SAVINGS_MI  = 2.0     # minimum savings to suggest a move
DAY_START_HOUR  = 9       # appointments begin at 9 AM
MAX_ITER        = 50      # safety cap on optimization iterations


# ── Date helpers ──────────────────────────────────────────────────────────────

def _month_range(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=ro.TZ)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=ro.TZ) - timedelta(seconds=1)
    else:
        end = datetime(year, month + 1, 1, tzinfo=ro.TZ) - timedelta(seconds=1)
    return start, end


MIN_GAP_DAYS = 20   # minimum days between appointments for the same customer


def _customer_dates(appt: dict, by_date: dict) -> list[date]:
    """All dates this customer currently has appointments (by email match)."""
    email = (appt.get("email") or "").lower()
    if not email:
        return []
    return [
        d for d, appts in by_date.items()
        for a in appts
        if (a.get("email") or "").lower() == email
    ]


def _valid_target_dates(original_dt: datetime, all_dates: set[date],
                        appt: dict | None = None,
                        by_date: dict | None = None) -> list[date]:
    """
    Dates within ±MAX_DAYS of original, Mon–Sat, that exist in the calendar,
    and that don't violate:
      - same-day duplicate (customer already has appointment that day)
      - MIN_GAP_DAYS spacing (no other appointment within 20 days)
    """
    orig = original_dt.date()

    # Collect this customer's other appointment dates (excluding current appt)
    other_dates: list[date] = []
    if appt and by_date:
        gcal_id = appt.get("gcal_id")
        email   = (appt.get("email") or "").lower()
        if email:
            other_dates = [
                d for d, day_appts in by_date.items()
                for a in day_appts
                if (a.get("email") or "").lower() == email
                and a.get("gcal_id") != gcal_id
            ]

    candidates = sorted(
        d for d in all_dates
        if d != orig
        and abs((d - orig).days) <= MAX_DAYS
        and d.weekday() < 6
    )

    valid = []
    for d in candidates:
        # No same-day duplicate
        if d in other_dates:
            continue
        # No appointment within MIN_GAP_DAYS of any other appointment
        if any(abs((d - od).days) < MIN_GAP_DAYS for od in other_dates):
            continue
        valid.append(d)

    return valid


# ── Distance helpers ──────────────────────────────────────────────────────────

def _day_addrs(d: date, by_date: dict) -> list[str]:
    return [a["address"] for a in by_date.get(d, []) if a.get("address")]


def _day_dist(d: date, by_date: dict, all_locs: list, dist_all: list) -> int:
    return ro._best_round_trip(_day_addrs(d, by_date), all_locs, dist_all, dist_all)


def _day_dist_without(d: date, appt: dict, by_date: dict,
                      all_locs: list, dist_all: list) -> int:
    addrs = [a["address"] for a in by_date.get(d, [])
             if a.get("address") and a["gcal_id"] != appt["gcal_id"]]
    return ro._best_round_trip(addrs, all_locs, dist_all, dist_all)


def _day_dist_with(d: date, appt: dict, by_date: dict,
                   all_locs: list, dist_all: list) -> int:
    addrs = _day_addrs(d, by_date)
    if appt.get("address") and appt["address"] not in addrs:
        addrs = addrs + [appt["address"]]
    return ro._best_round_trip(addrs, all_locs, dist_all, dist_all)


# ── Day scheduling (9 AM start, stacked consecutively) ───────────────────────

def _schedule_day(d: date, by_date: dict,
                  all_locs: list, dist_all: list, mins_all: list) -> list[tuple]:
    """
    Return list of (appt, new_start_datetime) for all appointments on date d,
    starting at 9 AM and stacked in optimized route order.
    Drive time + service duration consumed between each stop.
    """
    appts = [a for a in by_date.get(d, []) if a.get("address")]
    if not appts:
        return []

    # Build sub-matrix for this day
    addrs    = [ro.DEPOT] + [a["address"] for a in appts]
    n        = len(appts)
    idx_map  = {addr: all_locs.index(addr) for addr in addrs if addr in all_locs}

    def sub_dist(i, j):
        gi = idx_map.get(addrs[i], 0)
        gj = idx_map.get(addrs[j], 0)
        return dist_all[gi][gj], mins_all[gi][gj]

    # Nearest-neighbor from depot
    visited = [False] * n
    order   = []
    cur     = 0   # depot
    for _ in range(n):
        best_j, best_d = -1, float("inf")
        for j in range(n):
            if not visited[j]:
                d_val, _ = sub_dist(cur, j + 1)
                if d_val < best_d:
                    best_d, best_j = d_val, j
        visited[best_j] = True
        order.append(best_j)
        cur = best_j + 1

    # Assign times starting at DAY_START_HOUR
    cursor = datetime(d.year, d.month, d.day,
                      DAY_START_HOUR, 0, 0, tzinfo=ro.TZ)
    prev_addr_idx = 0  # depot index in addrs list
    result = []
    for j in order:
        appt = appts[j]
        _, drive_secs = sub_dist(prev_addr_idx, j + 1)
        cursor += timedelta(seconds=drive_secs)
        # Round to nearest 15 min
        mins_past = cursor.minute % 15
        if mins_past:
            cursor += timedelta(minutes=15 - mins_past)
        result.append((appt, cursor))
        cursor += timedelta(minutes=appt.get("duration_mins", 30))
        prev_addr_idx = j + 1

    return result


# ── Optimization ──────────────────────────────────────────────────────────────

def _optimize_month(by_date: dict, all_locs: list,
                    dist_all: list, mins_all: list) -> list[dict]:
    """
    Iterative greedy improvement. Each iteration finds the single best move
    (highest driving savings ≥ MIN_SAVINGS_MI that respects ±MAX_DAYS).
    Returns ordered list of move dicts.
    """
    all_dates = set(by_date.keys())
    moves     = []

    moved_ids = set()   # each appointment moves at most once

    for iteration in range(MAX_ITER):
        best_savings_m = int(MIN_SAVINGS_MI * 1609.34)
        best_move      = None

        baseline = {d: _day_dist(d, by_date, all_locs, dist_all) for d in all_dates}

        for src_date, appts in list(by_date.items()):
            for appt in appts:
                if not appt.get("address"):
                    continue
                if appt["gcal_id"] in moved_ids:
                    continue

                src_new = _day_dist_without(src_date, appt, by_date, all_locs, dist_all)

                for dst_date in _valid_target_dates(appt["original_dt"], all_dates,
                                                   appt=appt, by_date=by_date):
                    if dst_date == src_date:
                        continue
                    dst_new = _day_dist_with(dst_date, appt, by_date, all_locs, dist_all)

                    savings = (baseline[src_date] + baseline.get(dst_date, 0)) \
                            - (src_new + dst_new)

                    if savings > best_savings_m:
                        best_savings_m = savings
                        best_move = {
                            "appt":       appt,
                            "src_date":   src_date,
                            "dst_date":   dst_date,
                            "savings_mi": round(savings / 1609.34, 1),
                        }

        if best_move is None:
            break

        m    = best_move
        appt = m["appt"]
        moved_ids.add(appt["gcal_id"])
        by_date[m["src_date"]] = [a for a in by_date[m["src_date"]]
                                   if a["gcal_id"] != appt["gcal_id"]]
        by_date[m["dst_date"]].append(appt)
        # Update dt so src_date stays correct in future iterations
        appt["dt"] = appt["dt"].replace(
            year=m["dst_date"].year, month=m["dst_date"].month, day=m["dst_date"].day
        )
        moves.append(m)

    return moves


# ── Print helpers ─────────────────────────────────────────────────────────────

def _print_calendar(by_date: dict, all_locs: list,
                    dist_all: list, mins_all: list):
    print()
    for d in sorted(by_date.keys()):
        appts = by_date[d]
        if not appts:
            continue
        day_d = _day_dist(d, by_date, all_locs, dist_all)
        scheduled = _schedule_day(d, by_date, all_locs, dist_all, mins_all)
        label = d.strftime("%a %b %-d")
        print(f"  {label}  ({len(appts)} stops, {round(day_d/1609.34,1)} mi)")
        for appt, new_start in scheduled:
            t = new_start.strftime("%-I:%M%p").lower()
            tanks = appt.get("tanks", 0)
            tank_str = f"  [{tanks}T]" if tanks else ""
            print(f"    {t}  {appt['name']}{tank_str}")
    print()


# ── Interactive approval ──────────────────────────────────────────────────────

def _apply_all(moves: list[dict], by_date: dict,
               all_locs: list, dist_all: list,
               mins_all: list, cal) -> None:
    """
    Apply all moves: reschedule every appointment in every affected day using
    the full 9 AM stacked schedule so times are coherent, not just dates.
    """
    affected = set()
    for m in moves:
        affected.add(m["src_date"])
        affected.add(m["dst_date"])

    print(f"\n  Rescheduling {len(affected)} affected days in Google Calendar …\n")
    ok_count = err_count = 0

    for d in sorted(affected):
        if not by_date.get(d):
            continue
        scheduled = _schedule_day(d, by_date, all_locs, dist_all, mins_all)
        for appt, new_start in scheduled:
            gcal_id = appt.get("gcal_id")
            if not gcal_id:
                continue
            ok, msg = ro._reschedule_gcal_event(cal, gcal_id, new_start,
                                                appt.get("duration_mins", 30))
            if ok:
                ok_count += 1
                print(f"  ✓  {appt['name']:<28} {d.strftime('%a %b %-d')}  {new_start.strftime('%-I:%M%p').lower()}")
            else:
                err_count += 1
                print(f"  ✗  {appt['name']:<28} {msg}")

    print(f"\n  Done — {ok_count} updated, {err_count} errors\n")


def _interactive_approve(moves: list[dict], by_date: dict,
                         all_locs: list, dist_all: list,
                         mins_all: list, cal) -> None:
    if not moves:
        print("  No moves to apply.")
        return

    print(f"\n  {len(moves)} proposed move(s). Review one by one:\n")
    queue = []

    for i, m in enumerate(moves, 1):
        appt    = m["appt"]
        src_d   = m["src_date"]
        dst_d   = m["dst_date"]
        savings = m["savings_mi"]

        print(f"  [{i}/{len(moves)}] {appt['name']}")
        print(f"    From : {src_d.strftime('%a %b %-d')}  ({appt['dt'].strftime('%-I:%M%p').lower()})")
        print(f"    To   : {dst_d.strftime('%a %b %-d')}")
        print(f"    Saves: {savings} mi")

        # Propose time based on stacked day schedule
        scheduled = _schedule_day(dst_d, by_date, all_locs, dist_all, mins_all)
        if scheduled:
            proposed = scheduled[-1][1] + timedelta(minutes=scheduled[-1][0].get("duration_mins", 30))
            # Round to next 15 min
            mins_past = proposed.minute % 15
            if mins_past:
                proposed += timedelta(minutes=15 - mins_past)
        else:
            proposed = datetime(dst_d.year, dst_d.month, dst_d.day,
                                DAY_START_HOUR, 0, 0, tzinfo=ro.TZ)

        print(f"    Proposed time: {proposed.strftime('%-I:%M %p')}")
        custom = input("    New time (Enter to keep, or HH:MM 24h): ").strip()
        if custom:
            try:
                hh, mm = map(int, custom.split(":"))
                proposed = proposed.replace(hour=hh, minute=mm)
            except ValueError:
                print("    Invalid — keeping proposed time.")

        ans = input("    Apply? (y/n): ").strip().lower()
        if ans == "y":
            queue.append({**m, "new_dt": proposed})
        print()

    if not queue:
        print("  No moves approved.")
        return

    print(f"\n  Applying {len(queue)} move(s) to Google Calendar …\n")
    for m in queue:
        appt   = m["appt"]
        new_dt = m["new_dt"]
        gcal_id = appt.get("gcal_id")
        if gcal_id:
            ok, msg = ro._reschedule_gcal_event(cal, gcal_id, new_dt,
                                                appt.get("duration_mins", 30))
            status = "✓" if ok else f"✗ {msg}"
        else:
            status = "✗ no gcal_id"
        print(f"  {status}  {appt['name']}  → {m['dst_date'].strftime('%a %b %-d')} "
              f"{new_dt.strftime('%-I:%M%p').lower()}")


# ── Main ──────────────────────────────────────────────────────────────────────

def run(year: int, month: int, apply: bool = False):
    from gmail_client import authenticate

    print(f"\n{'='*60}")
    print(f"  Monthly Route Optimizer — "
          f"{datetime(year, month, 1).strftime('%B %Y')}")
    print(f"  Constraint: ±{MAX_DAYS} days  |  Min savings: {MIN_SAVINGS_MI} mi")
    print(f"{'='*60}\n")

    # Auth
    _, creds = authenticate()
    cal      = build("calendar", "v3", credentials=creds)

    # Fetch all events for the month
    start, end = _month_range(year, month)
    print("  Fetching calendar …")
    days_dict = ro.fetch_week(cal, start, end)   # works for any range

    # Flatten to by_date dict, recording original_dt
    by_date: dict[date, list[dict]] = defaultdict(list)
    for appts in days_dict.values():
        for a in appts:
            if not a.get("address"):
                continue
            d = a["dt"].date()
            a["original_dt"] = a["dt"]
            by_date[d].append(a)

    total_appts = sum(len(v) for v in by_date.values())
    print(f"  {total_appts} appointments across {len(by_date)} days\n")

    # Build global distance matrix
    all_addrs = list({a["address"] for appts in by_date.values() for a in appts
                      if a.get("address")})
    all_locs  = [ro.DEPOT] + all_addrs
    cache     = ro._load_cache()
    ro._prune_cache(cache)

    print(f"  Building distance matrix ({len(all_locs)} locations) …")
    dist_all, mins_all = ro.build_matrix(all_locs, cache)

    # Baseline
    baseline_m = sum(_day_dist(d, by_date, all_locs, dist_all) for d in by_date)
    baseline_mi = round(baseline_m / 1609.34, 1)
    print(f"  Baseline: {baseline_mi} mi\n")

    # Optimize
    print("  Optimizing …")
    moves = _optimize_month(by_date, all_locs, dist_all, mins_all)

    optimized_m  = sum(_day_dist(d, by_date, all_locs, dist_all) for d in by_date)
    optimized_mi = round(optimized_m / 1609.34, 1)
    savings_mi   = round(baseline_mi - optimized_mi, 1)
    pct          = round(savings_mi / baseline_mi * 100, 1) if baseline_mi else 0

    print(f"\n  {'─'*56}")
    print(f"  Baseline : {baseline_mi} mi")
    print(f"  Optimized: {optimized_mi} mi")
    print(f"  Savings  : {savings_mi} mi ({pct}%)")
    print(f"  Moves    : {len(moves)}")
    print(f"  {'─'*56}")

    if not moves:
        print("\n  Schedule is already well optimized.\n")
        _print_calendar(by_date, all_locs, dist_all, mins_all)
        return

    # Print proposed moves summary
    print("\n  Proposed moves:")
    for m in moves:
        print(f"    {m['appt']['name']:<28} "
              f"{m['src_date'].strftime('%a %b %-d'):>10} → "
              f"{m['dst_date'].strftime('%a %b %-d'):<10}  "
              f"saves {m['savings_mi']} mi")

    # Print optimized calendar
    print("\n  Optimized calendar:")
    _print_calendar(by_date, all_locs, dist_all, mins_all)

    if apply == "all":
        _apply_all(moves, by_date, all_locs, dist_all, mins_all, cal)

    elif apply:
        _interactive_approve(moves, by_date, all_locs, dist_all, mins_all, cal)
    else:
        print("  Run with --apply to approve and update Google Calendar.\n")


if __name__ == "__main__":
    args  = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = sys.argv[1:]

    now = datetime.now()
    if args:
        try:
            parts = args[0].split("-")
            yr, mo = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            print("Usage: python3 monthly_optimizer.py [YYYY-MM] [--apply]")
            sys.exit(1)
    else:
        yr, mo = now.year, now.month

    apply = "all" if "--apply-all" in flags else ("--apply" in flags)
    run(yr, mo, apply=apply)
