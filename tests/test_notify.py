"""Operator notifications: the update check, the notifier, config, and the hub."""

import json

import pytest

from openhop_openbot.config import validate
from openhop_openbot.engine.notify import Notifier
from openhop_openbot.services.github import GitHubError, latest_release, newer, repository

ADMIN = "ab" * 32


# ------------------------------------------------------------------ versions

@pytest.mark.parametrize("a,b", [
    ("1.6.0", "1.5.0"), ("v1.10.0", "1.9.9"), ("1.6.0", "1.6.0-rc.1"), ("1.6.0", "1.6.0rc1"),
    ("1.6.0+edge.3", "1.6.0"), ("1.6.1", "1.6.0+edge.99"), ("2.0", "1.99.99"),
    ("1.6.0-rc.2", "1.6.0-rc.1"), ("1.6.0b1", "1.6.0a9"),
])
def test_newer(a, b):
    assert newer(a, b) and not newer(b, a)


@pytest.mark.parametrize("a,b", [("1.6.0", "1.6.0"), ("v1.6.0", "1.6"), ("junk", "1.0.0"),
                                 ("1.0.0", "junk"), ("", "")])
def test_not_newer(a, b):
    assert not newer(a, b)


# ------------------------------------------------------------- GitHub check

class FakeGet:
    def __init__(self, status=200, data=None):
        self.status, self.data, self.calls = status, data, []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers))
        if isinstance(self.data, Exception):
            raise self.data
        return self.status, self.data


def rel(tag, draft=False, pre=False, wheel=True):
    whl = "openhop_openbot_plugin-%s-py3-none-any.whl" % tag.lstrip("v")
    assets = [{"name": whl}] if wheel else []
    return {"tag_name": tag, "draft": draft, "prerelease": pre, "assets": assets,
            "html_url": "https://github.com/o/r/releases/tag/" + tag}


def test_latest_release_is_the_highest_stable_one_with_a_wheel():
    get = FakeGet(data=[rel("edge", pre=True), rel("v1.7.0", draft=True), rel("v1.6.0"),
                        rel("v1.10.0", wheel=False), rel("v1.5.0"), rel("v2.0.0-rc.1", pre=True)])
    out = latest_release("zfouts/openhop-openbot-plugin", user_agent="ua", get=get)
    assert out == {"version": "1.6.0", "tag": "v1.6.0",
                   "url": "https://github.com/o/r/releases/tag/v1.6.0"}
    url, headers = get.calls[0]
    assert url.startswith("https://api.github.com/repos/zfouts/openhop-openbot-plugin/releases")
    assert headers["User-Agent"] == "ua"


def test_no_stable_release_is_none():
    assert latest_release("o/r", get=FakeGet(data=[rel("edge", pre=True)])) is None


@pytest.mark.parametrize("status,data", [(403, {"message": "API rate limit exceeded"}),
                                         (404, {"message": "Not Found"}), (200, {"x": 1}),
                                         (200, OSError("timed out"))])
def test_github_failures_raise(status, data):
    with pytest.raises(GitHubError):
        latest_release("o/r", get=FakeGet(status, data))


