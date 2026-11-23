import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from openhop_openbot.engine.builtins import fn_pathnames
from openhop_openbot.services.corescope import CoreScope, CoreScopeError, _default_get
from openhop_openbot.store import Store

PK_7DA9 = "7da93d0b4e22545dbb6d2ed1ca26fed63f6f37c7054e727b180b339cc6499304"
PK_AB_FAR = "ab095acb51bbf808e7cb15c81393622fec64309b7c7728da3262308ee11b2701"
PK_AB_NEAR = "ab12" + "1" * 60
PK_AB12_FAR = "ab12" + "9" * 60


def cand(pubkey, name, lat=None, lon=None):
    return {"name": name, "pubkey": pubkey, "lat": lat, "lon": lon, "affinityScore": None}


def payload(**hops):
    """A /api/resolve-hops body shaped like CoreScope's: hash -> its candidates."""
    return {"resolved": {h: {"name": cs[0]["name"] if cs else None,
                             "pubkey": cs[0]["pubkey"] if cs else None,
                             "candidates": cs, "conflicts": [],
                             "confidence": "unique_prefix" if len(cs) == 1 else "ambiguous"}
                         for h, cs in hops.items()}}


class FakeGet:
    def __init__(self, status=200, data=None):
        self.status, self.data = status, data
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers, timeout))
        return self.status, self.data


# ------------------------------------------------------------------ client --

def test_resolve_hops_request_and_parsed_candidates():
    get = FakeGet(data=payload(**{"7da9": [cand(PK_7DA9, "HOG-Repeater", 32.850299, -97.40788)]}))
    cs = CoreScope("https://analyzer.example.net/", user_agent="ua", get=get)
    out = cs.resolve_hops(["7DA9"])
    url, headers, timeout = get.calls[0]
    assert url == "https://analyzer.example.net/api/resolve-hops?hops=7da9"
    assert headers["User-Agent"] == "ua" and timeout <= 5
    assert out == {"7da9": [{"pubkey": PK_7DA9, "name": "HOG-Repeater", "type": None,
                             "lat": 32.850299, "lon": -97.40788, "last_heard": None}]}


def test_resolve_hops_sends_whole_chain_in_one_request():
    get = FakeGet(data=payload(**{"90e2": [], "fe27": []}))
    cs = CoreScope("http://corescope.lan:8080", get=get)
    assert cs.resolve_hops(["90e2", "fe27"]) == {"90e2": [], "fe27": []}
    assert [c[0] for c in get.calls] == ["http://corescope.lan:8080/api/resolve-hops?hops=90e2,fe27"]


def test_malformed_candidates_are_dropped_or_cleaned():
    rows = [
        cand(PK_AB_NEAR, "Good", 30.6, -97.8),
        cand("cd" + "0" * 62, "WrongPrefix", 30.0, -97.0),     # not this hop
        cand("ab-not-hex", "BadKey", 30.0, -97.0),
        cand(None, "NoKey"),
        cand(PK_AB_FAR, "x" * 80, 0, 0),                       # 0,0 = no fix; long name
        cand("ab" + "2" * 62, 12345, 91.0, -97.0),             # non-str name, lat off-globe
        cand("ab" + "3" * 62, "Bools", True, False),
        "not-a-dict",
    ]
    out = CoreScope("https://cs.example", get=FakeGet(data=payload(ab=rows))).resolve_hops(["ab"])
    got = {c["pubkey"]: c for c in out["ab"]}
    assert set(got) == {PK_AB_NEAR, PK_AB_FAR, "ab" + "2" * 62, "ab" + "3" * 62}
    assert got[PK_AB_NEAR]["lat"] == 30.6 and got[PK_AB_NEAR]["name"] == "Good"
    assert got[PK_AB_FAR]["lat"] is None and len(got[PK_AB_FAR]["name"]) == 32
    assert got["ab" + "2" * 62]["name"] is None and got["ab" + "2" * 62]["lat"] is None
    assert got["ab" + "3" * 62]["lat"] is None


def test_hash_missing_from_response_has_no_candidates():
    cs = CoreScope("https://cs.example", get=FakeGet(data={"resolved": {}}))
    assert cs.resolve_hops(["90e2"]) == {"90e2": []}


@pytest.mark.parametrize("status,data", [
    (503, {"error": "down"}), (404, None), (200, []), (200, {"resolved": []}), (200, None),
])
def test_bad_responses_raise(status, data):
    with pytest.raises(CoreScopeError):
        CoreScope("https://cs.example", get=FakeGet(status, data)).resolve_hops(["90e2"])


