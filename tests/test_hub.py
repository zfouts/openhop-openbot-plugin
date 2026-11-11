"""End to end through the hub: raw frame + decoded message in, a paced reply out."""

import json
from pathlib import Path

import pytest

from openhop_openbot.hub import Hub, split_sender
from openhop_openbot.mesh.frames import region_key

from .helpers import chan_hash, channel_msg, grp_frame


class FakeClient:
    host, port = "127.0.0.1", 5001
    self_info = None

    def __init__(self):
        self.sent = []

    def is_connected(self):
        return True

    async def send_channel_message(self, idx, text, *, scope=None):
        self.sent.append(("chan", idx, text, scope))
        return True

    async def send_text_message(self, prefix, text):
        self.sent.append(("dm", prefix, text, None))
        return True


@pytest.fixture
def hub(tmp_path):
    cfg = {"regions": ["#tx-dfw"], "node": {"latitude": 32.8, "longitude": -97.2},
           "limits": {"user_cooldown_s": 0}}
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


async def test_region_scoped_path_reply(hub):
    hub.store.upsert_node("a1b2" + "0" * 60, name="RptNorth", type="repeater",
                          lat=33.0, lon=-97.0)
    await deliver(hub, "bob: !path", path=bytes.fromhex("a1b2c3d4"), region="#tx-dfw")
    jobs = list(hub.tx._jobs)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.channel_idx == 1 and job.scope == region_key("#tx-dfw")
    assert job.text == "@[bob] [2h] a1b2,c3d4"
    assert hub.regions_seen["#tx-dfw"] == 1
    kinds = [a["kind"] for a in hub.activity]
    assert kinds[-2:] == ["rx", "reply"]


async def test_unscoped_request_gets_unscoped_reply(hub):
    await deliver(hub, "bob: !ping")
    assert hub.tx._jobs[0].scope == b""


async def test_out_of_scope_region_is_skipped(hub):
    hub.cfg["scope"]["regions"] = ["#tx-dfw"]
    await deliver(hub, "bob: !ping")                      # unscoped
    assert not hub.tx._jobs
    assert hub.activity[-1]["kind"] == "skip"
    assert "region" in hub.activity[-1]["detail"]


async def test_chatter_is_recorded_not_answered(hub):
    await deliver(hub, "bob: hello all", path=bytes.fromhex("a1b2"))
    assert not hub.tx._jobs
    assert hub.store.last_msg("bob")["text"] == "hello all"


async def test_mailbox_test_and_runtime(hub, tmp_path):
    res = await hub.execute("test", {"text": "!path", "channel": "#bot", "sender": "eve",
                                     "region": "tx-dfw", "hops": "a1b2>c3d4"})
    assert res["matched"] == "path" and res["reply"].startswith("@[eve] [2h]")
    assert not hub.tx._jobs                                # dry run never transmits
    await hub.execute("send", {"channel": "#bot", "text": "hi", "region": "*"})
    assert hub.tx._jobs[0].scope == b""
    hub._write_runtime()
    rt = json.loads((tmp_path / "runtime.json").read_text())
    assert rt["id"] == "openhop.openbot" and rt["status"]["companion"] == "connected"
    assert {b["name"] for b in rt["builtins"]} >= {"path", "seen"}


async def test_txqueue_paces_and_sends(hub):
    import asyncio

    hub.tx.gap_s = 0.05
    await deliver(hub, "bob: !path", path=bytes.fromhex("a1"), hash_size=1)
    assert len(hub.tx._jobs) == 2                          # reply + 1-byte hint
    hub.tx._jobs[1].not_before = 0
    task = asyncio.create_task(hub.tx.run())
    for _ in range(50):
        if len(hub.client.sent) == 2:
            break
        await asyncio.sleep(0.02)
    hub.tx.stop()
    await task
    assert [s[0] for s in hub.client.sent] == ["chan", "chan"]
    assert "path.hash.mode" in hub.client.sent[1][2]


def test_split_sender():
    assert split_sender("bob: !path") == ("bob", "!path")
    assert split_sender("no prefix") == ("", "no prefix")


