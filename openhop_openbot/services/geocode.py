"""Turn what a mesh user typed into a point: `!wx 76131`, `!wx Karlsruhe`.

Everything goes through OpenStreetMap Nominatim, worldwide:

* A bare postal code (``76131``, ``76131-1234``) is ambiguous across
  countries (76131 is both Fort Worth and Karlsruhe), so it is searched as a
  postal code in ``default_country`` first, when one is configured, and
  worldwide only if that finds nothing. ``!wx 76131 DE`` or ``!wx Karlsruhe``
  reaches the other one.
* ``lat,lon`` is taken as given, with no network call.
* Anything else is a free-text search: ``Fort Worth, TX``, ``Paris``,
  ``Wellington NZ``.

Nominatim's usage policy asks for an identifying User-Agent, at most one
request per second, and caching; all three are enforced here. Answers,
including "not found", are cached for a long time, so a popular
``!wx Dallas`` costs one lookup.

Lookups block (urllib) and are meant to run on the engine's worker thread.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

log = logging.getLogger("openbot.geocode")

NOMINATIM = "https://nominatim.openstreetmap.org/search"
DEFAULT_USER_AGENT = "openhop-openbot (https://github.com/zfouts/openhop-openbot-plugin)"
TIMEOUT_S = 6.0
MIN_INTERVAL_S = 1.1           # Nominatim policy: max 1 request/second
CACHE_TTL_S = 30 * 86400.0
CACHE_MAX = 512
MAX_QUERY = 64

_POSTAL_RE = re.compile(r"^\d{4,6}(?:-\d{4})?$")
_LATLON_RE = re.compile(r"^(-?\d{1,2}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)$")
_PLACE_KEYS = ("city", "town", "village", "hamlet", "municipality", "suburb",
               "city_district", "county")

Fetch = Callable[[str, float], object]


@dataclass(frozen=True)
class Place:
    lat: float
    lon: float
    label: str
    country: Optional[str] = None      # ISO 3166-1 alpha-2, lowercase


def _default_fetch(user_agent: str) -> Fetch:
    def fetch(url: str, timeout: float = TIMEOUT_S):
        req = urllib.request.Request(url, headers={
            "User-Agent": user_agent, "Accept": "application/json",
            "Accept-Language": "en"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    return fetch


def _label(hit: dict, fallback: str) -> str:
    """Short, recognisable: 'Fort Worth, TX' in the US, 'Karlsruhe, DE' elsewhere."""
    addr = hit.get("address") or {}
    place = next((addr[k] for k in _PLACE_KEYS if addr.get(k)), None)
    cc = str(addr.get("country_code") or "").upper()
    iso = str(addr.get("ISO3166-2-lvl4") or "")
    region = iso[3:] if cc == "US" and iso.startswith("US-") else cc
    if not place:
        place = addr.get("postcode") or hit.get("name") or fallback
    return "%s, %s" % (place, region) if region else str(place)


class Geocoder:
    def __init__(self, user_agent: str = "", fetch: Optional[Fetch] = None,
                 default_country: str = ""):
        self.user_agent = (user_agent or "").strip() or DEFAULT_USER_AGENT
        self.default_country = (default_country or "").strip().lower()[:2]
        self._fetch = fetch or _default_fetch(self.user_agent)
        self._cache: Dict[str, Tuple[float, Optional[Place]]] = {}
        self._lock = threading.Lock()          # cache
        self._net_lock = threading.Lock()      # one Nominatim request at a time
        self._last = 0.0

    def lookup(self, query: str) -> Optional[Place]:
        """A Place, or None when nothing matched. Network errors raise."""
        q = " ".join((query or "").split())[:MAX_QUERY]
        if not q:
            return None
        m = _LATLON_RE.match(q)
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return Place(lat, lon, "%.2f,%.2f" % (lat, lon))
            return None
        if _POSTAL_RE.match(q):
            code = q.split("-")[0]
            if self.default_country:
                hit = self._search({"postalcode": code, "countrycodes": self.default_country})
                if hit:
                    return hit
            return self._search({"postalcode": code}) or self._search({"q": q})
        return self._search({"q": q})

    def _search(self, params: Dict[str, str]) -> Optional[Place]:
        key = json.dumps(sorted((k, v.lower()) for k, v in params.items()))
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < CACHE_TTL_S:
                return hit[1]
        url = NOMINATIM + "?" + urllib.parse.urlencode(
            {**params, "format": "jsonv2", "limit": 1, "addressdetails": 1})
        with self._net_lock:
            wait = MIN_INTERVAL_S - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                data = self._fetch(url, TIMEOUT_S)
            finally:
                self._last = time.monotonic()
        place = None
        if isinstance(data, list) and data:
            h = data[0]
            try:
                cc = str((h.get("address") or {}).get("country_code") or "").lower() or None
                place = Place(float(h["lat"]), float(h["lon"]),
                              _label(h, params.get("q") or params.get("postalcode", "")), cc)
            except (KeyError, TypeError, ValueError):
                place = None
        with self._lock:
            self._cache[key] = (now, place)
            if len(self._cache) > CACHE_MAX:
                oldest = min(self._cache, key=lambda k: self._cache[k][0])
                self._cache.pop(oldest, None)
        return place
