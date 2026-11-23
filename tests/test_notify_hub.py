"""Operator notifications through the hub: what triggers them and where they go."""

import json

import pytest

from openhop_openbot import __version__
from openhop_openbot.hub import Hub
from openhop_openbot.mesh.frames import region_key

from .helpers import chan_hash, channel_msg, grp_frame
from .test_hub import FakeClient

ADMIN = "ab" * 32


def make_hub(tmp_path, events=("update", "ignored", "started", "problems"), **extra):
    cfg = {"channels": [{"idx": 1, "name": "#bot"}, {"idx": 2, "name": "#ops"}],
           "limits": {"user_cooldown_s": 0, "tx_max_per_min": 100},
           "notify": {"targets": [{"kind": "channel", "channel": "#ops", "region": "#tx-dfw"},
                                  {"kind": "dm", "pubkey": ADMIN, "name": "zach"}],
                      "events": {e: True for e in events}, "max_per_hour": 20},
           **extra}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    h = Hub(tmp_path / "config.json", tmp_path)
    h.client = FakeClient()
    h.slots = {1: {"name": "#bot", "ok": True}, 2: {"name": "#ops", "ok": True}}
    h.correlator.channel_hashes[1] = chan_hash("#bot")
    h._contacts = {bytes.fromhex(ADMIN)[:6]: "zach"}
    h.node.update(name="openbot")
    return h


def notes(h):
    return [j for j in h.tx._jobs if j.kind == "notify"]


async def deliver(h, text):
    raw, pl = grp_frame(text)
    await h._on_raw_frame(raw, 6.0, -90)
    await h._on_message(channel_msg(text, pl))


def test_channel_and_dm_targets(tmp_path):
    h = make_hub(tmp_path)
    h.notify("started", "hello")
    ch, dm = notes(h)
    assert ch.channel_idx == 2 and ch.scope == region_key("#tx-dfw") and ch.text == "hello"
    assert dm.dm_prefix == bytes.fromhex(ADMIN)[:6] and dm.text == "hello"


def test_an_admin_who_is_not_a_contact_is_reported(tmp_path):
    h = make_hub(tmp_path)
    h._contacts = {}
    out = h.notify("started", "hello")
    assert out["sent"] == 1 and "not a contact" in out["failed"][0]
    assert any(a["kind"] == "error" and "zach" in a.get("detail", "") for a in h.activity)


def test_started_is_sent_once_per_run(tmp_path):
    h = make_hub(tmp_path)
    h._notify_started()
    h._notify_started()
    assert len(notes(h)) == 2 and __version__ in notes(h)[0].text


async def test_a_banned_sender_is_reported_once_a_day(tmp_path):
    h = make_hub(tmp_path, bans=[{"pattern": "^spammer$"}])
    await deliver(h, "spammer: !ping")
    await deliver(h, "spammer: !ping")
    texts = [j.text for j in notes(h)]
    assert len(texts) == 2 and "spammer" in texts[0]          # one message, two targets


async def test_an_automatic_ban_is_reported(tmp_path):
    h = make_hub(tmp_path, limits={"user_cooldown_s": 0, "tx_max_per_min": 100,
                                   "user_max_requests": 1, "user_window_s": 600},
                 auto_ban={"enabled": True, "strikes": 1, "window_s": 600, "ban_h": 2})
    await deliver(h, "bob: !ping")                            # answered
    await deliver(h, "bob: !path")                            # over the cap: strike 1 -> ban
    assert any("bob" in j.text and "ban" in j.text.lower() for j in notes(h))


def test_update_is_announced_once_per_version(tmp_path):
    h = make_hub(tmp_path)
    latest = {"version": "99.0.0", "tag": "v99.0.0", "url": "https://github.com/x"}
    h.check_update(fetch=lambda: latest)
    assert len(notes(h)) == 2 and "99.0.0" in notes(h)[0].text
    h.check_update(fetch=lambda: latest)
    assert len(notes(h)) == 2
    assert h.update_info["latest"] == "99.0.0" and h.update_info["available"] is True
    h2 = make_hub(tmp_path)                                   # survives a restart
    h2.check_update(fetch=lambda: latest)
    assert not notes(h2)


def test_update_not_marked_while_notifications_are_off(tmp_path):
    h = make_hub(tmp_path, events=())
    latest = {"version": "99.0.0", "tag": "v99.0.0", "url": "https://github.com/x"}
    h.check_update(fetch=lambda: latest)
    assert not notes(h)
    h2 = make_hub(tmp_path)                                   # turned on later: still told
    h2.check_update(fetch=lambda: latest)
    assert len(notes(h2)) == 2


def test_no_update_when_running_the_latest(tmp_path):
    h = make_hub(tmp_path)
    h.check_update(fetch=lambda: {"version": __version__, "tag": "v" + __version__, "url": ""})
    assert not notes(h) and h.update_info["available"] is False


def test_a_failed_update_check_is_recorded_not_sent(tmp_path):
    from openhop_openbot.services.github import GitHubError
    h = make_hub(tmp_path)

    def boom():
        raise GitHubError("HTTP 403 API rate limit exceeded")

    h.check_update(fetch=boom)
    assert not notes(h) and "rate limit" in h.update_info["error"]


def test_new_config_problems_are_reported(tmp_path):
    h = make_hub(tmp_path)
    cfg = json.loads((tmp_path / "config.json").read_text())
    cfg["node"] = {"path_hash_bytes": 9}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    assert h._reload_config()
    assert any("path_hash_bytes" in j.text for j in notes(h))


def test_repeated_tx_failures_are_reported_once(tmp_path):
    from openhop_openbot.mesh.txqueue import TxJob
    h = make_hub(tmp_path)
    for _ in range(5):
        h._on_tx_result(TxJob(text="x", channel_idx=1, kind="reply"), False, "refused")
    assert len(notes(h)) == 2
    h._on_tx_result(TxJob(text="x", channel_idx=1, kind="notify"), False, "refused")
    assert len(notes(h)) == 2                                  # its own failures never loop


async def test_corescope_failing_three_times_is_reported(tmp_path):
    from .test_scope_regions import FakeCoreScope
    h = make_hub(tmp_path, corescope={"enabled": True, "base_url": "https://cs.example"})
    h.engine.corescope = FakeCoreScope(error="HTTP 503 down")
    for _ in range(3):
        with pytest.raises(ValueError):
            await h.refresh_corescope_regions()
    assert len([j for j in notes(h) if "CoreScope" in j.text]) == 2


async def test_test_action_and_runtime(tmp_path):
    h = make_hub(tmp_path, events=())
    res = await h.execute("notify_test", {})
    assert res["sent"] == 2 and len(notes(h)) == 2
    h._contacts_list = [{"name": "zach", "pubkey": ADMIN}]
    h._write_runtime()
    rt = json.loads((tmp_path / "runtime.json").read_text())
    assert rt["contacts"] == [{"name": "zach", "pubkey": ADMIN}]
    assert rt["notify"]["sent"] == 1 and "update" in rt
