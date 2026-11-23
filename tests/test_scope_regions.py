"""The Scope tab's region data, end to end through the hub: scoped frames heard
on air, CoreScope's region list fetched and cached, and both in runtime.json."""

import json
import time

import pytest

from openhop_openbot.hub import Hub
from openhop_openbot.mesh.frames import region_key
from openhop_openbot.services.corescope import CoreScopeError

from .helpers import grp_frame

CS_URL = "https://cs.example"


class FakeCoreScope:
    def __init__(self, base=CS_URL, regions=None, error=None):
        self.base, self.calls = base, 0
        self._regions, self._error = regions, error

    def regions(self):
        self.calls += 1
        if self._error:
            raise CoreScopeError(self._error)
        return {"regions": self._regions or [], "partial": None}


def make_hub(tmp_path, regions=("#tx-dfw",), corescope=True):
    cfg = {"regions": list(regions)}
    if corescope:
        cfg["corescope"] = {"enabled": True, "base_url": CS_URL}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    return Hub(tmp_path / "config.json", tmp_path)


def runtime(h, tmp_path):
    h._write_runtime()
    return json.loads((tmp_path / "runtime.json").read_text())["regions"]


ROWS = [{"name": "#tx-dfw", "packets": 1203, "repeaters": 9},
        {"name": "#tx-fw", "packets": 88, "repeaters": 2}]


async def test_scoped_frames_heard_are_counted_per_region(tmp_path):
    h = make_hub(tmp_path)
    for i in range(3):
        raw, _ = grp_frame("bob: %d" % i, region="#tx-dfw")
        await h._on_raw_frame(raw, 6.0, -90)
    raw, _ = grp_frame("bob: plain")                       # unscoped flood
    await h._on_raw_frame(raw, 6.0, -90)
    rg = runtime(h, tmp_path)
    assert rg["scoped_packets"] == 3
    assert rg["heard"][0]["name"] == "#tx-dfw" and rg["heard"][0]["packets"] == 3
    assert rg["heard"][0]["verified"] is True


async def test_non_text_scoped_frames_count_too(tmp_path):
    h = make_hub(tmp_path)
    raw, _ = grp_frame("bob: x", region="#tx-dfw")
    advert_like = bytes([raw[0] & 0x03 | (4 << 2)]) + raw[1:]   # payload type ADVERT
    from openhop_openbot.mesh.frames import decode_frame_header, transport_code
    hdr = decode_frame_header(advert_like)
    code = transport_code(region_key("#tx-dfw"), 4, hdr["payload"])
    advert_like = advert_like[:1] + code.to_bytes(2, "little") + advert_like[3:]
    await h._on_raw_frame(advert_like, 6.0, -90)
    assert runtime(h, tmp_path)["heard"][0]["name"] == "#tx-dfw"


async def test_refresh_caches_the_list_and_watches_unticked_regions(tmp_path):
    h = make_hub(tmp_path)
    h.engine.corescope = FakeCoreScope(regions=ROWS)
    res = await h.execute("corescope_regions", {})
    assert res == {"regions": 2, "partial": None}
    assert h.regionwatch.watching() == ["#tx-dfw", "#tx-fw"]
    assert "#tx-fw" in h.regionwatch.candidates and "#tx-dfw" in h.regionwatch.enabled
    cs = runtime(h, tmp_path)["corescope"]
    assert cs["enabled"] and cs["regions"] == ROWS and cs["error"] is None
    assert time.time() - cs["fetched_at"] < 5
    saved = json.loads((tmp_path / "corescope_regions.json").read_text())
    assert saved["base_url"] == CS_URL and saved["regions"] == ROWS


async def test_cached_list_is_used_after_a_restart(tmp_path):
    h = make_hub(tmp_path)
    h.engine.corescope = FakeCoreScope(regions=ROWS)
    await h.execute("corescope_regions", {})
    h2 = make_hub(tmp_path)
    assert "#tx-fw" in h2.regionwatch.candidates
    assert runtime(h2, tmp_path)["corescope"]["regions"] == ROWS


async def test_a_failed_refresh_keeps_the_last_list_and_says_why(tmp_path):
    h = make_hub(tmp_path)
    h.engine.corescope = FakeCoreScope(regions=ROWS)
    await h.execute("corescope_regions", {})
    h.engine.corescope = FakeCoreScope(error="HTTP 503 down")
    with pytest.raises(ValueError, match="503"):
        await h.execute("corescope_regions", {})
    cs = runtime(h, tmp_path)["corescope"]
    assert cs["regions"] == ROWS and "503" in cs["error"] and cs["error_at"] >= cs["fetched_at"]
    assert "#tx-fw" in h.regionwatch.candidates


