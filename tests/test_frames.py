from openhop_core.protocol.transport_keys import calc_transport_code, get_auto_key_for

from openhop_openbot.mesh.correlate import FrameCorrelator, region_of
from openhop_openbot.mesh.frames import (
    canonical_region,
    decode_frame_header,
    frame_region,
    region_key,
    transport_code,
)

from .helpers import chan_hash, channel_msg, grp_frame

KEYS = {"#tx-dfw": region_key("#tx-dfw"), "#tx-austin": region_key("tx-austin")}


class _Pkt:
    def __init__(self, ptype, payload):
        self._t, self._p = ptype, payload

    def get_payload_type(self):
        return self._t

    def get_payload(self):
        return self._p


def test_region_key_and_code_match_openhop_core():
    for name in ("#tx-dfw", "usa", "#a"):
        assert region_key(name) == get_auto_key_for(name)
    for payload in (b"", b"x", bytes(range(200))):
        assert transport_code(region_key("#tx"), 5, payload) == \
            calc_transport_code(get_auto_key_for("#tx"), _Pkt(5, payload))


def test_canonical_region():
    assert canonical_region("tx-dfw") == "#tx-dfw"
    assert canonical_region("#tx-dfw") == "#tx-dfw"
    assert canonical_region("*") == "*"


def test_header_carries_transport_codes():
    raw, _ = grp_frame("bob: hi", region="#tx-dfw", path=b"\x01\x02\x03\x04")
    hdr = decode_frame_header(raw)
    assert hdr["route"] == 0 and hdr["transport_codes"] is not None
    assert hdr["hop_count"] == 2 and hdr["path"] == b"\x01\x02\x03\x04"


def test_frame_region_variants():
    assert frame_region(decode_frame_header(grp_frame("a: b", region="#tx-dfw")[0]),
                        KEYS) == "#tx-dfw"
    assert frame_region(decode_frame_header(grp_frame("a: b", region="#tx-austin")[0]),
                        KEYS) == "#tx-austin"
    assert frame_region(decode_frame_header(grp_frame("a: b")[0]), KEYS) == "*"
    # scoped to a region we don't know
    assert frame_region(decode_frame_header(grp_frame("a: b", region="#elsewhere")[0]),
                        KEYS) == "?"
    assert frame_region(decode_frame_header(grp_frame("a: b", route=2)[0]), KEYS) == "direct"
    assert frame_region(None, KEYS) == "?"


def test_correlator_claims_matching_frame_once():
    c = FrameCorrelator()
    c.channel_hashes[1] = chan_hash("#bot")
    text = "bob: !path"
    raw, pl = grp_frame(text, path=b"\xa1\xb2\xc3\xd4", region="#tx-dfw")
    c.remember(raw, 5.0)
    msg = channel_msg(text, pl)
    frame = c.claim(msg)
    assert frame is not None and frame.path == b"\xa1\xb2\xc3\xd4"
    assert region_of(frame, KEYS) == "#tx-dfw"
    assert c.claim(msg) is None


def test_correlator_rejects_other_channel():
    c = FrameCorrelator()
    c.channel_hashes[1] = chan_hash("#bot")
    raw, pl = grp_frame("bob: !path", channel="#other")
    c.remember(raw, 5.0)
    assert c.claim(channel_msg("bob: !path", pl)) is None