def test_manifest_matches_defaults_and_ships_ui():
    root = Path(__file__).parent.parent
    manifest = json.loads((root / "openhop-plugin.json").read_text())
    defaults = json.loads((root / "config.default.json").read_text())
    assert manifest["config"]["defaults"] == defaults
    from openhop_openbot import __version__

    import tomllib

    assert manifest["version"] == __version__
    assert tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"] == __version__
    assert (root / manifest["ui"]["entry"]).is_file()
    pyproject = (root / "pyproject.toml").read_text()
    for f in ("ui/index.html", "ui/app.js", "ui/styles.css"):
        assert (root / f).is_file() and f in pyproject


def _hub_with(tmp_path, cfg):
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    return Hub(tmp_path / "config.json", tmp_path)


def test_corescope_client_built_from_config(tmp_path):
    h = _hub_with(tmp_path, {"corescope": {"enabled": True, "base_url": "http://corescope.lan:8080/"}})
    assert h.engine.corescope.base == "http://corescope.lan:8080"
    assert h.engine.context()["corescope"] is h.engine.corescope


@pytest.mark.parametrize("cs", [
    {"enabled": False, "base_url": "https://cs.example"},
    {"enabled": True, "base_url": ""},
    {"enabled": True, "base_url": "ftp://cs.example"},
])
def test_corescope_off_or_misconfigured_resolves_locally(tmp_path, cs):
    assert _hub_with(tmp_path, {"corescope": cs}).engine.corescope is None


def test_corescope_rebuilt_when_its_config_changes(tmp_path):
    h = _hub_with(tmp_path, {})
    assert h.engine.corescope is None
    (tmp_path / "config.json").write_text(json.dumps(
        {"corescope": {"enabled": True, "base_url": "https://cs.example/x"}}))
    assert h._reload_config()
    assert h.engine.corescope.base == "https://cs.example/x"


def test_corescope_ships_disabled():
    from openhop_openbot.config import defaults
    assert defaults()["corescope"] == {"enabled": False, "base_url": "", "shortener": "v.gd",
                                      "shortener_url": "", "shortener_key": "",
                                      "mpath_wait_s": 30}


def test_unknown_config_sections_are_ignored(tmp_path):
    from openhop_openbot.config import defaults
    d = defaults()
    assert "uplink" not in d and "remote_api" not in d
    # A hand-edited config.json may carry sections this plugin does not know.
    h = _hub_with(tmp_path, {
        "uplink": {"enabled": True, "host": "mqtt.example.com", "username": "u", "password": "p"},
        "remote_api": {"enabled": True, "base_url": "https://example.com"}})
    assert not hasattr(h, "uplink") and "remote_api" not in h.engine.context()
    h._write_runtime()
    assert "uplink" not in json.loads((tmp_path / "runtime.json").read_text())["status"]


async def test_path_link_uses_the_requests_own_packet_hash(hub):
    from openhop_openbot.mesh.frames import decode_frame_header, packet_hash
    from openhop_openbot.services.corescope import CoreScope

    class Short:
        calls = []

        def shorten(self, url):
            self.calls.append(url)
            return "https://v.gd/Xy12z"

    hub.engine.corescope = CoreScope("https://cs.example")
    hub.engine.shortener = Short()
    raw, pl = grp_frame("bob: !path", path=bytes.fromhex("a1b2c3d4"))
    await hub._on_raw_frame(raw, 6.0, -90)
    await hub._on_message(channel_msg("bob: !path", pl))
    want = packet_hash(decode_frame_header(raw)).upper()
    assert Short.calls == ["https://cs.example/#/map?packet=%s&hops=a1b2,c3d4" % want]
    assert hub.tx._jobs[0].text == "@[bob] [2h] a1b2,c3d4 https://v.gd/Xy12z"


@pytest.mark.parametrize("name,want", [("is.gd", "is.gd"), ("tinyurl.com", "tinyurl.com"),
                                       ("none", None), ("bit.ly", None)])
def test_shortener_follows_corescope_config(tmp_path, name, want):
    h = _hub_with(tmp_path, {"corescope": {"enabled": True, "base_url": "https://cs.example",
                                           "shortener": name}})
    got = h.engine.shortener
    assert (got.service if got else None) == want
    assert any("shortener" in p for p in h.problems) == (name == "bit.ly")