async def test_a_list_from_another_corescope_is_not_used(tmp_path):
    h = make_hub(tmp_path)
    h.engine.corescope = FakeCoreScope(regions=ROWS)
    await h.execute("corescope_regions", {})
    (tmp_path / "config.json").write_text(json.dumps(
        {"regions": ["#tx-dfw"], "corescope": {"enabled": True, "base_url": "https://other.example"}}))
    assert h._reload_config()
    assert h.regionwatch.candidates == {}
    assert runtime(h, tmp_path)["corescope"]["regions"] == []


async def test_corescope_off_means_no_candidates_and_no_refresh(tmp_path):
    h = make_hub(tmp_path, corescope=False)
    with pytest.raises(ValueError, match="CoreScope is off"):
        await h.execute("corescope_regions", {})
    rg = runtime(h, tmp_path)
    assert rg["corescope"] == {"enabled": False, "fetched_at": None, "error": None,
                               "error_at": None, "partial": None, "regions": []}
    assert rg["watching"] == ["#tx-dfw"]


async def test_the_region_list_has_its_own_switch(tmp_path):
    """corescope.regions off: no fetch, no candidates, none in runtime; map links
    and !mpath keep their client. Back on, the cached list returns unfetched."""
    h = make_hub(tmp_path)
    h.engine.corescope = FakeCoreScope(regions=ROWS)
    await h.execute("corescope_regions", {})
    assert set(h.regionwatch.candidates) == {"#tx-fw"}

    def write(regions):
        (tmp_path / "config.json").write_text(json.dumps(
            {"regions": ["#tx-dfw"],
             "corescope": {"enabled": True, "base_url": CS_URL, "regions": regions}}))
        assert h._reload_config()

    write(False)
    assert h.engine.corescope is not None and h.regionwatch.candidates == {}
    with pytest.raises(ValueError, match="region list is off"):
        await h.execute("corescope_regions", {})
    cs = runtime(h, tmp_path)["corescope"]
    assert cs["enabled"] is False and cs["regions"] == []
    assert h._cs_list_client() is None

    write(True)
    assert set(h.regionwatch.candidates) == {"#tx-fw"}
    assert runtime(h, tmp_path)["corescope"]["regions"] == ROWS


async def test_the_refresh_loop_leaves_corescope_alone_while_the_list_is_off(
        tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps(
        {"corescope": {"enabled": True, "base_url": CS_URL, "regions": False}}))
    h = Hub(tmp_path / "config.json", tmp_path)
    h.engine.corescope = FakeCoreScope(regions=ROWS)
    ticks = []

    async def sleep(_):
        ticks.append(1)
        return len(ticks) >= 2

    monkeypatch.setattr(h, "_sleep", sleep)
    await h._corescope_loop()
    assert h.engine.corescope.calls == 0


def test_a_region_switch_that_is_not_a_boolean_is_reported():
    from openhop_openbot.config import validate
    cfg, problems = validate({"corescope": {"enabled": True, "regions": "no"}})
    assert cfg["corescope"]["regions"] is True
    assert any("corescope.regions" in p for p in problems)


async def test_ticking_a_region_moves_it_from_candidates_to_enabled(tmp_path):
    h = make_hub(tmp_path)
    h.engine.corescope = FakeCoreScope(regions=ROWS)
    await h.execute("corescope_regions", {})
    (tmp_path / "config.json").write_text(json.dumps(
        {"regions": ["#tx-dfw", "#tx-fw"], "corescope": {"enabled": True, "base_url": CS_URL}}))
    assert h._reload_config()
    assert set(h.regionwatch.enabled) == {"#tx-dfw", "#tx-fw"} and not h.regionwatch.candidates


def test_refresh_is_due(tmp_path):
    h = make_hub(tmp_path)
    cs = FakeCoreScope()
    now = 1_800_000_000
    assert h._cs_regions_due(cs, now)                                   # never fetched
    h.cs_regions = {"base_url": CS_URL, "attempt_at": now - 60, "error": None}
    assert not h._cs_regions_due(cs, now)
    h.cs_regions["attempt_at"] = now - 6 * 3600
    assert h._cs_regions_due(cs, now)
    h.cs_regions.update(attempt_at=now - 60, error="down")
    assert not h._cs_regions_due(cs, now)
    h.cs_regions["attempt_at"] = now - 15 * 60
    assert h._cs_regions_due(cs, now)                                   # retry a failure sooner
    h.cs_regions = {"base_url": "https://other.example", "attempt_at": now, "error": None}
    assert h._cs_regions_due(cs, now)                                   # different instance


def test_a_corrupt_cache_is_ignored(tmp_path):
    (tmp_path / "corescope_regions.json").write_text("{not json")
    h = make_hub(tmp_path)
    assert h.regionwatch.candidates == {}
    (tmp_path / "corescope_regions.json").write_text(json.dumps(
        {"base_url": CS_URL, "regions": [{"name": "#ok"}, "junk", {"name": 5}, {"x": 1}]}))
    h = make_hub(tmp_path)
    assert list(h.regionwatch.candidates) == ["#ok"]


