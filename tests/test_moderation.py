"""Aliases, bans, per-sender limits, automatic bans, traffic analytics, nudges."""

import copy
import json
from pathlib import Path

import pytest

from openhop_openbot.config import validate
from openhop_openbot.engine.usage import skip_category, summarize_traffic
from openhop_openbot.hub import Hub
from openhop_openbot.mesh.frames import (
    SCOPE_OTHER,
    decode_frame_header,
    frame_scope,
    group_text_plain,
    hashtag_secret,
    region_key,
)

from .helpers import TS, chan_hash, channel_msg, grp_frame, grp_payload
from .test_engine import DEFAULTS, make, req
from .test_hub import FakeClient


# ------------------------------------------------------------- aliases ----

def test_alias_answers_as_its_command_and_shares_its_cooldown():
    eng, _ = make()
    eng.cfg["commands"][0]["aliases"] = ["!p", "!route"]          # path
    res = eng.handle(req("!p"), now=1000)
    assert res.matched == "path" and res.reply == "@[bob] [2h] a1b2,c3d4"
    assert eng.handle(req("!route"), now=1001).reason == "command cooldown"
    assert eng.handle(req("!px"), now=1100).matched is None        # word boundary


def test_alias_args_start_after_the_alias():
    eng, _ = make({"commands": [{"id": "e", "trigger": "!echo", "aliases": ["!e"],
                                 "reply": "<{args}>", "cooldown_s": 0}]})
    assert eng.handle(req("!e hi there")).reply == "<hi there>"
    assert eng.handle(req("!echo hi")).reply == "<hi>"


def test_help_lists_triggers_not_aliases():
    eng, _ = make()
    eng.cfg["commands"][0]["aliases"] = ["!p"]
    out = eng.handle(req("!help")).reply
    assert "!path" in out and "!p " not in out + " "


def test_alias_can_not_take_another_commands_trigger():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["commands"][0]["aliases"] = ["!p", "!PING", "!p"]
    out, problems = validate(cfg)
    assert [c["id"] for c in out["commands"]][:2] == ["path", "ping"]
    assert out["commands"][0]["aliases"] == ["!p"]
    assert len(problems) == 2


# ---------------------------------------------------------------- bans ----

def test_ban_pattern_is_a_regex_searched_in_the_name():
    eng, _ = make({"bans": [{"pattern": r"^spam\d*$"}, {"pattern": "troll", "enabled": False}]})
    res = eng.handle(req(sender="SPAM42"))
    assert res.matched == "ping" and not res.reply and res.reason.startswith("banned")
    assert skip_category(res.reason) == "banned"
    assert eng.handle(req(sender="spammer")).reply            # anchored: no match
    assert eng.handle(req(sender="troll")).reply              # disabled rule


def test_ban_case_sensitivity_is_per_rule():
    eng, _ = make({"bans": [{"pattern": "^Bob$", "ignore_case": False}]})
    assert eng.handle(req(sender="Bob")).reason.startswith("banned")
    assert eng.handle(req(sender="bob")).reply


def test_bad_ban_pattern_is_reported_and_skipped():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["bans"] = [{"pattern": "(unclosed"}, {"pattern": ""}, "^ok$"]
    out, problems = validate(cfg)
    assert [b["pattern"] for b in out["bans"]] == ["^ok$"]
    assert len(problems) == 2 and "unterminated" in problems[0]


def test_banned_requests_hold_no_cooldown_and_dry_run_sees_bans():
    eng, store = make({"bans": [{"pattern": "^eve$"}]})
    assert eng.handle(req(sender="eve"), dry_run=True).reason.startswith("banned")
    eng.cfg["bans"] = []
    assert eng.handle(req(sender="eve"), now=1000).reply      # nothing held from before


# ------------------------------------------------------- per-sender limits ----

