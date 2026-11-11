"""!mpath: CoreScope's observations of the request, summarised a window later."""

import copy
import json
from pathlib import Path

import pytest

from openhop_openbot.config import validate
from openhop_openbot.engine import mpath
from openhop_openbot.engine.bot import Engine, Request
from openhop_openbot.engine.templates import MAX_TEXT_BYTES
from openhop_openbot.services.corescope import CoreScope, CoreScopeError
from openhop_openbot.store import Store

DEFAULTS = json.loads((Path(__file__).parent.parent / "config.default.json").read_text())
PKT = "9ee40a871b8f697b"
OBS_A = "022592AD81013F24C5B1220F41760BCB0C592EC5FBA1FB0472B28EBD2FF1C42E"
OBS_B = "8922F28AF02A5A8B44EA26E27DBAB670B58655F897BA1C17BF48C9DBA5738FA1"


def obs(observer, path, name="Obs", snr=5.0, **kw):
    """An observation shaped like CoreScope's (path_json is a JSON string)."""
    return {"id": 1, "observer_id": observer, "observer_name": name, "observer_iata": "SAT",
            "snr": snr, "rssi": -90, "path_json": json.dumps(path), "direction": "rx",
            "timestamp": "2026-11-11T14:28:03.000Z", **kw}


def packet(*observations):
    return {"packet": {"hash": PKT, "observer_id": OBS_A, "observer_name": "Rishi",
                       "path_json": '["9370"]', "snr": 1.0},
            "path": [], "observation_count": len(observations) or 1,
            "observations": list(observations)}


class FakeGet:
    def __init__(self, status=200, data=None, exc=None):
        self.status, self.data, self.exc = status, data, exc
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append(url)
        if self.exc:
            raise self.exc
        return self.status, self.data


class Short:
    def __init__(self, link="https://v.gd/Mp1", fail=False):
        self.link, self.fail, self.calls = link, fail, []

    def shorten(self, url):
        self.calls.append(url)
        if self.fail:
            raise RuntimeError("v.gd: HTTP 406 rate limited")
        return self.link


# --------------------------------------------------------------- client ----

def test_observations_request_and_parse():
    get = FakeGet(data=packet(
        obs(OBS_A, ["9370", "BD7D", "A300"], name="Rishi", snr=-7.5),
        obs(OBS_B, ["9370", "F15D"], name="Voyager", snr=12),
    ))
    cs = CoreScope("https://cs.example/", get=get)
    out = cs.observations(PKT.upper())
    assert get.calls == ["https://cs.example/api/packets/" + PKT]
    assert out == [
        {"observer": OBS_A.lower(), "name": "Rishi", "hops": ["9370", "bd7d", "a300"],
         "snr": -7.5},
        {"observer": OBS_B.lower(), "name": "Voyager", "hops": ["9370", "f15d"], "snr": 12.0},
    ]


def test_observations_drop_own_transmissions_and_unknown_paths():
    rows = [
        obs(OBS_A, ["36", "4A"]),
        obs(OBS_A, ["36"], direction="tx"),                  # it relayed, not heard
        obs(OBS_B, [], name="Direct"),                       # heard with no relays: kept
        {**obs(OBS_B, []), "path_json": None},               # unknown path: not direct
        {**obs(OBS_B, []), "path_json": "not json"},
        obs(OBS_B, ["zz"]),
        obs(OBS_B, [1, 2]),
        obs(None, ["36"], name=None),                        # nobody
        obs(None, ["4a"], name="NameOnly"),                  # no id: keyed by name
        "not-a-dict",
    ]
    out = CoreScope("https://cs.example", get=FakeGet(data=packet(*rows))).observations(PKT)
    assert [(o["observer"], o["hops"]) for o in out] == [
        (OBS_A.lower(), ["36", "4a"]), (OBS_B.lower(), []), ("NameOnly", ["4a"])]


def test_observations_404_means_never_heard():
    assert CoreScope("https://cs.example",
                     get=FakeGet(404, {"error": "Not found"})).observations(PKT) is None


def test_observations_fall_back_to_the_packet_row():
    out = CoreScope("https://cs.example", get=FakeGet(data=packet())).observations(PKT)
    assert out == [{"observer": OBS_A.lower(), "name": "Rishi", "hops": ["9370"], "snr": 1.0}]


@pytest.mark.parametrize("get", [
    FakeGet(500, {"error": "db locked"}),
    FakeGet(200, ["not", "an", "object"]),
    FakeGet(200, {"observations": []}),                    # no packet
    FakeGet(exc=TimeoutError("timed out")),
])
def test_observations_failures_raise_corescope_error(get):
    with pytest.raises(CoreScopeError):
        CoreScope("https://cs.example", get=get).observations(PKT)


@pytest.mark.parametrize("bad", ["", "9ee40a871b8f697", "../../api/admin", None])
def test_observations_reject_bad_hash_before_any_request(bad):
    get = FakeGet(data=packet())
    with pytest.raises(ValueError):
        CoreScope("https://cs.example", get=get).observations(bad)
    assert not get.calls


def test_packet_url():
    assert CoreScope("https://cs.example/").packet_url(PKT) == \
        "https://cs.example/#/packet/9EE40A871B8F697B"


# -------------------------------------------------------------- summary ----

def o(observer, *hops):
    return {"observer": observer, "name": None, "hops": list(hops), "snr": None}


