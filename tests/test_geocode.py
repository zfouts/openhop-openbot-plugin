import time
from urllib.parse import parse_qs, urlparse

from openhop_openbot.engine.builtins import fn_forecast
from openhop_openbot.services.geocode import Geocoder, Place
from openhop_openbot.services.openmeteo import OpenMeteo, pick_units


def hit(lat, lon, cc, city, iso=None, **addr):
    a = {"city": city, "country_code": cc, **addr}
    if iso:
        a["ISO3166-2-lvl4"] = iso
    return [{"lat": str(lat), "lon": str(lon), "name": city, "address": a}]


FORT_WORTH = hit(32.88, -97.35, "us", "Fort Worth", "US-TX")
KARLSRUHE = hit(49.01, 8.40, "de", "Karlsruhe", "DE-BW")


class FakeNominatim:
    """Answers by query parameters, like Nominatim would."""

    def __init__(self, table=None):
        self.table = table if table is not None else {}
        self.calls = []

    def __call__(self, url, timeout):
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        self.calls.append((time.monotonic(), q))
        search = {k: v for k, v in q.items() if k in ("q", "postalcode", "countrycodes")}
        for match, result in self.table.items():
            if dict(match) == search:
                return result
        return []


def geo(**kw):
    table = {
        (("postalcode", "76131"), ("countrycodes", "us")): FORT_WORTH,
        (("postalcode", "76131"),): KARLSRUHE,
        (("q", "Karlsruhe"),): KARLSRUHE,
        (("q", "76131 DE"),): KARLSRUHE,
        (("q", "Fort Worth, TX"),): FORT_WORTH,
    }
    fake = FakeNominatim(table)
    return Geocoder("t", fetch=fake, **kw), fake


def test_postal_code_prefers_default_country():
    g, fake = geo(default_country="US")
    p = g.lookup("76131")
    assert p.label == "Fort Worth, TX" and p.country == "us"
    assert fake.calls[0][1]["countrycodes"] == "us"
    assert g.lookup("76131-4321") == p                  # ZIP+4, same cached search


def test_postal_code_worldwide_without_default_or_on_miss():
    g, _ = geo()
    assert g.lookup("76131").label == "Karlsruhe, DE"
    g, fake = geo(default_country="fr")                 # no 76131 in France here
    assert g.lookup("76131").label == "Karlsruhe, DE"
    assert [c[1].get("countrycodes") for c in fake.calls] == ["fr", None]


def test_free_text_and_country_hint():
    g, _ = geo(default_country="us")
    assert g.lookup("Karlsruhe").label == "Karlsruhe, DE"
    assert g.lookup("76131 DE").label == "Karlsruhe, DE"
    assert g.lookup("Fort  Worth,   TX").label == "Fort Worth, TX"


def test_latlon_passthrough_needs_no_network():
    g, fake = geo()
    assert g.lookup("32.9, -97.3") == Place(32.9, -97.3, "32.90,-97.30")
    assert g.lookup("95, 10") is None
    assert not fake.calls


def test_label_fallbacks():
    g = Geocoder(fetch=lambda u, t: [{"lat": "51.5", "lon": "-0.14", "name": "",
                                      "address": {"suburb": "Westminster",
                                                  "country_code": "gb"}}])
    assert g.lookup("SW1A 1AA").label == "Westminster, GB"


def test_not_found_is_cached_and_rate_limited():
    fake = FakeNominatim({})
    g = Geocoder(fetch=fake)
    assert g.lookup("nowhereville") is None
    assert g.lookup("nowhereville") is None
    assert len(fake.calls) == 1
    g.lookup("elsewhere")
    assert fake.calls[1][0] - fake.calls[0][0] >= 1.0   # Nominatim: max 1 req/s


# ------------------------------------------------------------- open-meteo --

OM = {
    "current": {"temperature_2m": 64.4, "weather_code": 3, "wind_speed_10m": 3.2},
    "daily": {"time": ["2026-11-09", "2026-11-10", "2026-11-11"],
              "weather_code": [61, 0, 95],
              "temperature_2m_max": [69.1, 74, 79.6], "temperature_2m_min": [53, 58, 55],
              "precipitation_probability_max": [40, 0, 80]},
}


def test_forecast_text_format_and_units_in_request():
    urls = []

    def fetch(url, timeout):
        urls.append(url)
        return OM

    om = OpenMeteo(fetch=fetch)
    out = om.forecast_text(32.88, -97.35, units="imperial")
    assert out == ("64F Cloudy, wind 3mph | Today 69/53F Lt rain 40% | Tue 74/58F Clear"
                   " | Wed 80/55F T-storms 80%")
    q = parse_qs(urlparse(urls[0]).query)
    assert q["temperature_unit"] == ["fahrenheit"] and q["timezone"] == ["auto"]
    om.forecast_text(32.88, -97.35, units="imperial")
    assert len(urls) == 1                               # cached
    assert "C " in OpenMeteo(fetch=fetch).forecast_text(1, 2, units="metric")


def test_forecast_text_fits_and_never_raises():
    om = OpenMeteo(fetch=lambda u, t: OM)
    assert len(om.forecast_text(1, 2, limit_bytes=40).encode()) <= 40

    def boom(u, t):
        raise OSError("down")

    assert OpenMeteo(fetch=boom).forecast_text(1, 2) == "weather unavailable, try later"
    assert OpenMeteo(fetch=lambda u, t: {"error": True, "reason": "x"}) \
        .forecast_text(1, 2) == "weather unavailable, try later"
    assert OpenMeteo(fetch=lambda u, t: {"current": {"temperature_2m": None}}) \
        .forecast_text(1, 2) == "no forecast available"


def test_pick_units():
    assert pick_units("auto", "us") == "imperial"
    assert pick_units("auto", "de") == "metric"
    assert pick_units("metric", "us") == "metric"
    assert pick_units("auto", None, "imperial") == "imperial"


# ---------------------------------------------------------------- {forecast}

def ctx(args, **kw):
    g, _ = geo(default_country="us")
    base = {"weather": OpenMeteo(fetch=lambda u, t: OM), "geocoder": g, "args": args,
            "cfg": {"weather": {"units": "auto"}}, "units": "imperial"}
    base.update(kw)
    return base


def test_wx_answers_for_the_requested_place():
    assert fn_forecast(ctx("76131"), None).startswith("Fort Worth, TX: 64F Cloudy")
    out = fn_forecast(ctx("Karlsruhe"), None)
    assert out.startswith("Karlsruhe, DE: ") and len(out.encode()) <= 160


def test_wx_usage_not_found_and_lookup_errors():
    assert fn_forecast(ctx(""), None) == "wx <zip, city or place>"
    assert fn_forecast(ctx("zzz"), None) == "couldn't find 'zzz'"

    def boom(url, timeout):
        raise OSError("down")

    assert "unavailable" in fn_forecast(ctx("Waco", geocoder=Geocoder(fetch=boom)), None)
