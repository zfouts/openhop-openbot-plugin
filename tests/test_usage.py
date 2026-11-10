"""Command-usage recording, windows and summaries."""

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from openhop_openbot.engine.usage import parse_window, skip_category, summarize
from openhop_openbot.hub import Hub
from openhop_openbot.store import Store

from .helpers import chan_hash, channel_msg, grp_frame
from .test_hub import FakeClient

CHI = "America/Chicago"
Z = ZoneInfo(CHI)


def local(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=Z).timestamp()


def row(ts, cmd="ping", outcome="answered", reason=None, channel="#bot", region="*",
        sender="bob"):
    return dict(ts=ts, cmd_id=cmd, outcome=outcome, reason=reason, channel=channel,
                region=region, sender=sender)


# ------------------------------------------------------------------ store ----

def test_record_and_window_bounds():
    s = Store()
    for ts in (100.0, 200.0, 300.0):
        s.record_usage(ts=ts, cmd_id="ping", outcome="answered", reason=None,
                       channel="#bot", region=None, sender="bob")
    got = s.usage_rows(100.0, 300.0)
    assert [r["ts"] for r in got] == [100.0, 200.0]
    assert set(got[0]) == {"ts", "cmd_id", "outcome", "reason", "channel", "region", "sender"}


def test_prune_usage_retention():
    s = Store()
    now = 2_000_000_000
    for days in (365, 367):
        s.record_usage(ts=now - days * 86400, cmd_id="ping", outcome="answered", reason=None,
                       channel="#bot", region=None, sender="bob")
    s.prune(now)
    assert [r["ts"] for r in s.usage_rows(0, now)] == [now - 365 * 86400]


# ----------------------------------------------------------------- module ----

@pytest.mark.parametrize("reason,cat", [
    ("requester cooldown", "cooldown"), ("command cooldown", "cooldown"),
    ("channel #x not in scope", "out of scope"), ("region tx not in scope", "out of scope"),
    ("protected channel #x", "out of scope"), ("DMs not allowed", "out of scope"),
    ("5 hops > max 3", "out of scope"), ("reply rendered empty", "empty reply"),
    ("weird", "other"),
])
def test_skip_category(reason, cat):
    assert skip_category(reason) == cat


@pytest.mark.parametrize("rng,secs,label", [
    ("24h", 86400, "Last 24 hours"), ("7d", 7 * 86400, "Last 7 days"),
    ("30d", 30 * 86400, "Last 30 days"), ("90d", 90 * 86400, "Last 90 days"),
    ("1y", 365 * 86400, "Last year"),
])
def test_parse_window_ranges(rng, secs, label):
    now = 1_800_000_000.0
    w = parse_window({"range": rng}, now, CHI)
    assert w["to"] == now and w["from"] == now - secs and w["label"] == label
    assert w["bucket"] == ("hour" if secs <= 48 * 3600 else "day")


def test_parse_window_dates():
    now = local(2026, 11, 9, 12)
    w = parse_window({"from": "2026-11-09", "to": "2026-11-09"}, now, CHI)
    assert w["from"] == local(2026, 11, 9) and w["to"] == now  # capped at now
    assert w["label"] == "2026-11-09" and w["bucket"] == "hour"
    w = parse_window({"from": "2026-11-01", "to": "2026-11-05"}, now, CHI)
    assert w["to"] == local(2026, 11, 6)
    assert w["label"] == "2026-11-01 – 2026-11-05" and w["bucket"] == "day"


@pytest.mark.parametrize("args", [
    {"range": "2w"}, {"from": "nope", "to": "2026-11-01"},
    {"from": "2026-11-05", "to": "2026-11-01"},
    {"from": "2025-01-01", "to": "2026-11-01"},
    {"from": "2026-12-01", "to": "2026-12-02"},
])
def test_parse_window_errors(args):
    with pytest.raises(ValueError):
        parse_window(args, local(2026, 11, 9, 12), CHI)


