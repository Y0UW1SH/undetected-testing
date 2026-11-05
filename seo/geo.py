"""Location helpers: uule encoding, geocoding, and rank-grid geometry."""
import base64
import json
import math
import urllib.parse
import urllib.request

# Google reads the search location from the `uule` URL parameter. It is
# "w+" followed by the base64 of a tiny protobuf message:
#   08 02 10 20 22 <len> <canonical name bytes>
# which is why every value starts with "w+CAIQICI" and the next character
# encodes the name length (e.g. "K" for the 10-byte "California").
_PREFIX = b"\x08\x02\x10\x20\x22"

NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "undetected-seo/0.1 (SeleniumBase research project)"


def uule(canonical_name):
    """Encode a canonical location name ("Austin,Texas,United States")
    into Google's uule parameter."""
    raw = canonical_name.strip().encode("utf-8")
    if not raw:
        raise ValueError("canonical_name must not be empty")
    if len(raw) > 127:
        raise ValueError("canonical_name is too long for uule encoding")
    payload = _PREFIX + bytes([len(raw)]) + raw
    return "w+" + base64.b64encode(payload).decode("ascii").rstrip("=")


def canonical_name(city, region=None, country="United States"):
    """Build the comma-separated name Google expects, e.g.
    canonical_name("Austin", "Texas") -> "Austin,Texas,United States"."""
    parts = [city.strip()]
    if region:
        parts.append(region.strip())
    parts.append(country.strip())
    return ",".join(parts)


def canonical_from_address(address):
    """Google's canonical location name ("Austin,Texas,United States")
    from a Nominatim address dict, or None if it lacks a city/country."""
    city = None
    for key in ("city", "town", "village", "municipality", "county"):
        if address.get(key):
            city = address[key]
            break
    region = (address.get("state") or address.get("region")
              or address.get("province"))
    country = address.get("country")
    if not city or not country:
        return None
    parts = [city]
    if region and region != city:
        parts.append(region)
    parts.append(country)
    return ",".join(p.strip() for p in parts)


def geocode(place, opener=None):
    """Resolve a free-text place with the OpenStreetMap Nominatim API.
    Returns (lat, lng, display_name, canonical_name); canonical_name may
    be None. `opener` is injectable for tests."""
    params = urllib.parse.urlencode(
        {"q": place, "format": "json", "limit": 1, "addressdetails": 1}
    )
    req = urllib.request.Request(
        "%s?%s" % (NOMINATIM, params), headers={"User-Agent": USER_AGENT}
    )
    opener = opener or urllib.request.urlopen
    with opener(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data:
        raise LookupError("No geocoding result for %r" % place)
    hit = data[0]
    canonical = canonical_from_address(hit.get("address") or {})
    return (float(hit["lat"]), float(hit["lon"]), hit.get("display_name"),
            canonical)


def offset(lat, lng, north_km, east_km):
    """Move a lat/lng point by kilometres (flat-earth approximation,
    accurate to well under 1% at city scale)."""
    d_lat = north_km / 110.574
    d_lng = east_km / (111.320 * math.cos(math.radians(lat)))
    return round(lat + d_lat, 6), round(lng + d_lng, 6)


def grid(center_lat, center_lng, size=5, spacing_km=1.0):
    """Return a list of (row, col, lat, lng) for a size x size grid centred
    on the given point. Row 0 is the northernmost row."""
    if size < 1 or size % 2 == 0:
        raise ValueError("size must be a positive odd number")
    half = size // 2
    cells = []
    for row in range(size):
        for col in range(size):
            north = (half - row) * spacing_km
            east = (col - half) * spacing_km
            lat, lng = offset(center_lat, center_lng, north, east)
            cells.append((row, col, lat, lng))
    return cells
