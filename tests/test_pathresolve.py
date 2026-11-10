import pytest

from openhop_openbot.engine import pathresolve as pr

NOW = 1_800_000_000.0


def node(pk, name, lat=None, lon=None, typ="repeater", heard=NOW - 3600):
    return {"pubkey": pk, "name": name, "type": typ, "lat": lat, "lon": lon,
            "last_heard": heard}


def lookup(nodes):
    def cands(prefix):
        return [n for n in nodes if n["pubkey"].startswith(prefix)]
    return cands


def test_haversine():
    assert pr.haversine_km(30, -97, 30, -97) == 0
    # one degree of latitude is about 111 km
    assert pr.haversine_km(30, -97, 31, -97) == pytest.approx(111.2, abs=0.5)


def test_split_chain():
    assert pr.split_chain("aabbcc", 3) == ["aa", "bb", "cc"]
    assert pr.split_chain("AABBCCDD", 2) == ["aabb", "ccdd"]
    assert pr.split_chain("aabbccddeeff", 2) == ["aabbcc", "ddeeff"]
    for bad in [("", 1), ("aabbc", 1), ("zz", 1), ("aabbcc", 2), ("aabbccdd", 1),
                ("aa", 0), ("aa", pr.MAX_HOPS + 1)]:
        with pytest.raises(ValueError):
            pr.split_chain(*bad)


def test_collision_resolved_by_geography():
    # Two "ab" nodes: one near the chain, one 1500 km away (e.g. another state).
    nodes = [
        node("aa01", "A", 30.00, -97.00),
        node("ab01", "B-near", 30.20, -97.10),
        node("ab99", "B-far", 41.00, -87.00),
        node("cc01", "C", 30.40, -97.20),
    ]
    r = pr.resolve(["aa", "ab", "cc"], lookup(nodes), now=NOW)
    assert [h["name"] for h in r["hops"]] == ["A", "B-near", "C"]
    assert r["hops"][1]["candidates"] == 2 and r["hops"][1]["ambiguous"]
    assert not r["hops"][0]["ambiguous"]
    assert r["resolved"] == 3
    assert r["total_km"] == pytest.approx(
        pr.haversine_km(30.0, -97.0, 30.2, -97.1) + pr.haversine_km(30.2, -97.1, 30.4, -97.2),
        abs=0.2)


def test_anchors_at_start_and_end():
    # Unanchored, the two "ab" candidates are indistinguishable single-hop
    # picks (tie -> first). The start anchor pulls the choice to the west node.
    nodes = [node("ab01", "East", 30.0, -96.0), node("ab02", "West", 30.0, -98.0)]
    west, east = (30.0, -98.05), (30.0, -95.95)
    assert pr.resolve(["ab"], lookup(nodes), start=west, now=NOW)["hops"][0]["name"] == "West"
    assert pr.resolve(["ab"], lookup(nodes), end=east, now=NOW)["hops"][0]["name"] == "East"
    r = pr.resolve(["ab"], lookup(nodes), start=west, end=(30.0, -98.1), now=NOW)
    assert r["hops"][0]["name"] == "West"
    assert r["total_km"] == pytest.approx(
        pr.haversine_km(*west, 30.0, -98.0) + pr.haversine_km(30.0, -98.0, 30.0, -98.1), abs=0.2)


def test_unlocated_single_candidate_resolves_by_name():
    nodes = [node("aa01", "A", 30.0, -97.0), node("bb01", "NoPos"),
             node("cc01", "C", 30.1, -97.1)]
    r = pr.resolve(["aa", "bb", "cc"], lookup(nodes), now=NOW)
    mid = r["hops"][1]
    assert mid["name"] == "NoPos" and mid["lat"] is None and mid["pubkey"] == "bb01"
    assert not mid["ambiguous"]
    assert r["resolved"] == 3
    assert r["total_km"] is None  # not every hop is located
    assert r["partial_km"] == pytest.approx(pr.haversine_km(30.0, -97.0, 30.1, -97.1), abs=0.2)


def test_zero_zero_position_is_unlocated():
    r = pr.resolve(["aa"], lookup([node("aa01", "Z", 0.0, 0.0)]), now=NOW)
    assert r["hops"][0]["name"] == "Z" and r["hops"][0]["lat"] is None


def test_no_candidates_and_ambiguous_unlocated():
    nodes = [node("dd01", "D1"), node("dd02", "D2")]
    r = pr.resolve(["ee", "dd"], lookup(nodes), now=NOW)
    ee, dd = r["hops"]
    assert ee == {"hash": "ee", "name": None, "pubkey": None, "lat": None, "lon": None,
                  "candidates": 0, "ambiguous": False}
    assert dd["name"] is None and dd["candidates"] == 2 and dd["ambiguous"]
    assert r["resolved"] == 0 and r["total_km"] is None and r["partial_km"] is None


def test_far_segment_penalty():
    # A and C are ~193 km apart. "Off" sits on A and is fresher, so a plain
    # distance sum (equal for both) plus recency would pick it; the far-segment
    # factor on its 193 km leg makes the evenly-placed (staler) "Mid" win.
    nodes = [
        node("aa01", "A", 30.0, -97.0),
        node("ab01", "Off", 30.0, -97.0, heard=NOW),
        node("ab02", "Mid", 30.0, -96.0, heard=NOW - 20 * 86400),
        node("cc01", "C", 30.0, -95.0),
    ]
    r = pr.resolve(["aa", "ab", "cc"], lookup(nodes), now=NOW)
    assert r["hops"][1]["name"] == "Mid"
    assert pr._seg_cost(50.0) == 50.0
    assert pr._seg_cost(150.0) == 100.0 + 50.0 * pr.FAR_SEGMENT_FACTOR


def test_non_relay_and_recency_penalties():
    nodes = [node("ab01", "Chat", 30.0, -97.0, typ="chat"),
             node("ab02", "Rep", 30.3, -97.0, typ="repeater")]
    # Chat is 33 km nearer to the start anchor but pays the non-relay penalty.
    r = pr.resolve(["ab"], lookup(nodes), start=(29.9, -97.0), now=NOW)
    assert r["hops"][0]["name"] == "Rep"
    nodes = [node("ab01", "Stale", 30.0, -97.0, heard=NOW - 400 * 86400),
             node("ab02", "Fresh", 30.0, -97.0, heard=NOW - 60)]
    assert pr.resolve(["ab"], lookup(nodes), now=NOW)["hops"][0]["name"] == "Fresh"


def test_max_hops_guard():
    ok = ["aa"] * pr.MAX_HOPS
    assert len(pr.resolve(ok, lookup([]), now=NOW)["hops"]) == pr.MAX_HOPS
    with pytest.raises(ValueError):
        pr.resolve(ok + ["aa"], lookup([]), now=NOW)
    with pytest.raises(ValueError):
        pr.resolve([], lookup([]), now=NOW)


@pytest.mark.parametrize("bad", [["a"], ["aa", "bbcc"], ["zz"], ["aabbccdd"]])
def test_malformed_hops_rejected(bad):
    with pytest.raises(ValueError):
        pr.resolve(bad, lookup([]), now=NOW)


def test_candidate_cap_and_dedupe():
    nodes = [node("ab%02x" % i, "n%d" % i, 30.0, -97.0, heard=NOW - i) for i in range(40)]
    nodes.append(dict(nodes[0]))  # duplicate pubkey
    r = pr.resolve(["ab"], lookup(nodes), now=NOW)
    assert r["hops"][0]["candidates"] == pr.MAX_CANDIDATES