def test_summary_counts_distinct_routes_per_observer():
    rows = [o("a", "36", "4a"), o("a", "36", "4a"),       # same copy twice: one route
            o("a", "36", "e9", "18"), o("b", "36"), o("c")]
    assert mpath.summary(rows) == "4 routes to 3 observers, 0-3 hops, 1 direct"


@pytest.mark.parametrize("rows,want", [
    ([o("a", "36", "4a")], "1 route to 1 observer, 2 hops"),
    ([o("a")], "1 route to 1 observer, 0 hops, 1 direct"),
    ([], None),
])
def test_summary_edges(rows, want):
    assert mpath.summary(rows) == want


class FakeCS:
    base = "https://cs.example"

    def __init__(self, rows=None, exc=None):
        self.rows, self.exc = rows, exc

    def observations(self, h):
        if self.exc:
            raise self.exc
        return self.rows

    def packet_url(self, h):
        return CoreScope(self.base).packet_url(h)


ROWS = [o("a", "36", "4a"), o("b", "36"), o("b", "e9")]


def test_report_with_short_link():
    short = Short()
    out = mpath.report(FakeCS(ROWS), short, "bob", PKT)
    assert out == "@[bob] 3 routes to 2 observers, 1-2 hops https://v.gd/Mp1"
    assert short.calls == ["https://cs.example/#/packet/9EE40A871B8F697B"]


def test_report_falls_back_to_long_link_then_to_none():
    out = mpath.report(FakeCS(ROWS), Short(fail=True), "bob", PKT)
    assert out.endswith(" https://cs.example/#/packet/9EE40A871B8F697B")
    cs = FakeCS(ROWS)
    cs.base = "https://" + "x" * 120 + ".example"
    out = mpath.report(cs, None, "bob", PKT)
    assert out == "@[bob] 3 routes to 2 observers, 1-2 hops"     # a link is never cut


@pytest.mark.parametrize("cs,want", [
    (FakeCS(None), "@[bob] no CoreScope observer reported hearing that"),
    (FakeCS([]), "@[bob] no CoreScope observer reported hearing that"),
    (FakeCS(exc=CoreScopeError("HTTP 500")), "@[bob] CoreScope lookup failed, try again later"),
])
def test_report_without_routes(cs, want):
    assert mpath.report(cs, Short(), "bob", PKT) == want


def test_report_fits_one_frame_with_a_long_name():
    rows = [o("obs%d" % i, *["%02x" % j for j in range(i % 9)]) for i in range(400)]
    out = mpath.report(FakeCS(rows), Short(), "N" * 32, PKT)
    assert len(out.encode()) <= MAX_TEXT_BYTES and out.endswith("https://v.gd/Mp1")


@pytest.mark.parametrize("v,want", [
    (None, 30), (45, 45), ("60", 60), (5, 5), (300, 300), (4, 30), (301, 30),
    ("soon", 30), (True, 30),
])
def test_wait_s(v, want):
    assert mpath.wait_s({"corescope": {"mpath_wait_s": v}}) == want


@pytest.mark.parametrize("v,problem", [(30, False), (None, False), (2, True), ("x", True)])
def test_wait_s_config_problem(v, problem):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["corescope"]["mpath_wait_s"] = v
    _, problems = validate(cfg)
    assert any("mpath_wait_s" in p for p in problems) is problem


# --------------------------------------------------------------- engine ----

def make(corescope=None, **cs):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["limits"]["user_cooldown_s"] = 0
    cfg["corescope"].update(cs)
    cfg, problems = validate(cfg)
    assert not problems
    return Engine(cfg, Store(":memory:"), corescope=corescope, shortener=Short(),
                  node={"name": "openbot"})


def req(text="!mpath", **kw):
    base = dict(sender="bob", text=text, channel="#bot", channel_idx=1, region="*",
                hops_n=2, hops=["a1b2", "c3d4"], hash_size=2, packet_hash=PKT)
    base.update(kw)
    return Request(**base)


def test_mpath_is_silent_without_corescope():
    res = make().handle(req())
    assert res.matched == "mpath" and not res.reply and not res.later
    assert res.reason == "reply rendered empty"


def test_mpath_acks_and_queues_the_lookup():
    res = make(FakeCS(ROWS), mpath_wait_s=45).handle(req())
    assert res.reply == "@[bob] asking CoreScope's observers, back in 45s"
    assert len(res.later) == 1
    after_s, fn = res.later[0]
    assert after_s == 45
    assert fn() == "@[bob] 3 routes to 2 observers, 1-2 hops https://v.gd/Mp1"


def test_mpath_without_a_packet_hash():
    res = make(FakeCS(ROWS)).handle(req(packet_hash=None))
    assert res.reply == "@[bob] can't trace that copy: no packet hash" and not res.later


def test_mpath_queues_one_lookup_per_request():
    eng = make(FakeCS(ROWS))
    eng.cfg["commands"].append({"id": "twice", "trigger": "!twice", "reply": "{mpath} {mpath}",
                                "cooldown_s": 0})
    assert len(eng.handle(req("!twice")).later) == 1


def test_help_lists_mpath_only_with_corescope():
    assert "!mpath" not in make().handle(req("!help")).reply
    assert "!mpath" in make(FakeCS(ROWS)).handle(req("!help")).reply
