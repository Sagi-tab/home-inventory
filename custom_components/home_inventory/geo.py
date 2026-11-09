"""Pure geo helpers for location reminders: distances and Google Maps links.

Kept free of Home Assistant imports so the parsing can be tested on its own.
"""
from __future__ import annotations

import math
import re
from urllib.parse import parse_qs, unquote, urlparse

_EARTH_RADIUS_M = 6_371_000

# Anything that looks like a Google Maps link, long or short.
_MAPS_URL_RE = re.compile(
    r"https?://(?:"
    r"maps\.app\.goo\.gl/\S+"
    r"|goo\.gl/maps/\S+"
    r"|(?:www\.)?google\.[a-z.]+/maps\S*"
    r"|maps\.google\.[a-z.]+/\S*"
    r")",
    re.IGNORECASE,
)

# Short links redirect to a full URL before they carry any coordinates.
SHORT_LINK_HOSTS = ("maps.app.goo.gl", "goo.gl")

_NUM = r"(-?\d{1,3}(?:\.\d+)?)"
# "!3d<lat>!4d<lon>" is the place itself; "@<lat>,<lon>" is only the map
# viewport, so it is the last resort.
_PLACE_RE = re.compile(rf"!3d{_NUM}!4d{_NUM}")
_PAIR_RE = re.compile(rf"^\s*{_NUM}\s*,\s*\+?{_NUM}\s*$")
_PATH_PAIR_RE = re.compile(rf"/(?:place|search|dir/[^/]*)/{_NUM},\+?{_NUM}")
_VIEWPORT_RE = re.compile(rf"@{_NUM},{_NUM}")


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def valid_coords(lat: float, lon: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0)


def find_maps_urls(text: str) -> list[str]:
    """Google Maps links in a message, trailing punctuation stripped."""
    return [m.group(0).rstrip(").,!?'\"") for m in _MAPS_URL_RE.finditer(text or "")]


def is_short_link(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in SHORT_LINK_HOSTS


def coords_from_url(url: str) -> tuple[float, float] | None:
    """Coordinates of the place a (long) Google Maps URL points at, if it has any."""
    url = unquote(url or "")
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    # A consent/redirect page wraps the real URL in ?continue=.
    for key in ("continue", "link"):
        if inner := query.get(key):
            if found := coords_from_url(inner[0]):
                return found

    candidates: list[tuple[str, str]] = []
    if m := _PLACE_RE.search(url):
        candidates.append(m.groups())
    for key in ("q", "query", "ll", "destination", "center", "daddr"):
        for value in query.get(key, []):
            if m := _PAIR_RE.match(value):
                candidates.append(m.groups())
    if m := _PATH_PAIR_RE.search(parsed.path):
        candidates.append(m.groups())
    if m := _VIEWPORT_RE.search(url):
        candidates.append(m.groups())

    for lat_s, lon_s in candidates:
        lat, lon = float(lat_s), float(lon_s)
        if valid_coords(lat, lon):
            return lat, lon
    return None
