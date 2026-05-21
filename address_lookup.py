"""
Geocodes a service address and fetches Street View + satellite imagery for Claude Vision.
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
    street_view_jpeg: bytes | None   # ground-level view for vegetation/risk
    satellite_jpeg: bytes | None     # aerial view for lot size estimation


def _fetch_image(url: str, min_size: int = 8_000) -> bytes | None:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = resp.read()
            return data if len(data) > min_size else None
    except Exception:
        return None


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

    # Street View — ground level, vegetation/structure detail
    sv_params = urllib.parse.urlencode({
        "location": f"{lat},{lng}",
        "size": "400x300",
        "fov": "90",
        "pitch": "5",
        "key": maps_api_key,
    })
    street_view_jpeg = _fetch_image(
        f"https://maps.googleapis.com/maps/api/streetview?{sv_params}"
    )

    # Satellite — zoom 19 shows the full lot clearly for size estimation
    sat_params = urllib.parse.urlencode({
        "center": f"{lat},{lng}",
        "zoom": "19",
        "size": "400x400",
        "maptype": "satellite",
        "key": maps_api_key,
    })
    satellite_jpeg = _fetch_image(
        f"https://maps.googleapis.com/maps/api/staticmap?{sat_params}"
    )

    return PropertyInfo(
        formatted_address=formatted_address,
        lat=lat,
        lng=lng,
        neighborhood=neighborhood,
        city=city,
        state=state,
        street_view_jpeg=street_view_jpeg,
        satellite_jpeg=satellite_jpeg,
    )
