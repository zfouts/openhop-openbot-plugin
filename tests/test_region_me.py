"""!region and !me."""

import json
import time

import pytest

from openhop_openbot.engine.builtins import BUILTINS
from openhop_openbot.hub import Hub

from .helpers import chan_hash, channel_msg, grp_frame
from .test_engine import make, req
from .test_hub import FakeClient

KEY = "a1b2c3d4e5f6" + "0" * 52


# -------------------------------------------------------------- !region ----

@pytest.mark.parametrize("scope,want", [
    ("#tx-dfw", "@[bob] heard with region #tx-dfw: only repeaters that carry it pass it on"),
    ("*", "@[bob] heard with no region: every repeater passes it on, across the whole mesh. "
          "Set a region in your app to keep it local"),
    ("direct", "@[bob] heard on a direct route: no region applies"),
    ("?", "@[bob] couldn't tell: your message wasn't matched to the packet the radio heard"),
    ("other", "@[bob] heard with a region this bot doesn't know (it knows #tx-dfw, #tx-aus)"),
])
def test_region_answers_by_how_the_request_was_scoped(scope, want):
    eng, _ = make({"regions": ["#tx-dfw", "#tx-aus"]})
    res = eng.handle(req("!region", scope=scope))
    assert res.matched == "region" and res.reply == want


def test_region_reply_fits_a_long_name():
    eng, _ = make()
    out = eng.handle(req("!region", sender="x" * 31, scope="*")).reply
    assert len(out.encode()) <= 160 and "no region" in out


def test_region_leaves_out_a_known_region_list_that_does_not_fit():
    eng, _ = make({"regions": ["#region-%02d-with-a-long-name" % i for i in range(9)]})
    out = eng.handle(req("!region", scope="other")).reply
    assert out == "@[bob] heard with a region this bot doesn't know"


# ------------------------------------------------------------------ !me ----

def test_me_without_an_advert():
    eng, store = make()
    store.record_msg(channel="#bot", sender="bob", text="hi")
    assert eng.handle(req("!me", hops_n=2, snr=6.5)).reply == (
        "@[bob] this msg 2 hops, SNR 6.5, no advert from you heard yet, 1 msgs in 24h")


def test_me_with_adverts_and_distance():
    eng, store = make(self_pos=(32.8, -97.2))
    now = int(time.time())
    store.upsert_node(KEY, name="Bob", type="chat", lat=32.9, lon=-97.3,
                      heard=now - 12 * 86400, advert=True)
    store.upsert_node(KEY, name="Bob", heard=now - 3 * 3600, advert=True)
    out = eng.handle(req("!me", hops_n=0, snr=9)).reply
    assert out.startswith("@[bob] this msg direct, SNR 9, first advert 12d ago, last 3h ago, ")
    assert "mi from here, 0 msgs in 24h" in out


def test_me_flags_a_shared_name():
    eng, store = make()
    now = int(time.time())
    store.upsert_node(KEY, name="bob", heard=now - 600, advert=True)
    store.upsert_node("ff" * 32, name="BOB", lat=33.0, lon=-97.0, heard=now - 60, advert=True)
    out = eng.handle(req("!me")).reply
    assert "2 nodes use this name, advert heard 60s ago, position on file" in out


def test_me_finds_a_dm_sender_by_key_not_name():
    eng, store = make()
    store.upsert_node(KEY, name="Robert", advert=True)
    store.upsert_node("ff" * 32, name="bob", advert=True)        # someone else named bob
    out = eng.handle(req("!me", channel="dm", channel_idx=None,
                         sender_prefix=bytes.fromhex(KEY[:12]))).reply
    assert "nodes use this name" not in out and "no position in your adverts" in out


def test_me_drops_detail_to_fit():
    eng, store = make(self_pos=(32.8, -97.2))
    for i in range(3):
        store.upsert_node("%02x" % i * 32, name="x" * 31, lat=33.0, lon=-97.0, advert=True)
    out = eng.handle(req("!me", sender="x" * 31, snr=-12.25)).reply
    assert len(out.encode()) <= 160 and out.startswith("@[" + "x" * 31 + "] this msg 2 hops")


def test_both_are_documented_builtins():
    assert {"region_check", "me"} <= set(BUILTINS)


# ------------------------------------------------------------------ hub ----

@pytest.fixture
def hub(tmp_path):
    cfg = {"limits": {"user_cooldown_s": 0},
           "nudges": [{"channel": "#bot", "per_sender": 5, "per_channel": 5}]}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    h = Hub(tmp_path / "config.json", tmp_path)
    h.client = FakeClient()
    h.slots = {1: {"name": "#bot", "ok": True}}
    h.correlator.channel_hashes[1] = chan_hash("#bot")
    h.node.update(name="openbot")
    h.nudge_settle_s = 0
    return h


async def say(h, text, **kw):
    import asyncio

    raw, pl = grp_frame(text, real=True, **kw)
    await h._on_raw_frame(raw, 6.0, -90)
    await h._on_message(channel_msg(text, pl))
    while h._nudges_waiting:
        await asyncio.gather(*list(h._nudges_waiting))


async def test_region_over_the_air_answers_instead_of_a_nudge(hub):
    await say(hub, "bob: !region")
    [job] = list(hub.tx._jobs)
    assert job.label.startswith("region:") and "heard with no region" in job.text
    await say(hub, "amy: hello")                              # other messages still get one
    assert any(j.label.startswith("nudge:") for j in hub.tx._jobs)


async def test_region_in_a_region_over_the_air(hub):
    hub.engine.cfg["regions"] = ["#tx-dfw"]
    hub._rebuild_regions()
    await say(hub, "bob: !region", region="#tx-dfw")
    assert [j.text for j in hub.tx._jobs] == [
        "@[bob] heard with region #tx-dfw: only repeaters that carry it pass it on"]


async def test_dry_run_uses_the_chosen_region(hub):
    res = await hub.execute("test", {"text": "!region", "channel": "#bot", "sender": "eve",
                                     "region": "*"})
    assert "heard with no region" in res["reply"]
    res = await hub.execute("test", {"text": "!me", "channel": "#bot", "sender": "eve"})
    assert res["matched"] == "me" and "no advert from you heard yet" in res["reply"]
