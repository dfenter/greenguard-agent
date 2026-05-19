"""
Geocodes a service address and fetches a Street View image for Claude Vision analysis.
"""

import urllib.request
import urllib.parse
from dataclasses import dataclass

import googlemaps

_gmaps_cache: dict[str, googlemaps.Client] = {}


def _gmaps(api_key: str) -> googlemaps.Client:
    if api_key not in _gmaps_cache:
        _gmaps_cache[api_key] = googlemaps.Client(key=api_key)
    return _gmaps_cache[api_key]


@dataclass
class PropertyInfo:
    formatted_address: str
    lat: float
    lng: float
    neighborhood: str
    city: str
    state: str
    street_view_jpeg: bytes | None  # raw JPEG bytes; None if imagery unavailable


def lookup_property(address: str, maps_api_key: str) -> PropertyInfo:
    gmaps = _gmaps(maps_api_key)

    results = gmaps.geocode(address)
    if not results:
        raise ValueError(f"Could not geocode: {address!r}")
    top = results[0]
    loc = top["geometry"]["location"]
    lat, lng = loc["lat"], loc["lng"]

    components = {c["types"][0]: c["long_name"] for c in top["address_components"]}
    neighborhood = components.get("neighborhood", components.get("sublocality", ""))
    city = components.get("locality", "")
    state = components.get("administrative_area_level_1", "")
    formatted_address = top["formatted_address"]

    # Street View — 400×300 keeps image tokens low while giving Claude enough detail
    street_view_jpeg: bytes | None = None
    params = urllib.parse.urlencode({
        "location": f"{lat},{lng}",
        "size": "400x300",
        "fov": "90",
        "pitch": "5",
        "key": maps_api_key,
    })
    try:
        with urllib.request.urlopen(
            f"https://maps.googleapis.com/maps/api/streetview?{params}", timeout=10
        ) as resp:
            data = resp.read()
            # Google returns a grey placeholder (~5 KB) when no imagery exists
            if len(data) > 8_000:
                street_view_jpeg = data
    except Exception:
        pass

    return PropertyInfo(
        formatted_address=formatted_address,
        lat=lat,
        lng=lng,
        neighborhood=neighborhood,
        city=city,
        state=state,
        street_view_jpeg=street_view_jpeg,
    )
