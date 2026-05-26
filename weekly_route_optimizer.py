#!/usr/bin/env python3
"""
Weekly route optimizer — runs Monday 9 AM CST via GitHub Actions.
Reads the week's bookings from Google Calendar (all Cal.com bookings sync there),
builds an optimized daily route using Google Maps Distance Matrix API,
commits route_plan_YYYY-WW.json and opens a GitHub issue for approval.

Auth: OAuth2 refresh token (GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN).
      No external libraries required — uses only Python built-ins.
"""
import json
import os
import sys
import datetime
import urllib.request
import urllib.parse
import re

DEPOT = '1519 Parkway, Austin, TX 78703'
CALENDAR_ID = 'admin@greenguard-usa.com'
BOOKING_TAG = 'GreenGuard USA'

GOOGLE_MAPS_API_KEY = os.environ.get('GOOGLE_MAPS_API_KEY', '')
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '')
GOOGLE_REFRESH_TOKEN = os.environ.get('GOOGLE_REFRESH_TOKEN', '')

# Load event type durations from cal-event-types.json
_EVENT_TYPES_PATH = os.path.join(os.path.dirname(__file__), '..', 'app', 'lib', 'cal-event-types.json')
try:
    with open(_EVENT_TYPES_PATH) as _f:
        _CAL_EVENT_TYPES = {
            et['title'].lower().strip(): et['durationMin']
            for et in json.load(_f)['eventTypes']
        }
except Exception as e:
    print(f'Warning: could not load cal-event-types.json: {e}')
    _CAL_EVENT_TYPES = {}

# Fallback duration by visit type keyword in title
FALLBACK_DURATIONS = [
    ('biogents', 35),
    ('mosqitter', 45),
    ('ten tank', 90),
    ('six', 75),
    ('four', 60),
    ('two', 45),
    ('one', 30),
    ('assessment', 30),
    ('installation', 60),
]


# ── Google auth ─────────────────────────────────────────────────────────────

