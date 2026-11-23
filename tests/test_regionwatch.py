"""Which regions this radio hears: matching, counting each packet once, and
when a count is enough to call a region verified."""

import random

import pytest

from openhop_openbot.mesh import regionwatch as rw
from openhop_openbot.mesh.frames import decode_frame_header, region_key
from openhop_openbot.mesh.regionwatch import RegionWatch, chance_tail, verified
from openhop_openbot.store import Store

from .helpers import grp_frame

DAY = 86400
NOW = 1_800_000_000


def hdr(text="bob: hi", **kw):
    return decode_frame_header(grp_frame(text, **kw)[0])


@pytest.fixture
def watch():
    w = RegionWatch(Store(":memory:"))
    w.set_enabled({"#tx-dfw": region_key("#tx-dfw")})
    w.set_candidates(["#tx-fw", "#tx-austin"])
    return w


def test_enabled_region_is_counted(watch):
    assert watch.observe(hdr(region="#tx-dfw"), ts=NOW) == "#tx-dfw"
    s = watch.summary(now=NOW)
    assert s["scoped_packets"] == 1
    assert s["heard"] == [{"name": "#tx-dfw", "packets": 1, "last_heard": NOW, "verified": False}]


def test_region_you_have_not_ticked_is_still_counted(watch):
    assert watch.observe(hdr(region="#tx-fw"), ts=NOW) == "#tx-fw"
    assert [h["name"] for h in watch.summary(now=NOW)["heard"]] == ["#tx-fw"]


def test_relay_copies_count_once(watch):
    a = hdr("bob: hi", region="#tx-dfw", path=bytes.fromhex("a1b2"))
    b = dict(a, path=bytes.fromhex("a1b2c3d4"), path_len=a["path_len"] + 1)
    assert watch.observe(a, ts=NOW) == "#tx-dfw"
    assert watch.observe(b, ts=NOW) is None
    assert watch.summary(now=NOW)["scoped_packets"] == 1


def test_plain_floods_and_direct_packets_are_not_scoped(watch):
    assert watch.observe(hdr(), ts=NOW) is None                          # plain flood
    assert watch.observe(hdr("bob: x", route=2), ts=NOW) is None         # direct
    assert watch.summary(now=NOW) == {"window_days": 7, "scoped_packets": 0,
                                      "watching": ["#tx-austin", "#tx-dfw", "#tx-fw"],
                                      "heard": []}


def test_unknown_region_counts_toward_the_total_only(watch):
    assert watch.observe(hdr(region="#somewhere-else"), ts=NOW) is None
    s = watch.summary(now=NOW)
    assert s["scoped_packets"] == 1 and s["heard"] == []


def test_enabled_region_wins_a_code_collision(watch, monkeypatch):
    monkeypatch.setattr(rw, "transport_code", lambda key, pt, payload: 0x1234)
    h = dict(hdr(region="#tx-dfw"), transport_codes=(0x1234, 0))
    assert watch.observe(h, ts=NOW) == "#tx-dfw"


def test_two_unticked_regions_matching_one_packet_count_for_neither(monkeypatch):
    w = RegionWatch(Store(":memory:"))
    w.set_candidates(["#a", "#b"])
    monkeypatch.setattr(rw, "transport_code", lambda key, pt, payload: 0x1234)
    assert w.observe(dict(hdr(region="#a"), transport_codes=(0x1234, 0)), ts=NOW) is None
    assert w.summary(now=NOW)["scoped_packets"] == 1


def test_candidates_skip_enabled_invalid_and_duplicates_and_are_capped():
    w = RegionWatch(Store(":memory:"))
    w.set_enabled({"#tx-dfw": region_key("#tx-dfw")})
    w.set_candidates(["#tx-dfw", "$priv", "*", "", "tx-fw", "#tx-fw"]
                     + ["#r%d" % i for i in range(100)])
    assert "#tx-dfw" not in w.candidates and "#tx-fw" in w.candidates
    assert len(w.candidates) == rw.MAX_CANDIDATES
    assert "#r0" in w.candidates and "#r99" not in w.candidates


def test_ticking_a_candidate_moves_it_to_enabled_and_back():
    w = RegionWatch(Store(":memory:"))
    w.set_candidates(["#tx-fw"])
    w.set_enabled({"#tx-fw": region_key("#tx-fw")})
    assert "#tx-fw" not in w.candidates
    w.set_enabled({})
    assert "#tx-fw" in w.candidates


# -------------------------------------------------------------- verification

def test_chance_tail():
    assert chance_tail(0, 5.0) == 1.0
    assert chance_tail(3, 0.0) == 0.0
    assert chance_tail(1, 0.5) == pytest.approx(1 - 2.718281828 ** -0.5)
    assert chance_tail(3, 0.01) == pytest.approx(1.6542e-7, rel=1e-3)