def test_burst_cap_counts_answered_requests_per_sender():
    eng, _ = make()
    eng.cfg["limits"].update(user_max_requests=2, user_window_s=60)
    assert eng.handle(req("!ping"), now=1000).reply
    assert eng.handle(req("!stats"), now=1001).reply
    res = eng.handle(req("!seen x"), now=1002)
    assert res.reason == "requester rate limit (2 per 60s)"
    assert skip_category(res.reason) == "rate limit"
    assert eng.handle(req("!seen x", sender="amy"), now=1002).reply
    assert eng.handle(req("!seen x"), now=1061).reply          # the window moved on


def test_burst_slot_is_given_back_when_the_reply_is_empty():
    eng, _ = make()
    eng.cfg["limits"].update(user_max_requests=1, user_window_s=60)
    assert eng.handle(req("!mpath"), now=1000).reason == "reply rendered empty"  # no CoreScope
    assert eng.handle(req("!ping"), now=1001).reply


def test_auto_ban_after_repeated_refusals():
    eng, store = make()
    eng.cfg["limits"]["user_cooldown_s"] = 30
    eng.cfg["auto_ban"] = {"enabled": True, "strikes": 2, "window_s": 600, "ban_h": 1}
    assert eng.handle(req("!ping"), now=1000).reply
    first = eng.handle(req("!stats"), now=1001)
    assert first.reason == "requester cooldown" and not first.auto_banned
    second = eng.handle(req("!stats"), now=1002)
    assert second.auto_banned and "banned automatically for 1h" in second.reason
    assert skip_category(second.reason) == "cooldown"
    assert eng.handle(req("!ping"), now=1100).reason.startswith("banned (automatic ban")
    assert [b["sender"] for b in store.auto_bans(now=1100)] == ["bob"]
    assert eng.handle(req("!ping"), now=1002 + 3601).reply     # an hour after it began
    assert eng.handle(req("!ping", sender="amy"), now=1100).reply


def test_auto_ban_off_never_bans():
    eng, store = make()
    eng.cfg["limits"]["user_cooldown_s"] = 30
    for t in range(1000, 1010):
        eng.handle(req("!ping"), now=t)
    assert not store.auto_bans(now=1010)


def test_lifting_an_auto_ban_clears_strikes():
    eng, store = make()
    store.auto_ban("bob", 5000, "test", now=1000)
    assert eng.ban_reason("Bob", now=1001)
    assert store.lift_auto_ban("bob") and not eng.ban_reason("bob", now=1001)


# ---------------------------------------------------------------- frames ----

def _hdr(text, **kw):
    raw, _ = grp_frame(text, real=True, **kw)
    return decode_frame_header(raw)


def test_frame_scope_keeps_unlisted_regions_scoped():
    keys = {"#tx-dfw": region_key("#tx-dfw")}
    assert frame_scope(_hdr("bob: hi"), keys) == "*"
    assert frame_scope(_hdr("bob: hi", region="#tx-dfw"), keys) == "#tx-dfw"
    assert frame_scope(_hdr("bob: hi", region="#elsewhere"), keys) == SCOPE_OTHER
    assert frame_scope(_hdr("bob: hi", route=2), keys) == "direct"
    assert frame_scope(None, keys) == "?"


def test_group_text_plain_needs_the_right_key():
    payload = grp_payload("bob: hello", "#bot")
    assert group_text_plain(hashtag_secret("#bot"), payload) == (TS, "bob: hello")
    assert group_text_plain(hashtag_secret("#other"), payload) is None
    assert group_text_plain(hashtag_secret("#bot"), payload[:3]) is None


# ------------------------------------------------------------- traffic ----