def get_google_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Exchange a refresh token for a short-lived access token (no external deps)."""
    data = urllib.parse.urlencode({
        'client_id': client_id,
        'client_secret': client_secret,
        'refresh_token': refresh_token,
        'grant_type': 'refresh_token',
    }).encode()
    req = urllib.request.Request(
        'https://oauth2.googleapis.com/token',
        data=data,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
    )
    with urllib.request.urlopen(req) as resp:
        token_data = json.loads(resp.read())
    if 'access_token' not in token_data:
        print(f'ERROR: token refresh failed: {token_data}')
        sys.exit(1)
    return token_data['access_token']


def gcal_list_events(access_token: str, time_min: str, time_max: str, q: str = 'GreenGuard USA'):
    """Fetch events from Google Calendar within a time range."""
    params = urllib.parse.urlencode({
        'calendarId': CALENDAR_ID,
        'timeMin': time_min,
        'timeMax': time_max,
        'maxResults': 250,
        'singleEvents': 'true',
        'orderBy': 'startTime',
        'q': q,
    })
    url = f'https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(CALENDAR_ID)}/events?{params}'
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {access_token}'})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read()).get('items', [])


# ── Parsing helpers ──────────────────────────────────────────────────────────

def parse_email(description: str) -> str:
    """Extract customer email from event description."""
    if not description:
        return ''
    m = re.search(r'Email:\s*(\S+)', description, re.IGNORECASE)
    return m.group(1).strip() if m else ''


def parse_customer_name(description: str) -> str:
    m = re.search(r'Name:\s*(.+)', description or '', re.IGNORECASE)
    return m.group(1).strip() if m else ''


def normalize_title(raw: str) -> str:
    """Strip 'CustomerName: ' prefix and ' (GreenGuard USA)' suffix."""
    title = re.sub(r'^[^:]+:\s*', '', raw or '')
    title = re.sub(r'\s*\(GreenGuard USA\)\s*$', '', title, flags=re.IGNORECASE)
    return title.strip()


def get_duration(title: str) -> int:
    """Look up service duration in minutes from event type title."""
    normalized = title.lower().strip()
    if normalized in _CAL_EVENT_TYPES:
        return _CAL_EVENT_TYPES[normalized]
    for keyword, duration in FALLBACK_DURATIONS:
        if keyword in normalized:
            return duration
    return 35  # default


# ── Maps helpers ─────────────────────────────────────────────────────────────

def maps_distance_matrix(origins: list, destinations: list) -> list:
    """Return duration_seconds matrix[origin][destination]."""
    o_param = '|'.join(urllib.parse.quote(o) for o in origins)
    d_param = '|'.join(urllib.parse.quote(d) for d in destinations)
    url = (
        f'https://maps.googleapis.com/maps/api/distancematrix/json'
        f'?origins={o_param}&destinations={d_param}&units=imperial&key={GOOGLE_MAPS_API_KEY}'
    )
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read())
    rows = data.get('rows', [])
    return [
        [row['elements'][j].get('duration', {}).get('value', 999999) for j in range(len(destinations))]
        for row in rows
    ]


def compute_optimized_times(depot: str, ordered_stops: list, day_str: str, day_start_hour: int = 9) -> list:
    """Given a route in optimized order, compute new start/end times.
    Day starts at `day_start_hour` AM CT at the depot. For each stop:
      new_start = depot_departure + drive_to_first  (or prev_end + drive_to_next)
      new_end   = new_start + service_duration
    Returns the same stops list with `optimized_start_time` and `optimized_end_time` added."""
    if not ordered_stops:
        return ordered_stops
    # Use CST (-06:00) — matches existing TZ assumption in event handling
    locs = [depot] + [s['address'] for s in ordered_stops]
    try:
        matrix = maps_distance_matrix(locs, locs)
    except Exception:
        return ordered_stops
    cursor = datetime.datetime.fromisoformat(f'{day_str}T{day_start_hour:02d}:00:00-06:00')
    prev_idx = 0
    for i, stop in enumerate(ordered_stops):
        drive_sec = matrix[prev_idx][i + 1]
        cursor += datetime.timedelta(seconds=drive_sec)
        stop['optimized_start_time'] = cursor.isoformat()
        stop['drive_min_from_prev'] = round(drive_sec / 60)
        cursor += datetime.timedelta(minutes=stop['duration_min'])
        stop['optimized_end_time'] = cursor.isoformat()
        prev_idx = i + 1
    return ordered_stops


def nearest_neighbor(depot: str, stops: list) -> list:
    """Farthest-first routing: start at the depot, hit the farthest stop first
    (so the tech tackles the long drive when fresh), then nearest-neighbor back
    toward the depot for the remaining stops. Ends with the closest stop to
    depot so the tech has the shortest drive home."""
    if not stops:
        return []
    if len(stops) == 1:
        return stops

    all_locs = [depot] + [s['address'] for s in stops]
    n = len(all_locs)

    try:
        matrix = maps_distance_matrix(all_locs, all_locs)
    except Exception as e:
        print(f'  Distance matrix failed ({e}), using original order')
        return stops

    visited = [False] * n
    visited[0] = True

    # Step 1: pick the farthest stop from depot as the first visit
    farthest = max(
        (j for j in range(1, n)),
        key=lambda j: matrix[0][j],
        default=None,
    )
    if farthest is None:
        return stops
    route = [farthest]
    visited[farthest] = True

    # Step 2: nearest-neighbor from there, biased toward the depot
    # (each step prefers stops that are both close to current AND closer to depot than current)
    for _ in range(len(stops) - 1):
        curr = route[-1]
        curr_dist_to_depot = matrix[curr][0]
        candidates = [j for j in range(1, n) if not visited[j]]
        if not candidates:
            break
        # Primary: closest to current. Tiebreak: closer to depot than current.
        def score(j):
            return (matrix[curr][j], matrix[j][0] - curr_dist_to_depot)
        nxt = min(candidates, key=score)
        route.append(nxt)
        visited[nxt] = True

    return [stops[i - 1] for i in route if i > 0]


def build_maps_url(depot: str, stops: list) -> str:
    if not stops:
        return ''
    enc = urllib.parse.quote
    origin = enc(depot)
    waypoints = '/'.join(enc(s['address']) for s in stops[:-1]) if len(stops) > 1 else ''
    dest = enc(stops[-1]['address'])
    url = f'https://www.google.com/maps/dir/{origin}/'
    if waypoints:
        url += waypoints + '/'
    return url + dest


# ── Main ─────────────────────────────────────────────────────────────────────

def get_week_dates(anchor: datetime.date | None = None):
    today = anchor or datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    saturday = monday + datetime.timedelta(days=5)
    week_num = monday.isocalendar()[1]
    return monday, saturday, f'{monday.year}-W{week_num:02d}'


def main():
    # Optional --week-start YYYY-MM-DD to override anchor (script auto-rounds to Monday)
    anchor = None
    for i, arg in enumerate(sys.argv):
        if arg == '--week-start' and i + 1 < len(sys.argv):
            anchor = datetime.date.fromisoformat(sys.argv[i + 1])
            break
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET or not GOOGLE_REFRESH_TOKEN:
        print('ERROR: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and GOOGLE_REFRESH_TOKEN must be set')
        sys.exit(1)
    if not GOOGLE_MAPS_API_KEY:
        print('ERROR: GOOGLE_MAPS_API_KEY not set')
        sys.exit(1)

    monday, saturday, week_label = get_week_dates(anchor)
    print(f'Building route plan for {week_label} ({monday} – {saturday})')

    access_token = get_google_access_token(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN)

    time_min = monday.isoformat() + 'T00:00:00-06:00'  # CST
    time_max = saturday.isoformat() + 'T23:59:59-06:00'

    events = gcal_list_events(access_token, time_min, time_max)
    bookings = [
        e for e in events
        if BOOKING_TAG in (e.get('summary') or '') or BOOKING_TAG in (e.get('description') or '')
    ]
    print(f'Found {len(bookings)} bookings')

    # Group by day
    days: dict[str, list] = {}
    for e in bookings:
        day = (e.get('start', {}).get('dateTime') or e.get('start', {}).get('date', ''))[:10]
        days.setdefault(day, []).append(e)

    day_plans = []
    for day_str in sorted(days):
        stops = []
        for e in days[day_str]:
            address = e.get('location') or ''
            if not address:
                # Try description
                desc = e.get('description', '')
                m = re.search(r'Location\s*={3,}\s*\n(.+)', desc, re.IGNORECASE)
                address = m.group(1).strip() if m else ''
            if not address:
                print(f'  Skipping {e.get("summary", "?")} — no address')
                continue

            title = normalize_title(e.get('summary', ''))
            stops.append({
                'gcal_event_id': e.get('id'),
                'customer': parse_customer_name(e.get('description', '')),
                'email': parse_email(e.get('description', '')),
                'address': address,
                'start_time': e.get('start', {}).get('dateTime', ''),
                'title': title,
                'duration_min': get_duration(title),
            })

        if not stops:
            continue

        optimized = nearest_neighbor(DEPOT, stops)
        optimized = compute_optimized_times(DEPOT, optimized, day_str)
        maps_url = build_maps_url(DEPOT, optimized)

        day_plans.append({
            'date': day_str,
            'stop_count': len(optimized),
            'total_service_min': sum(s['duration_min'] for s in optimized),
            'maps_url': maps_url,
            'approved': False,
            'stops': optimized,
        })

    plan = {
        'week': week_label,
        'generated_at': datetime.datetime.utcnow().isoformat() + 'Z',
        'depot': DEPOT,
        'source': 'google-calendar',
        'days': day_plans,
    }

    total_stops = sum(d['stop_count'] for d in day_plans)
    print(f'Done. {total_stops} stops across {len(day_plans)} day(s)')
    return plan


if __name__ == '__main__':
    plan = main()
    # When run as a CLI (local testing), also write the legacy JSON file so
    # nothing existing breaks. In production this is called from
    # webhook_server.py's /cron/route-optimizer endpoint, which stores the
    # result in the shared DB instead of on disk.
    out_dir = os.path.join(os.path.dirname(__file__), '..', 'greenguard-usa-web', 'app', 'public', 'data')
    if os.path.isdir(out_dir):
        out_path = os.path.join(out_dir, f"route_plan_{plan['week']}.json")
        with open(out_path, 'w') as f:
            json.dump(plan, f, indent=2)
        print(f'wrote {out_path}')
