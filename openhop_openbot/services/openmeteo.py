"""Open-Meteo forecasts for `!wx`, sized for one LoRa frame.

Open-Meteo (https://open-meteo.com) is free for non-commercial use, needs no
key, and covers the whole world. Its data is CC BY 4.0; the attribution is in
the README. One request per forecast: current conditions plus three days,
in the forecast location's own time zone (``timezone=auto``), so "Tue"
means Tuesday where the place is, not where the node is.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from datetime import date
from typing import Callable, Dict, Optional, Tuple

log = logging.getLogger("openbot.openmeteo")

API = "https://api.open-meteo.com/v1/forecast"
DEFAULT_USER_AGENT = "openhop-openbot (https://github.com/zfouts/openhop-openbot-plugin)"
TIMEOUT_S = 6.0
CACHE_S = 600.0
CACHE_MAX = 256

# Countries that use Fahrenheit and mph; everyone else gets metric with "auto".
IMPERIAL_COUNTRIES = frozenset({"us", "lr", "mm", "pr", "gu", "vi", "as", "mp", "bs", "ky", "pw",
                                "fm", "mh"})

# WMO weather interpretation codes, as short as they can be and still read.
WMO = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy",
    45: "Fog", 48: "Icy fog",
    51: "Lt drizzle", 53: "Drizzle", 55: "Hvy drizzle",
    56: "Frz drizzle", 57: "Frz drizzle",
    61: "Lt rain", 63: "Rain", 65: "Hvy rain", 66: "Frz rain", 67: "Frz rain",
    71: "Lt snow", 73: "Snow", 75: "Hvy snow", 77: "Snow grains",
    80: "Lt showers", 81: "Showers", 82: "Hvy showers",
    85: "Snow showers", 86: "Hvy snow showers",
    95: "T-storms", 96: "T-storms, hail", 99: "T-storms, hail",
}

Fetch = Callable[[str, float], dict]


def truncate_bytes(text: str, limit: int) -> str:
    b = text.encode("utf-8")
    if len(b) <= limit:
        return text
    return b[:max(0, limit)].decode("utf-8", errors="ignore")


def pick_units(setting: str, country: Optional[str], fallback: str = "metric") -> str:
    """'imperial' or 'metric'. 'auto' follows the forecast location's country."""
    setting = (setting or "auto").lower()
    if setting in ("imperial", "metric"):
        return setting
    if country:
        return "imperial" if country.lower() in IMPERIAL_COUNTRIES else "metric"
    return fallback if fallback in ("imperial", "metric") else "metric"


def _default_fetch(user_agent: str) -> Fetch:
    def fetch(url: str, timeout: float = TIMEOUT_S) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": user_agent,
                                                   "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    return fetch


def _num(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) else None


class OpenMeteo:
    def __init__(self, user_agent: str = "", fetch: Optional[Fetch] = None,
                 cache_s: float = CACHE_S):
        self.user_agent = (user_agent or "").strip() or DEFAULT_USER_AGENT
        self._fetch = fetch or _default_fetch(self.user_agent)
        self.cache_s = cache_s
        self._cache: Dict[str, Tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def forecast(self, lat: float, lon: float, units: str = "metric") -> dict:
        imperial = units == "imperial"
        params = {
            "latitude": "%.3f" % lat, "longitude": "%.3f" % lon,
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max",
            "timezone": "auto", "forecast_days": 3,
            "temperature_unit": "fahrenheit" if imperial else "celsius",
            "wind_speed_unit": "mph" if imperial else "kmh",
        }
        url = API + "?" + urllib.parse.urlencode(params)
        now = time.time()
        with self._lock:
            hit = self._cache.get(url)
            if hit and now - hit[0] < self.cache_s:
                return hit[1]
        data = self._fetch(url, TIMEOUT_S)
        if not isinstance(data, dict) or data.get("error"):
            raise ValueError(str((data or {}).get("reason") or "bad response"))
        with self._lock:
            self._cache[url] = (now, data)
            if len(self._cache) > CACHE_MAX:
                self._cache.pop(min(self._cache, key=lambda k: self._cache[k][0]), None)
        return data

    def forecast_text(self, lat: float, lon: float, *, units: str = "metric",
                      limit_bytes: int = 160) -> str:
        """'72F Cloudy, wind 8mph | Today 77/58F Showers 40% | Tue 80/60F Clear'.

        Never raises: a failure is a short apology, not a dropped reply.
        """
        try:
            data = self.forecast(lat, lon, units)
        except Exception as exc:
            log.warning("open-meteo failed: %s", exc)
            return truncate_bytes("weather unavailable, try later", limit_bytes)
        deg = "F" if units == "imperial" else "C"
        wind_u = "mph" if units == "imperial" else "km/h"
        parts = []
        cur = data.get("current") or {}
        t = _num(cur.get("temperature_2m"))
        if t is not None:
            s = ("%d%s %s" % (round(t), deg, WMO.get(cur.get("weather_code"), ""))).rstrip()
            w = _num(cur.get("wind_speed_10m"))
            if w is not None:
                s += ", wind %d%s" % (round(w), wind_u)
            parts.append(s)
        daily = data.get("daily") or {}
        days = daily.get("time") or []
        for i, day in enumerate(days[:3]):
            try:
                name = "Today" if i == 0 else date.fromisoformat(day).strftime("%a")
            except (TypeError, ValueError):
                name = "?"
            hi = _num((daily.get("temperature_2m_max") or [None] * 3)[i])
            lo = _num((daily.get("temperature_2m_min") or [None] * 3)[i])
            code = (daily.get("weather_code") or [None] * 3)[i]
            pop = _num((daily.get("precipitation_probability_max") or [None] * 3)[i])
            s = name
            if hi is not None and lo is not None:
                s += " %d/%d%s" % (round(hi), round(lo), deg)
            if code in WMO:
                s += " " + WMO[code]
            if pop:
                s += " %d%%" % round(pop)
            parts.append(s)
        if not parts:
            return truncate_bytes("no forecast available", limit_bytes)
        text = parts[0]
        for p in parts[1:]:
            if len((text + " | " + p).encode()) > limit_bytes:
                break
            text += " | " + p
        return truncate_bytes(text, limit_bytes)