def test_traffic_summary_splits_by_scope_and_ignores_unknown_in_the_share():
    window = {"from": 7200.0 + 1800, "to": 7200.0 + 4 * 3600, "label": "w", "bucket": "hour"}
    rows = [{"ts": 7200, "channel": "#bot", "scope": "*", "count": 3},      # straddles start
            {"ts": 10800, "channel": "#bot", "scope": "#tx-dfw", "count": 1},
            {"ts": 10800, "channel": "Public", "scope": "other", "count": 2},
            {"ts": 14400, "channel": "#bot", "scope": "?", "count": 5},
            {"ts": 3600, "channel": "#bot", "scope": "*", "count": 9}]      # before the window
    out = summarize_traffic(rows, [{"ts": 9000, "channel": "bot", "sender": "bob"}],
                            window, "UTC")
    t = out["totals"]
    assert (t["unscoped"], t["scoped"], t["unknown"], t["messages"]) == (3, 3, 5, 11)
    assert t["unscoped_pct"] == 50.0 and t["nudges"] == 1
    bot = next(c for c in out["channels"] if c["name"] == "#bot")
    assert bot["unscoped_pct"] == 75.0 and bot["nudges"] == 1
    assert {r["name"] for r in out["regions"]} == {"#tx-dfw", "other regions"}
    assert sum(b["counts"]["unscoped"] for b in out["series"]) == 3


def test_traffic_share_is_none_without_known_scopes():
    window = {"from": 0.0, "to": 3600.0, "label": "w", "bucket": "hour"}
    out = summarize_traffic([{"ts": 0, "channel": "#bot", "scope": "?", "count": 2}], [],
                            window, "UTC")
    assert out["totals"]["unscoped_pct"] is None


# ------------------------------------------------------------ hub: nudges ----

@pytest.fixture
def hub(tmp_path):
    cfg = {"regions": ["#tx-dfw"],
           "limits": {"user_cooldown_s": 0},
           "nudges": [{"channel": "#bot", "per_sender": 1, "per_channel": 2, "window_h": 1,
                       "region": "#tx-dfw"}]}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    h = Hub(tmp_path / "config.json", tmp_path)
    h.client = FakeClient()
    h.slots = {1: {"name": "#bot", "ok": True}}
    h.correlator.channel_hashes[1] = chan_hash("#bot")
    h.node.update(name="openbot")
    return h


async def say(h, text, *, frame=True, **frame_kw):
    raw, pl = grp_frame(text, **{"real": True, **frame_kw})
    if frame:
        await h._on_raw_frame(raw, 6.0, -90)
    await h._on_message(channel_msg(text, pl))


def nudges(h):
    return [j for j in h.tx._jobs if j.label.startswith("nudge:")]


async def test_nudge_goes_to_a_verified_unscoped_sender_in_the_rule_region(hub):
    await say(hub, "bob: hello all")
    [job] = nudges(hub)
    assert job.text.startswith("@[bob] your message had no region")
    assert job.channel_idx == 1 and job.scope == region_key("#tx-dfw")
    assert hub.activity[-1]["kind"] == "nudge"


async def test_no_nudge_without_proof_of_an_unscoped_flood(hub):
    await say(hub, "amy: hi", real=False)                  # ciphertext does not decrypt
    await say(hub, "cy: hi", region="#tx-dfw")             # scoped
    await say(hub, "di: hi", region="#elsewhere")          # scoped, to a region we don't list
    await say(hub, "ed: hi", frame=False)                  # no frame matched
    await say(hub, "fi: hi", route=2)                      # direct
    assert not nudges(hub)


async def test_nudge_caps_per_sender_and_per_channel(hub):
    await say(hub, "bob: one")
    await say(hub, "bob: two")
    await say(hub, "amy: one")
    await say(hub, "cy: one")                              # channel cap of 2 reached
    assert [j.text.split("]")[0] for j in nudges(hub)] == ["@[bob", "@[amy"]


async def test_nudge_skips_banned_senders_other_channels_and_the_bot(hub):
    hub.engine.cfg["bans"] = [{"pattern": "^bob$"}]
    await say(hub, "bob: hello")
    await say(hub, "openbot: hello")
    hub.slots[2] = {"name": "#chat", "ok": True}
    hub.correlator.channel_hashes[2] = chan_hash("#chat")
    raw, pl = grp_frame("amy: hello", "#chat", real=True)
    await hub._on_raw_frame(raw, 6.0, -90)
    await hub._on_message(channel_msg("amy: hello", pl, idx=2))
    assert not nudges(hub)