@pytest.mark.parametrize("cache", [
    {"regions": 5}, {"regions": True}, {"regions": "abc"}, [1, 2], "x", None,
    {"base_url": CS_URL, "regions": [{"name": "#a"}], "error": {"x": 1}, "fetched_at": "soon",
     "partial": 7, "attempt_at": "nan"},
])
def test_a_malformed_cache_never_stops_the_bot(tmp_path, cache):
    (tmp_path / "corescope_regions.json").write_text(json.dumps(cache))
    h = make_hub(tmp_path)
    cs = runtime(h, tmp_path)["corescope"]
    assert all(v is None or isinstance(v, (int, str)) for k, v in cs.items() if k != "regions")
    assert all(isinstance(r["name"], str) for r in cs["regions"])


def test_cache_rows_are_capped(tmp_path):
    rows = [{"name": "#r%d" % i, "packets": 1} for i in range(500)]
    (tmp_path / "corescope_regions.json").write_text(
        json.dumps({"base_url": CS_URL, "regions": rows}))
    assert len(runtime(make_hub(tmp_path), tmp_path)["corescope"]["regions"]) == 200


@pytest.mark.parametrize("at", [float("nan"), float("inf"), 1_800_000_000 + 86400, "x"])
def test_a_bad_or_future_attempt_time_is_due(tmp_path, at):
    h = make_hub(tmp_path)
    h.cs_regions = {"base_url": CS_URL, "attempt_at": at, "error": None}
    assert h._cs_regions_due(FakeCoreScope(), 1_800_000_000)


class SlowCoreScope(FakeCoreScope):
    def regions(self):
        time.sleep(0.2)
        return super().regions()


async def test_concurrent_refreshes_share_one_fetch(tmp_path):
    import asyncio
    h = make_hub(tmp_path)
    h.engine.corescope = SlowCoreScope(regions=ROWS)
    a, b = await asyncio.gather(h.refresh_corescope_regions(), h.refresh_corescope_regions())
    assert a == b == {"regions": 2, "partial": None}
    assert h.engine.corescope.calls == 1


async def test_background_refresh_skips_when_a_fetch_just_happened(tmp_path):
    h = make_hub(tmp_path)
    h.engine.corescope = FakeCoreScope(regions=ROWS)
    await h.refresh_corescope_regions()
    assert await h.refresh_corescope_regions(force=False) is None
    assert h.engine.corescope.calls == 1
    await h.refresh_corescope_regions()                 # the button always fetches
    assert h.engine.corescope.calls == 2


async def test_refresh_loop_survives_unexpected_errors(tmp_path, monkeypatch):
    import asyncio

    import openhop_openbot.hub as hubmod
    h = make_hub(tmp_path)
    h.engine.corescope = FakeCoreScope()
    calls = []

    async def boom(force=True):
        calls.append(force)
        raise RuntimeError("unexpected")

    monkeypatch.setattr(h, "refresh_corescope_regions", boom)
    monkeypatch.setattr(hubmod, "CS_REGIONS_TICK_S", 0.01)
    task = asyncio.ensure_future(h._corescope_loop())
    await asyncio.sleep(0.1)
    h.stop()
    await task
    assert len(calls) >= 2                              # kept going after the first error


async def test_the_bots_own_relayed_messages_do_not_count(tmp_path):
    from .helpers import chan_hash
    h = make_hub(tmp_path)
    h.slots = {1: {"name": "#bot", "ok": True}}
    h.correlator.channel_hashes[1] = chan_hash("#bot")
    h.node["name"] = "openbot"
    for i in range(4):
        raw, _ = grp_frame("openbot: reply %d" % i, region="#tx-dfw", real=True, ts=1000 + i)
        await h._on_raw_frame(raw, 6.0, -90)
    assert runtime(h, tmp_path)["scoped_packets"] == 0
    raw, _ = grp_frame("bob: hi", region="#tx-dfw", real=True)
    await h._on_raw_frame(raw, 6.0, -90)
    rg = runtime(h, tmp_path)
    assert rg["scoped_packets"] == 1 and rg["heard"][0]["packets"] == 1


async def test_own_adverts_do_not_count(tmp_path, monkeypatch):
    import openhop_openbot.hub as hubmod
    h = make_hub(tmp_path)
    h.node["pubkey"] = "ab" * 32
    raw, _ = grp_frame("x: y", region="#tx-dfw")
    monkeypatch.setattr(hubmod, "decode_advert_frame",
                        lambda raw: {"pubkey": bytes.fromhex("ab" * 32), "path": b""})
    await h._on_raw_frame(raw, 6.0, -90)
    assert runtime(h, tmp_path)["scoped_packets"] == 0