def test_network_errors_become_corescope_errors():
    def boom(url, headers, timeout):
        raise OSError("connection refused")
    with pytest.raises(CoreScopeError):
        CoreScope("https://cs.example", get=boom).resolve_hops(["90e2"])


@pytest.mark.parametrize("hops", [[], ["zz"], ["90e2", "fe"], ["90e2" * 4], ["90e2,fe27"]])
def test_bad_hops_rejected_before_any_request(hops):
    get = FakeGet(data={"resolved": {}})
    with pytest.raises(ValueError):
        CoreScope("https://cs.example", get=get).resolve_hops(hops)
    assert not get.calls


@pytest.mark.parametrize("url", ["", "corescope.lan", "ftp://cs.example", "javascript:alert(1)",
                                 "https://"])
def test_base_url_must_be_http_or_https(url):
    with pytest.raises(ValueError):
        CoreScope(url)


def test_base_url_keeps_path_prefix_and_strips_slash():
    assert CoreScope("http://10.0.0.5:3000/corescope/").base == "http://10.0.0.5:3000/corescope"


# -------------------------------------------------------- default getter ---

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/ok"):
            body, code = json.dumps({"resolved": {}}).encode(), 200
        elif self.path.startswith("/big"):
            body, code = b'{"resolved": {"' + b"a" * (2 * 1024 * 1024) + b'": 1}}', 200
        elif self.path.startswith("/html"):
            body, code = b"<html>nope</html>", 200
        else:
            body, code = json.dumps({"error": "down"}).encode(), 503
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def test_default_get_reads_json(server):
    assert _default_get(server + "/ok", {}, 5) == (200, {"resolved": {}})


def test_default_get_returns_http_error_status(server):
    assert _default_get(server + "/err", {}, 5) == (503, {"error": "down"})


@pytest.mark.parametrize("path", ["/big", "/html"])
def test_default_get_rejects_oversize_or_non_json(server, path):
    with pytest.raises(CoreScopeError):
        _default_get(server + path, {}, 5)


# ------------------------------------------------------------- {pathnames} --

def ctx(corescope, **kw):
    base = {"store": Store(":memory:"), "corescope": corescope, "sender": "bob", "hops_n": 2,
            "hops": ["90e2", "fe27"], "hash_size": 2, "gateway": "openbot",
            "self_pos": (32.8, -97.2), "self_pubkey": "ab" * 32, "units": "imperial",
            "extra": []}
    base.update(kw)
    return base


def remote(**hops):
    get = FakeGet(data=payload(**hops))
    return CoreScope("https://cs.example", get=get), get


def test_path_names_hops_the_local_roster_has_never_heard():
    cs, _ = remote(**{"90e2": [cand("90e2" + "0" * 60, "RptA", 32.9, -97.3)],
                      "fe27": [cand("fe27" + "0" * 60, "RptB", 32.85, -97.25)]})
    out = fn_pathnames(ctx(cs), None)
    assert out.startswith("@[bob] [2h] ~") and out.endswith("mi RptA>RptB")


def test_path_picks_the_nearby_candidate_not_corescopes_first_pick():
    cs, _ = remote(ab12=[cand(PK_AB12_FAR, "AMA-Rpt", 35.2, -101.8),
                         cand(PK_AB_NEAR, "Near-Rpt", 30.6, -97.8)])
    out = fn_pathnames(ctx(cs, hops_n=1, hops=["ab12"], self_pos=(30.67, -97.92)), None)
    assert out.endswith(" Near-Rpt") and "AMA" not in out


def test_local_name_kept_and_missing_position_filled_from_corescope():
    store = Store(":memory:")
    store.upsert_node("fe27" + "0" * 60, name="LocalB", type="repeater")      # no position
    cs, _ = remote(**{"90e2": [cand("90e2" + "0" * 60, "RptA", 32.9, -97.3)],
                      "fe27": [cand("fe27" + "0" * 60, "RemoteB", 32.85, -97.25)]})
    out = fn_pathnames(ctx(cs, store=store), None)
    assert out.endswith(" RptA>LocalB") and " ~" in out          # full distance, not ">"


def test_path_falls_back_to_local_when_corescope_fails():
    cs = CoreScope("https://cs.example", get=FakeGet(503, {"error": "down"}))
    assert fn_pathnames(ctx(cs), None) == "@[bob] [2h] 90e2>fe27"


def test_direct_request_never_calls_corescope():
    cs, get = remote()
    out = fn_pathnames(ctx(cs, hops_n=0, hops=[]), None)
    assert "direct" in out and not get.calls


