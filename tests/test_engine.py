import copy
import json
from datetime import datetime
from pathlib import Path

import pytest

from openhop_openbot.config import validate
from openhop_openbot.engine.bot import Engine, Request
from openhop_openbot.engine.builtins import BUILTINS, HELP
from openhop_openbot.engine.schedule import Schedule
from openhop_openbot.engine.templates import clamp_bytes, render
from openhop_openbot.store import Store

DEFAULTS = json.loads((Path(__file__).parent.parent / "config.default.json").read_text())


def make(cfg_over=None, **node):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["limits"]["user_cooldown_s"] = 0
    for k, v in (cfg_over or {}).items():
        cfg[k] = v
    cfg, problems = validate(cfg)
    assert not problems
    store = Store(":memory:")
    return Engine(cfg, store, node={"name": "openbot", **node}), store


def req(text="!ping", **kw):
    base = dict(sender="bob", text=text, channel="#bot", channel_idx=1, region="*",
                hops_n=2, hops=["a1b2", "c3d4"], hash_size=2, snr=6.5)
    base.update(kw)
    return Request(**base)


# ------------------------------------------------------------ templates ----

def test_render_nesting_and_brace_neutralising():
    fns = {"x": lambda c, a: "{stats}", "echo": lambda c, a: a or ""}
    assert render("[{x}]", {}, fns) == "[(stats)]"
    assert render("{echo {x}}", {}, fns) == "(stats)"
    assert render("{nope} ok", {}, fns) == "{nope} ok"
    assert render("{boom}", {}, {"boom": lambda c, a: 1 / 0}) == "∅"


def test_clamp_never_splits_codepoints():
    s = clamp_bytes("é" * 100, 161)
    assert len(s.encode()) <= 161 and s == "é" * 80


def test_every_builtin_has_help():
    assert set(BUILTINS) == set(HELP)


# --------------------------------------------------------------- scope -----

def test_ping_answers_in_scope():
    eng, _ = make()
    res = eng.handle(req())
    assert res.matched == "ping" and res.reply.startswith("@[bob] pong! 2 hops traversed")


def test_trigger_needs_word_boundary_and_prefers_longest():
    eng, _ = make({"commands": [
        {"id": "p", "trigger": "!p", "reply": "short", "cooldown_s": 0},
        {"id": "path", "trigger": "!path", "reply": "long", "cooldown_s": 0},
    ]})
    assert eng.handle(req("!path now")).reply == "long"
    assert eng.handle(req("!pathx")).matched is None
    assert eng.handle(req("!P")).reply == "short"


def test_channel_scope_global_and_override():
    eng, _ = make()
    assert eng.handle(req(channel="#random")).reason.startswith("channel")
    eng.cfg["commands"][1]["channels"] = ["random"]
    assert eng.handle(req(channel="#Random")).matched == "ping"
    assert "channel" in eng.handle(req(channel="#bot", sender="eve")).reason


def test_protected_channel_always_wins():
    eng, _ = make({"scope": {**DEFAULTS["scope"], "channels": []}})
    eng.cfg["commands"][1]["channels"] = ["Public"]
    assert "protected" in eng.handle(req(channel="Public", channel_idx=0)).reason


@pytest.mark.parametrize("region,ok", [
    ("#tx-dfw", True), ("#TX-DFW", True), ("#tx-austin", False),
    ("*", False), ("?", False), ("direct", True),
])
def test_region_scope(region, ok):
    eng, _ = make({"regions": ["#tx-dfw", "#tx-austin"],
                   "scope": {**DEFAULTS["scope"], "regions": ["tx-dfw"]}})
    res = eng.handle(req(region=region))
    assert bool(res.reply) is ok, res.reason


def test_region_scope_star_allows_unscoped_and_command_override():
    eng, _ = make({"scope": {**DEFAULTS["scope"], "regions": ["#tx-dfw", "*"]}})
    assert eng.handle(req(region="*")).reply
    eng.cfg["commands"][1]["regions"] = ["#tx-austin"]
    assert not eng.handle(req(region="#tx-dfw", sender="z")).reply
    assert eng.handle(req(region="#tx-austin", sender="z")).reply


def test_dm_scope():
    eng, _ = make()
    assert eng.handle(req(channel="dm", channel_idx=None)).reply
    eng.cfg["scope"]["allow_dm"] = False
    assert "DM" in eng.handle(req(channel="dm", channel_idx=None, sender="q")).reason
    eng.cfg["commands"][1]["dm"] = True
    assert eng.handle(req(channel="dm", channel_idx=None, sender="q")).reply


def test_max_hops():
    eng, _ = make({"scope": {**DEFAULTS["scope"], "max_hops": 1}})
    assert "hops" in eng.handle(req()).reason


def test_ignores_own_messages():
    eng, _ = make()
    assert eng.handle(req(sender="OpenBot")).reason == "own message"


# ------------------------------------------------------------ cooldowns ----

def test_cooldown_per_requester_channel_region():
    eng, _ = make()
    assert eng.handle(req(), now=1000).reply
    assert eng.handle(req(), now=1005).reason == "command cooldown"
    assert eng.handle(req(sender="alice"), now=1005).reply
    assert eng.handle(req(region="#x"), now=1005).reply
    assert eng.handle(req(), now=1011).reply


def test_user_floor_spans_commands():
    eng, _ = make()
    eng.cfg["limits"]["user_cooldown_s"] = 30
    assert eng.handle(req("!ping"), now=1000).reply
    assert eng.handle(req("!stats"), now=1001).reason == "requester cooldown"