def test_custom_shortener_from_config(tmp_path):
    h = _hub_with(tmp_path, {"corescope": {"enabled": True, "base_url": "https://cs.example",
                                           "shortener": "custom",
                                           "shortener_url": "https://short.example.org",
                                           "shortener_key": "k"}})
    assert h.engine.shortener.label == "short.example.org" and not h.problems


@pytest.mark.parametrize("url", ["", "http://short.example.org"])
def test_custom_shortener_needs_an_https_url(tmp_path, url):
    h = _hub_with(tmp_path, {"corescope": {"enabled": True, "base_url": "https://cs.example",
                                           "shortener": "custom", "shortener_url": url}})
    assert h.engine.shortener is None
    assert any(p.startswith("corescope.shortener_url") for p in h.problems)


async def test_ping_took_is_measured_to_when_the_frame_was_heard(hub):
    import re
    import time

    from openhop_openbot.mesh.companion import Message

    raw, pl = grp_frame("bob: !ping", path=bytes.fromhex("a1b2c3d4"))
    await hub._on_raw_frame(raw, 6.0, -90)
    hub.correlator._frames[-1].t -= 2.0          # the radio heard it 2 s before we process it
    sent = int(time.time()) - 3
    await hub._on_message(Message(text="bob: !ping", timestamp=sent, snr=6.5, path_len=pl,
                                  channel_idx=1))
    text = hub.tx._jobs[0].text
    m = re.fullmatch(r"@\[bob\] pong! 2 hops traversed, took (\d+\.\d) seconds\.", text)
    assert m, text
    assert 0.5 <= float(m.group(1)) < 2.5           # heard time, not processing time (3-4 s)


class NamingClient(FakeClient):
    def __init__(self, name):
        super().__init__()
        from openhop_openbot.mesh.companion import SelfInfo
        self.self_info = SelfInfo(public_key=b"\xab" * 32, name=name, latitude=0.0,
                                  longitude=0.0)
        self.renames, self.adverts = [], []

    async def set_advert_name(self, name):
        self.renames.append(name)
        return True

    async def send_self_advert(self, *, flood=True):
        self.adverts.append(flood)
        return True

    async def set_device_time(self, epoch=None):
        return True

    async def set_path_hash_mode(self, hash_bytes):
        return True

    async def set_channel(self, idx, name, secret):
        return True

    async def get_channel(self, idx):
        return None

    async def get_contacts(self):
        return []


@pytest.mark.parametrize("radio,configured,pushed", [
    ("zach_bot", "LibertyHillBot", ["LibertyHillBot"]),
    ("LibertyHillBot", "LibertyHillBot", []),       # already right
    ("zach_bot", "", []),                           # no name set in the plugin page
])
async def test_node_name_is_pushed_to_the_radio_on_connect(hub, radio, configured, pushed):
    hub.client = NamingClient(radio)
    hub.cfg["node"]["name"] = configured
    await hub._on_connected()
    assert hub.client.renames == pushed
    assert hub.node["name"] == (configured or radio)


async def test_node_name_change_in_config_renames_while_connected(hub, tmp_path):
    import asyncio

    hub.client = NamingClient("zach_bot")
    await hub._on_connected()
    cfg = json.loads((tmp_path / "config.json").read_text())
    cfg.setdefault("node", {})["name"] = "LibertyHillBot"
    (tmp_path / "config.json").write_text(json.dumps(cfg) + "\n")
    assert hub._reload_config()
    for _ in range(20):
        if hub.client.renames:
            break
        await asyncio.sleep(0.01)
    assert hub.client.renames == ["LibertyHillBot"] and hub.node["name"] == "LibertyHillBot"


async def test_advert_action(hub):
    hub.client = NamingClient("LibertyHillBot")
    assert await hub.execute("advert", {"flood": False}) == {"sent": True, "flood": False}
    assert await hub.execute("advert", {}) == {"sent": True, "flood": True}
    assert hub.client.adverts == [False, True]


def test_overlong_node_name_is_a_config_problem(tmp_path):
    h = _hub_with(tmp_path, {"node": {"name": "Liberty Hill Community Repeater Bot"}})
    assert any("node.name" in p and "31" in p for p in h.problems)
    assert not _hub_with(tmp_path, {"node": {"name": "LibertyHillBot"}}).problems