async def test_nudge_and_command_reply_both_go_out(hub):
    await say(hub, "bob: !ping")
    labels = sorted(j.label.split(":")[0] for j in hub.tx._jobs)
    assert labels == ["nudge", "ping"]


async def test_dry_run_previews_the_nudge(hub):
    res = await hub.execute("test", {"text": "hello", "channel": "#bot", "sender": "eve",
                                     "region": "*"})
    assert res["matched"] is None and res["nudge"].startswith("@[eve] your message")
    res = await hub.execute("test", {"text": "hello", "channel": "#bot", "sender": "eve",
                                     "region": "#tx-dfw"})
    assert "nudge" not in res


async def test_usage_reports_traffic_by_scope(hub):
    await say(hub, "bob: one")
    await say(hub, "amy: two", region="#tx-dfw")
    await say(hub, "cy: three", region="#elsewhere")
    await say(hub, "di: four", frame=False)
    t = (await hub.execute("usage", {"range": "24h"}))["traffic"]
    assert (t["totals"]["unscoped"], t["totals"]["scoped"], t["totals"]["unknown"]) == (1, 2, 1)
    assert t["totals"]["unscoped_pct"] == pytest.approx(33.3) and t["totals"]["nudges"] == 1
    assert [c["name"] for c in t["channels"]] == ["#bot"]


# ---------------------------------------------------------- hub: bans ----

async def test_ban_test_and_unban_actions(hub):
    hub.engine.cfg["bans"] = [{"pattern": "^spam", "note": "flooding"}]
    assert (await hub.execute("ban_test", {"name": "Spammy"}))["banned"] == "ban #1 ^spam"
    assert (await hub.execute("ban_test", {"name": "bob"}))["banned"] == ""
    hub.store.auto_ban("bob", 4e9, "test")                # 2096
    assert (await hub.execute("ban_test", {"name": "BOB"}))["banned"].startswith("automatic")
    assert [b["sender"] for b in hub.runtime_payload()["auto_bans"]] == ["bob"]
    assert (await hub.execute("unban", {"sender": "Bob"}))["lifted"] is True
    assert (await hub.execute("unban", {"sender": "bob"}))["lifted"] is False
    with pytest.raises(ValueError):
        await hub.execute("ban_test", {"name": " "})


async def test_auto_ban_is_logged_in_activity(hub):
    hub.engine.cfg["limits"]["user_cooldown_s"] = 30
    hub.engine.cfg["auto_ban"] = {"enabled": True, "strikes": 1, "window_s": 600, "ban_h": 2}
    await say(hub, "bob: !ping", region="#tx-dfw")
    await say(hub, "bob: !stats", region="#tx-dfw")
    bans = [a for a in hub.activity if a["kind"] == "ban"]
    assert len(bans) == 1 and bans[0]["sender"] == "bob"
    assert "banned automatically" in bans[0]["detail"]


def test_nudge_validation():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["nudges"] = [{"channel": "Public"}, {"channel": "#bot", "per_channel": "lots"},
                     {"channel": "bot"}, {"channel": "#x", "text": " "}]
    out, problems = validate(cfg)
    assert [n["channel"] for n in out["nudges"]] == ["#bot"]
    assert out["nudges"][0]["per_channel"] == 6
    assert len(problems) == 4


def test_defaults_ship_moderation_off_and_burst_on():
    out, problems = validate(copy.deepcopy(DEFAULTS))
    assert not problems
    assert out["bans"] == [] and out["nudges"] == [] and out["auto_ban"]["enabled"] is False
    assert out["limits"]["user_max_requests"] == 8 and out["limits"]["user_window_s"] == 600
    shipped = json.loads((Path(__file__).parent.parent / "config.default.json").read_text())
    assert "bans" in shipped and "nudges" in shipped and "auto_ban" in shipped