def test_empty_reply_releases_cooldown():
    eng, _ = make({"commands": [{"id": "e", "trigger": "!e", "reply": "{args}",
                                 "cooldown_s": 60}]})
    assert eng.handle(req("!e"), now=1000).reason == "reply rendered empty"
    assert eng.handle(req("!e hi"), now=1001).reply == "hi"


def test_dry_run_takes_no_cooldown():
    eng, _ = make()
    assert eng.handle(req(), dry_run=True, now=1000).reply
    assert eng.handle(req(), now=1000).reply


def test_reply_scope():
    eng, _ = make()
    from openhop_openbot.mesh.frames import region_key

    assert eng.reply_scope("#tx-dfw") == region_key("#tx-dfw")
    assert eng.reply_scope("*") == b""
    assert eng.reply_scope("?") is None and eng.reply_scope("direct") is None
    eng.cfg["limits"]["reply_in_request_region"] = False
    assert eng.reply_scope("#tx-dfw") is None


# ----------------------------------------------------------------- path ----

def seed(store):
    store.upsert_node("a1b2" + "0" * 60, name="RptNorth", type="repeater", lat=33.0, lon=-97.0)
    store.upsert_node("c3d4" + "0" * 60, name="RptMid", type="repeater", lat=32.9, lon=-97.1)
    store.upsert_node("bb" * 32, name="bob", type="chat", lat=33.1, lon=-96.9)


def test_path_shows_the_hop_hashes():
    eng, store = make(self_pos=(32.8, -97.2))
    seed(store)
    assert eng.handle(req("!path")).reply == "@[bob] [2h] a1b2,c3d4"


def test_pathnames_resolves_names_and_distance():
    eng, store = make({"commands": [{"id": "names", "trigger": "!names",
                                     "reply": "{pathnames}", "cooldown_s": 0}]},
                      self_pos=(32.8, -97.2))
    seed(store)
    out = eng.handle(req("!names")).reply
    assert out.startswith("@[bob] [2h] ~") and "mi RptNorth>RptMid" in out
    assert len(out.encode()) <= 160


def test_path_direct_unresolved_and_hint():
    eng, store = make()
    assert "direct" in eng.handle(req("!path", hops_n=0, hops=[])).reply
    res = eng.handle(req("!path", sender="z", hops=["ff", "ee"], hash_size=1))
    assert res.reply == "@[z] [2h] ff,ee"
    assert res.extra and "path.hash.mode 1" in res.extra[0]
    # the nudge is rate-limited per requester
    eng.cfg["commands"][0]["cooldown_s"] = 0
    assert not eng.handle(req("!path", sender="z", hops=["ff"], hash_size=1)).extra


def test_seen_whois_stats_help():
    eng, store = make()
    seed(store)
    assert eng.handle(req("!seen rptn")).reply.startswith("RptNorth (repeater)")
    assert eng.handle(req("!whois c3d4", sender="b2")).reply == "RptMid"
    assert eng.handle(req("!stats", sender="b3")).reply.startswith("3 nodes / 2 repeaters")
    assert "!path" in eng.handle(req("!help", sender="b4")).reply


# ------------------------------------------------------------- schedule ----

def test_cron_and_every():
    s = Schedule.parse("30 7 * * 1-5")
    assert s.matches(datetime(2026, 11, 9, 7, 30))      # Monday
    assert not s.matches(datetime(2026, 11, 8, 7, 30))  # Sunday
    assert Schedule.parse("*/15 * * * *").matches(datetime(2026, 1, 1, 3, 45))
    assert Schedule.parse("0 0 * * 0").matches(datetime(2026, 11, 8, 0, 0))
    assert Schedule.parse("0 0 * * 7").matches(datetime(2026, 11, 8, 0, 0))
    assert Schedule.parse("every 2h").every_s == 7200
    for bad in ("every 5s", "* * *", "61 * * * *", "every"):
        with pytest.raises(ValueError):
            Schedule.parse(bad)


# --------------------------------------------------------------- config ----

def test_validate_reports_and_skips_bad_entries():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["channels"] = [{"idx": 0, "name": "Public"}, {"idx": 2, "name": "nohash"},
                       {"idx": 3, "name": "#ok"}, {"idx": 3, "name": "#dup"}]
    cfg["commands"].append({"id": "ping", "trigger": "!other", "reply": "x"})
    cfg["announcements"] = [{"id": "a", "channel": "#ok", "schedule": "bogus", "text": "x"}]
    out, problems = validate(cfg)
    assert [c["idx"] for c in out["channels"]] == [3]
    assert len(problems) == 5
    assert not out["announcements"]


# ----------------------------------------------------------------- took ----

@pytest.mark.parametrize("sent,heard,want", [
    (1_700_000_000, 1_700_000_004.6027, "4.6"),
    (1_700_000_000, 1_700_000_000.0, "0.0"),
    (1_700_000_010, 1_700_000_004.6, "?"),          # sender's clock ahead
    (1_700_000_000, 1_700_009_000.0, "?"),          # over an hour: sender's clock behind
    (None, 1_700_000_004.6, "?"),                   # no sender timestamp (test box)
    (0, 1_700_000_004.6, "?"),                      # unset clock
])
def test_took_seconds_from_sender_timestamp_to_heard(sent, heard, want):
    assert BUILTINS["took"]({"sent_ts": sent, "heard_at": heard}, None) == want


def test_default_ping_reply():
    eng, _ = make()
    res = eng.handle(req(ts=1_700_000_000, heard_at=1_700_000_004.6027))
    assert res.reply == "@[bob] pong! 2 hops traversed, took 4.6 seconds."