@pytest.mark.parametrize("packets,total,ok", [
    (2, 10, False),        # too few, however quiet the mesh
    (3, 10, True),
    (3, 20_000, False),    # 20k scoped packets: 3 matches could be chance
    (7, 20_000, True),
    (12, 600_000, False),  # a busy week: ~9 chance matches per region
    (40, 600_000, True),
])
def test_verified_needs_more_than_chance(packets, total, ok):
    assert verified(packets, total) is ok


def test_noise_never_verifies_a_region():
    """Random scoped traffic for regions nobody listed, tested against a full
    candidate list: some codes collide, none adds up to a verified region."""
    w = RegionWatch(Store(":memory:"))
    w.set_candidates(["#c%d" % i for i in range(rw.MAX_CANDIDATES)])
    rnd = random.Random(7)
    for i in range(4000):
        h = hdr("x: %d" % i, region="#noise")
        h["payload"] = rnd.randbytes(len(h["payload"]))
        h["transport_codes"] = (rnd.randrange(1, 0xFFFF), 0)
        w.observe(h, ts=NOW)
    s = w.summary(now=NOW)
    assert s["scoped_packets"] == 4000
    assert not any(r["verified"] for r in s["heard"])


def test_a_real_region_verifies_among_the_noise():
    w = RegionWatch(Store(":memory:"))
    w.set_candidates(["#tx-fw"])
    for i in range(5):
        w.observe(hdr("bob: %d" % i, region="#tx-fw"), ts=NOW)
    (row,) = w.summary(now=NOW)["heard"]
    assert row["name"] == "#tx-fw" and row["packets"] == 5 and row["verified"]


def test_only_the_last_week_counts_but_last_heard_is_kept():
    w = RegionWatch(Store(":memory:"))
    w.set_candidates(["#tx-fw"])
    for i in range(5):
        w.observe(hdr("bob: %d" % i, region="#tx-fw"), ts=NOW - 10 * DAY)
    (row,) = w.summary(now=NOW)["heard"]
    assert row == {"name": "#tx-fw", "packets": 0, "last_heard": NOW - 10 * DAY,
                   "verified": False}


def test_store_prunes_region_counts_after_30_days():
    s = Store(":memory:")
    s.count_region("#a", ts=NOW - 31 * DAY)
    s.count_region("#b", ts=NOW - 29 * DAY)
    s.prune(now=NOW)
    assert set(s.region_counts(since=0)) == {"#b"}


def test_store_counts_per_day_and_keeps_the_latest_time():
    s = Store(":memory:")
    s.count_region("#a", ts=NOW - 2 * DAY)
    s.count_region("#a", ts=NOW - 5)
    s.count_region("#a", ts=NOW - 50)
    assert s.region_counts(since=NOW - DAY) == {"#a": {"packets": 2, "last": NOW - 5}}
    assert s.region_counts(since=0)["#a"]["packets"] == 3


def test_counts_are_written_in_batches():
    class CountingStore(Store):
        batches = 0

        def add_region_counts(self, rows):
            rows = list(rows)
            if rows:
                CountingStore.batches += 1
            super().add_region_counts(rows)

    w = RegionWatch(CountingStore(":memory:"))
    w.set_candidates(["#tx-fw"])
    for i in range(20):
        w.observe(hdr("bob: %d" % i, region="#tx-fw"), ts=NOW)
    assert CountingStore.batches == 0
    (row,) = w.summary(now=NOW)["heard"]
    assert row["packets"] == 20 and CountingStore.batches == 1


def test_a_failed_flush_keeps_the_counts():
    class FlakyStore(Store):
        fail = True

        def add_region_counts(self, rows):
            if FlakyStore.fail:
                FlakyStore.fail = False
                raise OSError("disk full")
            super().add_region_counts(rows)

    w = RegionWatch(FlakyStore(":memory:"))
    w.set_candidates(["#tx-fw"])
    for i in range(3):
        w.observe(hdr("bob: %d" % i, region="#tx-fw"), ts=NOW)
    with pytest.raises(OSError):
        w.flush()
    (row,) = w.summary(now=NOW)["heard"]
    assert row["packets"] == 3 and row["verified"]


def test_summary_survives_a_failed_flush():
    class BrokenStore(Store):
        def add_region_counts(self, rows):
            raise OSError("disk full")

    w = RegionWatch(BrokenStore(":memory:"))
    w.set_candidates(["#tx-fw"])
    w.observe(hdr(region="#tx-fw"), ts=NOW)
    assert w.summary(now=NOW)["heard"] == []          # nothing stored yet, no exception
    assert w._pending                                  # kept for the next try