def test_repository_from_openhop_state(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    assert repository(data) == "zfouts/openhop-openbot-plugin"            # no state.json
    (tmp_path / "state.json").write_text(json.dumps({"repository": "someone/fork"}))
    assert repository(data) == "someone/fork"
    (tmp_path / "state.json").write_text(json.dumps({"repository": "../../etc"}))
    assert repository(data) == "zfouts/openhop-openbot-plugin"
    (tmp_path / "state.json").write_text("not json")
    assert repository(data) == "zfouts/openhop-openbot-plugin"


# ----------------------------------------------------------------- notifier

class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def notifier(events=("update", "ignored", "started", "problems"), targets=None, cap=6):
    sent = []
    clock = Clock()

    def send(target, text):
        sent.append((target.get("channel") or target.get("pubkey"), text))
        return None

    n = Notifier(send, clock=clock)
    n.configure({"targets": targets if targets is not None else
                 [{"kind": "channel", "channel": "#ops", "region": ""},
                  {"kind": "dm", "pubkey": ADMIN, "name": "zach"}],
                 "events": {e: True for e in events}, "max_per_hour": cap})
    return n, sent, clock


def test_each_event_goes_to_every_target():
    n, sent, _ = notifier()
    assert n.notify("started", "OpenBot 1.7.0 started")["sent"] == 2
    assert sent == [("#ops", "OpenBot 1.7.0 started"), (ADMIN, "OpenBot 1.7.0 started")]


def test_events_are_opt_in():
    n, sent, _ = notifier(events=("update",))
    assert n.notify("ignored", "x")["why"] == "off"
    assert n.notify("update", "y")["sent"] == 2 and len(sent) == 2


def test_no_targets_sends_nothing():
    n, sent, _ = notifier(targets=[])
    assert n.notify("started", "x")["why"] == "no targets" and not sent


def test_repeats_are_held_back_per_key():
    n, sent, clock = notifier()
    assert n.notify("ignored", "bob ignored", key="bob", every_s=86400)["sent"] == 2
    assert n.notify("ignored", "bob ignored", key="bob", every_s=86400)["why"] == "repeat"
    assert n.notify("ignored", "eve ignored", key="eve", every_s=86400)["sent"] == 2
    clock.t += 86400
    assert n.notify("ignored", "bob ignored", key="bob", every_s=86400)["sent"] == 2


def test_hourly_cap_counts_messages_not_targets():
    n, sent, clock = notifier(cap=2)
    assert n.notify("problems", "a")["sent"] == 2
    assert n.notify("problems", "b")["sent"] == 2
    assert n.notify("problems", "c")["why"] == "hourly cap"
    assert n.dropped == 1 and len(sent) == 4
    clock.t += 3601
    assert n.notify("problems", "d")["sent"] == 2


def test_text_is_clamped_to_one_frame():
    n, sent, _ = notifier()
    n.notify("started", "é" * 200)
    assert all(len(t.encode()) <= 160 for _, t in sent)


def test_a_target_that_cannot_send_is_reported():
    sent = []
    n = Notifier(lambda t, text: "not a contact" if t["kind"] == "dm" else sent.append(text))
    n.configure({"targets": [{"kind": "channel", "channel": "#ops"},
                             {"kind": "dm", "pubkey": ADMIN, "name": "zach"}],
                 "events": {"started": True}})
    out = n.notify("started", "hi")
    assert out["sent"] == 1 and out["failed"] == ["zach: not a contact"]


def test_test_message_ignores_event_switches():
    n, sent, _ = notifier(events=())
    assert n.test("Test notification")["sent"] == 2


# ------------------------------------------------------------------- config

def test_notify_config_is_validated():
    cfg, problems = validate({
        "scope": {"protected_channels": ["Public"]},
        "notify": {"targets": [
            {"kind": "channel", "channel": "#ops", "region": "tx-dfw"},
            {"kind": "channel", "channel": "Public"},
            {"kind": "channel", "channel": ""},
            {"kind": "dm", "pubkey": ADMIN.upper(), "name": "zach"},
            {"kind": "dm", "pubkey": "abcd"},
            {"kind": "carrier-pigeon"},
            "junk"],
            "events": {"update": True, "ignored": "yes", "bogus": True},
            "max_per_hour": 500}})
    n = cfg["notify"]
    assert n["targets"] == [{"kind": "channel", "channel": "#ops", "region": "#tx-dfw"},
                            {"kind": "dm", "pubkey": ADMIN, "name": "zach"}]
    assert n["events"] == {"update": True, "ignored": False, "started": False, "problems": False}
    assert n["max_per_hour"] == 6
    text = " ".join(problems)
    for needle in ("Public", "no channel", "abcd", "carrier-pigeon", "max_per_hour"):
        assert needle in text


def test_notify_ships_off():
    from openhop_openbot.config import defaults
    assert defaults()["notify"] == {"targets": [], "events": {"update": False, "ignored": False,
                                                              "started": False, "problems": False},
                                    "max_per_hour": 6}