# ---------------------------------------------------------------- !mpath ---

class _Short:
    def __init__(self):
        self.calls = []

    def shorten(self, url):
        self.calls.append(url)
        return "https://v.gd/Mp1"


def _with_corescope(hub, rows):
    from openhop_openbot.services.corescope import CoreScope

    calls = []

    def get(url, headers, timeout):
        calls.append(url)
        return 200, {"packet": {"hash": "x"}, "observations": rows}

    hub.engine.corescope = CoreScope("https://cs.example", get=get)
    hub.engine.shortener = _Short()
    waits = []

    async def no_wait(secs):
        waits.append(secs)
        return False

    hub._sleep = no_wait
    return calls, waits


async def test_mpath_acks_then_follows_up_where_the_request_came_from(hub):
    import asyncio

    from openhop_openbot.mesh.frames import decode_frame_header, packet_hash

    rows = [{"observer_id": "AA" * 32, "path_json": '["A1B2","C3D4"]', "snr": 3},
            {"observer_id": "BB" * 32, "path_json": '["A1B2"]', "snr": 9},
            {"observer_id": "BB" * 32, "path_json": '["E5F6"]', "snr": 1}]
    calls, waits = _with_corescope(hub, rows)
    raw, pl = grp_frame("bob: !mpath", path=bytes.fromhex("a1b2c3d4"), region="#tx-dfw")
    await hub._on_raw_frame(raw, 6.0, -90)
    await hub._on_message(channel_msg("bob: !mpath", pl))
    assert [j.text for j in hub.tx._jobs] == ["@[bob] asking CoreScope's observers, back in 30s"]
    await asyncio.gather(*list(hub._follow_ups))
    pkt = packet_hash(decode_frame_header(raw))
    assert waits == [30] and calls == ["https://cs.example/api/packets/" + pkt.lower()]
    assert hub.engine.shortener.calls == ["https://cs.example/#/packet/" + pkt.upper()]
    ack, follow = hub.tx._jobs
    assert follow.text == "@[bob] 3 routes to 2 observers, 1-2 hops https://v.gd/Mp1"
    assert follow.channel_idx == ack.channel_idx == 1
    assert follow.scope == ack.scope == region_key("#tx-dfw")
    assert hub.activity[-1]["detail"] == "mpath (follow-up)"
    assert not hub._follow_ups


async def test_mpath_is_silent_without_corescope(hub):
    await deliver(hub, "bob: !mpath", path=bytes.fromhex("a1b2"))
    assert not hub.tx._jobs and not hub._follow_ups
    assert hub.activity[-1]["kind"] == "skip"


async def test_mpath_dry_run_never_schedules(hub):
    _with_corescope(hub, [])
    res = await hub.execute("test", {"text": "!mpath", "channel": "#bot"})
    assert res["matched"] == "mpath" and "no packet hash" in res["reply"]
    assert not hub._follow_ups and not hub.tx._jobs


async def test_follow_ups_end_with_the_hub(hub):
    import asyncio

    _with_corescope(hub, [])
    del hub._sleep                                # the real, stop-aware sleep
    await deliver(hub, "bob: !mpath", path=bytes.fromhex("a1b2"))
    assert len(hub._follow_ups) == 1
    hub.stop()
    await asyncio.wait_for(asyncio.gather(*list(hub._follow_ups)), 2)
    assert len(hub.tx._jobs) == 1                 # the ack only


async def test_follow_ups_are_capped(hub, monkeypatch):
    import asyncio

    import openhop_openbot.hub as hub_mod

    monkeypatch.setattr(hub_mod, "MAX_FOLLOW_UPS", 1)
    _with_corescope(hub, [])
    del hub._sleep
    await deliver(hub, "bob: !mpath", path=bytes.fromhex("a1b2"))
    await deliver(hub, "eve: !mpath", path=bytes.fromhex("a1b2"))
    assert len(hub._follow_ups) == 1
    assert "follow-up dropped" in hub.activity[-1]["detail"]
    hub.stop()
    await asyncio.gather(*list(hub._follow_ups))