# ----------------------------------------------------------------- regions --
#
# /api/scope-stats?window=7d: packets CoreScope's observers matched to each
# region they hold a key for. /api/scope-audit?window=1h: each repeater's
# declared region list (the window only scopes forwarding counts).

class RouteGet:
    """Answers by URL path: path -> (status, data), or an exception to raise."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, timeout))
        for prefix, answer in self.routes.items():
            if prefix in url:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return 404, {"error": "not found"}


def stats(*rows):
    return 200, {"window": "7d", "summary": {"scoped": 9}, "timeSeries": [],
                 "byRegion": [{"name": n, "count": c} for n, c in rows]}


def audit(*declared):
    return 200, {"window": "1h", "repeaters": [
        {"publicKey": "%064x" % i, "declaredRegions": regs, "declaredWildcard": True}
        for i, regs in enumerate(declared)]}


def test_regions_merge_traffic_and_declarations():
    get = RouteGet({"/api/scope-stats": stats(("#tx-dfw", 1203), ("#tx-fw", 88)),
                    "/api/scope-audit": audit(["tx-dfw", "tx-austin"], ["#tx-dfw"], [])})
    out = CoreScope("https://cs.example", get=get).regions()
    assert [c[0] for c in get.calls] == ["https://cs.example/api/scope-stats?window=7d",
                                         "https://cs.example/api/scope-audit?window=1h"]
    assert out == {"partial": None, "regions": [
        {"name": "#tx-dfw", "packets": 1203, "repeaters": 2},
        {"name": "#tx-fw", "packets": 88, "repeaters": 0},
        {"name": "#tx-austin", "packets": 0, "repeaters": 1},
    ]}


def test_regions_keep_case_and_drop_names_that_cannot_be_regions():
    get = RouteGet({
        "/api/scope-stats": stats(("#TX-DFW", 5), ("*", 9), ("#a b", 1), ("#x,y", 1),
                                  ("#" + "z" * 33, 1), ("$priv", 1), ("#é", 1), ("", 3),
                                  ("##dup", 1)),
        "/api/scope-audit": audit(["#*", None, 7, "ok-okc", "ok-okc"])})
    names = [r["name"] for r in CoreScope("https://cs.example", get=get).regions()["regions"]]
    # #TX-DFW and #tx-dfw are different keys on air, so case is kept.
    assert names == ["#TX-DFW", "#ok-okc"]


def test_regions_tolerate_malformed_rows():
    get = RouteGet({"/api/scope-stats": (200, {"byRegion": [
        "x", {"name": "#a"}, {"name": "#b", "count": "9"}, {"name": "#c", "count": True},
        {"name": "#d", "count": -4}, {"name": "#e", "count": 3}]}),
        "/api/scope-audit": (200, {"repeaters": ["x", {"declaredRegions": "a,b"}, {}]})})
    out = CoreScope("https://cs.example", get=get).regions()["regions"]
    assert {r["name"]: r["packets"] for r in out} == {"#a": 0, "#b": 0, "#c": 0, "#d": 0, "#e": 3}


def test_regions_without_declarations_are_partial():
    get = RouteGet({"/api/scope-stats": stats(("#tx-dfw", 4)),
                    "/api/scope-audit": OSError("timed out")})
    out = CoreScope("https://cs.example", get=get).regions()
    assert out["regions"] == [{"name": "#tx-dfw", "packets": 4, "repeaters": 0}]
    assert "timed out" in out["partial"]


@pytest.mark.parametrize("answer", [
    (500, {"error": "scope_name column not present"}),
    (404, None),
    (200, {"byRegion": "nope"}),
    OSError("connection refused"),
])
def test_regions_fail_without_traffic_stats(answer):
    get = RouteGet({"/api/scope-stats": answer, "/api/scope-audit": audit(["a"])})
    with pytest.raises(CoreScopeError):
        CoreScope("https://cs.example", get=get).regions()


def test_regions_are_capped():
    rows = [("#r%d" % i, 1000 - i) for i in range(400)]
    get = RouteGet({"/api/scope-stats": stats(*rows), "/api/scope-audit": audit()})
    out = CoreScope("https://cs.example", get=get).regions()["regions"]
    assert len(out) == 200 and out[0]["name"] == "#r0"


def test_regions_audit_gets_a_longer_timeout():
    get = RouteGet({"/api/scope-stats": stats(), "/api/scope-audit": audit()})
    CoreScope("https://cs.example", get=get).regions()
    assert get.calls[0][1] <= 5 < get.calls[1][1] <= 20