def test_summarize_basics():
    now = local(2026, 11, 9, 12)
    w = parse_window({"range": "24h"}, now, CHI)
    rows = [row(now - 3600, "path", sender="amy"),
            row(now - 7200, "ping", "skipped", "cooldown", region=None),
            row(now - 7300, "ping", "skipped", "cooldown", channel="dm"),
            row(now - 7400, "ping")]
    rows += [row(now - 100, "x", sender="u%02d" % i) for i in range(12)]
    out = summarize(rows, w, CHI, [{"id": "ping", "trigger": "!ping"}])
    assert out["totals"] == {"requests": 16, "answered": 14, "skipped": 2}
    assert out["commands"][0]["id"] == "x" and out["commands"][0]["trigger"] == "x"
    assert out["commands"][1] == {"id": "ping", "trigger": "!ping", "answered": 1, "skipped": 2}
    assert len(out["requesters"]) == 10
    assert out["requesters"][0] == {"name": "bob", "requests": 3}
    assert {"name": "?", "requests": 1} in out["regions"]
    assert out["skipped"] == [{"reason": "cooldown", "count": 2}]
    assert out["bucket"] == "hour" and len(out["series"]) in (24, 25)
    assert all(set(s) == {"t", "label", "counts"} for s in out["series"])
    assert sum(sum(s["counts"].values()) for s in out["series"]) == 16
    assert any(s["counts"] == {} for s in out["series"])
    assert out["series"][0]["label"].endswith(":00")


def test_summarize_day_labels():
    now = local(2026, 11, 9, 12)
    w = parse_window({"from": "2026-11-07", "to": "2026-11-09"}, now, CHI)
    out = summarize([row(local(2026, 11, 8, 3))], w, CHI, [])
    assert [s["label"] for s in out["series"]] == ["Nov 7", "Nov 8", "Nov 9"]
    assert out["series"][1]["counts"] == {"ping": 1}


def test_dst_day():
    now = local(2026, 11, 9, 12)
    day = parse_window({"from": "2026-11-01", "to": "2026-11-01"}, now, CHI)
    assert day["to"] - day["from"] == 25 * 3600
    assert len(summarize([], day, CHI, [])["series"]) == 25
    wide = parse_window({"from": "2026-11-01", "to": "2026-11-03"}, now, CHI)
    out = summarize([], wide, CHI, [])
    assert [s["label"] for s in out["series"]] == ["Nov 1", "Nov 2", "Nov 3"]
    assert out["series"][1]["t"] - out["series"][0]["t"] == 25 * 3600


# -------------------------------------------------------------------- hub ----

@pytest.fixture
def hub(tmp_path):
    cfg = {"regions": ["#tx-dfw"], "node": {"latitude": 32.8, "longitude": -97.2},
           "limits": {"user_cooldown_s": 3600}}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    h = Hub(tmp_path / "config.json", tmp_path)
    h.client = FakeClient()
    h.slots = {1: {"name": "#bot", "ok": True}}
    h.correlator.channel_hashes[1] = chan_hash("#bot")
    h.node.update(name="openbot", self_pos=(32.8, -97.2))
    return h


async def deliver(h, text, **frame_kw):
    raw, pl = grp_frame(text, **frame_kw)
    await h._on_raw_frame(raw, 6.0, -90)
    await h._on_message(channel_msg(text, pl))


async def test_hub_records_answered_and_skipped(hub):
    await deliver(hub, "bob: !ping")
    await deliver(hub, "bob: !ping")                       # requester cooldown
    rows = hub.store.usage_rows(0, time.time() + 60)
    assert [(r["cmd_id"], r["outcome"], r["reason"]) for r in rows] == [
        ("ping", "answered", None), ("ping", "skipped", "cooldown")]
    assert rows[0]["sender"] == "bob" and rows[0]["channel"] == "#bot"


async def test_hub_ignores_unmatched_and_dry_run(hub):
    await deliver(hub, "bob: hello all")
    await hub.execute("test", {"text": "!ping", "channel": "#bot", "sender": "eve"})
    assert hub.store.usage_rows(0, time.time() + 60) == []


async def test_execute_usage(hub):
    await deliver(hub, "bob: !ping")
    out = await hub.execute("usage", {"range": "24h"})
    assert set(out) == {"tz", "from", "to", "label", "bucket", "totals", "commands",
                        "series", "requesters", "channels", "regions", "skipped"}
    assert out["totals"] == {"requests": 1, "answered": 1, "skipped": 0}
    assert out["commands"][0]["trigger"] == "!ping"
    with pytest.raises(ValueError):
        await hub.execute("usage", {"range": "2w"})
