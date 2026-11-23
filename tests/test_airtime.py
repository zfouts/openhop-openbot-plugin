"""The bot's own transmissions: size and airtime estimates, recording, report."""

import asyncio
import copy
import json
import struct

import pytest

from openhop_openbot.config import validate
from openhop_openbot.engine.usage import summarize_airtime
from openhop_openbot.hub import Hub
from openhop_openbot.mesh.airtime import (
    advert_packet_bytes,
    airtime_ms,
    radio_known,
    text_packet_bytes,
)
from openhop_openbot.mesh.companion import RESP_CODE_SELF_INFO, parse_self_info
from openhop_openbot.mesh.txqueue import MAX_DEPTH, TxJob

from .helpers import chan_hash
from .test_engine import DEFAULTS
from .test_hub import FakeClient, deliver

RADIO = {"freq_khz": 910525, "bw_hz": 250000, "sf": 10, "cr": 5}


# ------------------------------------------------------------ estimates ----

def test_packet_sizes():
    # header, path_len, channel hash, MAC, then 16-byte blocks of ts|flags|"name: text"
    assert text_packet_bytes("hi", node_name="openbot") == 1 + 1 + 1 + 2 + 16
    # 5 + len("openbot: ") + 18 fills two blocks exactly; one more byte needs a third
    assert text_packet_bytes("x" * 18, node_name="openbot") == 1 + 1 + 1 + 2 + 32
    assert text_packet_bytes("x" * 19, node_name="openbot") == 1 + 1 + 1 + 2 + 48
    assert text_packet_bytes("hi", node_name="openbot", scoped=True) == 4 + 21
    # DM: dest and src hashes, and a trailing NUL inside the blocks
    assert text_packet_bytes("x" * 10, dm=True) == 1 + 1 + 2 + 2 + 16
    assert text_packet_bytes("x" * 11, dm=True) == 1 + 1 + 2 + 2 + 32
    assert advert_packet_bytes("openbot", True) == 1 + 1 + 32 + 4 + 64 + 1 + 8 + 7


def test_airtime_uses_the_radio_settings():
    from openhop_core.protocol.packet_utils import calculate_lora_airtime_ms

    assert airtime_ms(69, RADIO, 16) == pytest.approx(calculate_lora_airtime_ms(
        69, 10, 250000, 5, 16))
    slower = airtime_ms(69, {**RADIO, "sf": 11}, 16)
    assert slower > airtime_ms(69, RADIO, 16) > airtime_ms(69, RADIO, 8)
    assert airtime_ms(69, None) is None and not radio_known({"sf": 0, "bw_hz": 0, "cr": 0})


def test_self_info_carries_the_radio():
    frame = (bytes([RESP_CODE_SELF_INFO, 1, 22, 22]) + b"\x11" * 32
             + struct.pack("<ii", 32_900_000, -97_300_000) + bytes(4)
             + struct.pack("<II", 910525, 250000) + bytes([10, 5]) + b"openbot\x00")
    si = parse_self_info(frame)
    assert (si.name, si.radio_freq_khz, si.radio_bw_hz, si.radio_sf, si.radio_cr) == \
        ("openbot", 910525, 250000, 10, 5)


@pytest.mark.parametrize("value,want,problems", [(None, 16, 0), (12, 12, 0), (2, 16, 1),
                                                 ("lots", 16, 1), (12.5, 16, 1)])
def test_preamble_validation(value, want, problems):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["node"]["preamble_symbols"] = value
    out, found = validate(cfg)
    assert out["node"]["preamble_symbols"] == want and len(found) == problems


# --------------------------------------------------------------- report ----

def test_airtime_report():
    window = {"from": 0.0, "to": 7200.0, "label": "w", "bucket": "hour"}
    rows = [
        {"ts": 100, "kind": "reply", "channel": "#bot", "bytes": 69, "airtime_ms": 400.0,
         "outcome": "sent"},
        {"ts": 200, "kind": "reply", "channel": "#bot", "bytes": 69, "airtime_ms": None,
         "outcome": "sent"},
        {"ts": 3700, "kind": "nudge", "channel": "#chat", "bytes": 133, "airtime_ms": 800.0,
         "outcome": "sent"},
        {"ts": 3800, "kind": "announce", "channel": "#bot", "bytes": 50, "airtime_ms": None,
         "outcome": "dropped"},
        {"ts": 3900, "kind": "reply", "channel": "dm", "bytes": 40, "airtime_ms": None,
         "outcome": "failed"},
    ]
    out = summarize_airtime(rows, window, "UTC")
    t = out["totals"]
    assert (t["sent"], t["failed"], t["dropped"], t["unestimated"]) == (3, 1, 1, 1)
    assert t["airtime_ms"] == 1200.0 and t["bytes"] == 69 + 69 + 133
    assert t["share_pct"] == pytest.approx(100 * 1.2 / 7200, abs=1e-3)
    assert out["busiest_hour"]["airtime_ms"] == 800.0
    assert out["busiest_hour"]["share_pct"] == pytest.approx(100 * 0.8 / 3600, abs=1e-3)
    assert [k["kind"] for k in out["kinds"]] == ["reply", "nudge", "announce"]
    assert [b["counts"] for b in out["series"]] == [{"reply": 2}, {"nudge": 1}]


def test_empty_airtime_report():
    out = summarize_airtime([], {"from": 0.0, "to": 3600.0, "label": "w", "bucket": "hour"},
                            "UTC")
    assert out["totals"]["sent"] == 0 and out["busiest_hour"] is None


# ------------------------------------------------------------------ hub ----

class AdvertClient(FakeClient):
    async def send_self_advert(self, flood=True):
        return True


class RefusingClient(FakeClient):
    async def send_channel_message(self, idx, text, *, scope=None):
        return False


@pytest.fixture
def hub(tmp_path):
    cfg = {"limits": {"user_cooldown_s": 0}, "node": {"preamble_symbols": 12}}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    h = Hub(tmp_path / "config.json", tmp_path)
    h.client = AdvertClient()
    h.slots = {1: {"name": "#bot", "ok": True}}
    h.correlator.channel_hashes[1] = chan_hash("#bot")
    h.node.update(name="openbot", radio=dict(RADIO))
    h.tx.gap_s = 0
    return h


async def flush(h):
    task = asyncio.create_task(h.tx.run())
    for _ in range(100):
        if not h.tx._jobs:
            break
        await asyncio.sleep(0.01)
    h.tx.stop()
    await task


async def test_a_reply_is_recorded_with_its_airtime(hub):
    await deliver(hub, "bob: !ping")
    await flush(hub)
    [row] = hub.store.tx_rows(0, 9e9)
    text = hub.client.sent[0][2]
    want = text_packet_bytes(text, node_name="openbot")
    assert (row["kind"], row["channel"], row["outcome"], row["bytes"]) == \
        ("reply", "#bot", "sent", want)
    assert row["airtime_ms"] == pytest.approx(airtime_ms(want, RADIO, 12))


async def test_refused_and_dropped_sends_are_recorded_without_airtime(hub):
    hub.client = RefusingClient()
    hub.send_text("#bot", "hello", kind="send")
    await flush(hub)
    for _ in range(MAX_DEPTH):
        hub.tx.put(TxJob(text="x", channel_idx=1, kind="announce"))
    hub.send_text("#bot", "one too many", kind="announce")
    rows = hub.store.tx_rows(0, 9e9)
    assert [(r["kind"], r["outcome"]) for r in rows] == [("send", "failed"),
                                                         ("announce", "dropped")]
    assert all(r["airtime_ms"] is None for r in rows)


async def test_adverts_are_recorded(hub):
    await hub.execute("advert", {"flood": False})
    [row] = hub.store.tx_rows(0, 9e9)
    assert (row["kind"], row["outcome"]) == ("advert", "sent")
    assert row["bytes"] == advert_packet_bytes("openbot", False)


async def test_without_radio_settings_nothing_is_estimated(hub):
    hub.node["radio"] = None
    hub.send_text("#bot", "hello")
    await flush(hub)
    [row] = hub.store.tx_rows(0, 9e9)
    assert row["outcome"] == "sent" and row["airtime_ms"] is None
    assert hub.runtime_payload()["status"]["radio"] is None


async def test_usage_and_runtime_report_airtime(hub):
    await deliver(hub, "bob: !ping")
    await flush(hub)
    out = await hub.execute("usage", {"range": "24h"})
    assert out["airtime"]["totals"]["sent"] == 1 and out["airtime"]["totals"]["airtime_ms"] > 0
    assert hub.runtime_payload()["status"]["radio"] == {**RADIO, "preamble": 12}
