"""Pair a decoded companion message with the raw frame it arrived in.

The companion's message sync frames carry only ``path_len``: the hop count,
no hop hashes, no route type, no transport codes. All of that was in the raw
frame the radio logged (PUSH_CODE_LOG_RX_DATA) moments earlier. ``!path``
needs the hashes and region scoping needs the transport codes, so each
decoded message is matched back to its frame.

There is no message id on either side, so the match is structural: same
payload type, same path_len byte, a ciphertext exactly as long as this text
encrypts to, and the channel hash or source hash when known. The oldest
frame that passes is the copy the node decoded; later ones are relay
duplicates and age out. A miss returns None, never a guess.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional

from .companion import Message
from .frames import (
    CIPHER_MAC,
    PAYLOAD_TYPE_GRP_TXT,
    PAYLOAD_TYPE_TXT_MSG,
    REGION_UNKNOWN,
    decode_frame_header,
    frame_region,
    packet_hash,
    text_cipher_lengths,
)

FRAME_MAX = 64
FRAME_MAX_AGE_S = 30.0


@dataclass
class TextFrame:
    t: float
    type: int
    path_len: int
    path: bytes
    hash_size: int
    hash: str
    lead: bytes
    cipher_len: int
    snr: float
    rssi: Optional[int]
    hdr: dict = field(repr=False, default_factory=dict)


class FrameCorrelator:
    def __init__(self) -> None:
        self._frames: Deque[TextFrame] = deque()
        # channel idx -> 1-byte channel hash, from the slot's key
        self.channel_hashes: Dict[int, int] = {}

    def remember(self, raw: bytes, snr: float, rssi: Optional[int] = None) -> None:
        hdr = decode_frame_header(raw)
        if hdr is None or hdr["payload_type"] not in (PAYLOAD_TYPE_TXT_MSG, PAYLOAD_TYPE_GRP_TXT):
            return
        payload = hdr["payload"]
        # GRP_TXT: channel_hash(1) | mac(2) | cipher. TXT_MSG: dest(1) src(1) | mac(2) | cipher.
        lead = 1 if hdr["payload_type"] == PAYLOAD_TYPE_GRP_TXT else 2
        if len(payload) < lead + CIPHER_MAC:
            return
        self._frames.append(
            TextFrame(
                t=time.monotonic(),
                type=hdr["payload_type"],
                path_len=hdr["path_len"],
                path=hdr["path"],
                hash_size=hdr["hash_size"],
                hash=packet_hash(hdr),
                lead=bytes(payload[:lead]),
                cipher_len=len(payload) - lead - CIPHER_MAC,
                snr=snr,
                rssi=rssi,
                hdr=hdr,
            )
        )
        while len(self._frames) > FRAME_MAX:
            self._frames.popleft()

    def claim(self, msg: Message) -> Optional[TextFrame]:
        now = time.monotonic()
        while self._frames and now - self._frames[0].t > FRAME_MAX_AGE_S:
            self._frames.popleft()

        is_channel = msg.channel_idx is not None
        want_type = PAYLOAD_TYPE_GRP_TXT if is_channel else PAYLOAD_TYPE_TXT_MSG
        lengths = text_cipher_lengths(len(msg.text.encode("utf-8")), trailing_nul=not is_channel)
        want_lead: Optional[int] = None
        if is_channel:
            want_lead = self.channel_hashes.get(msg.channel_idx)
        elif msg.sender_prefix:
            want_lead = msg.sender_prefix[0]

        for i, frame in enumerate(self._frames):
            if frame.type != want_type or frame.path_len != msg.path_len:
                continue
            if frame.cipher_len not in lengths:
                continue
            if want_lead is not None:
                lead_byte = frame.lead[0] if is_channel else frame.lead[1]
                if lead_byte != want_lead:
                    continue
            del self._frames[i]
            return frame
        return None


def region_of(frame: Optional[TextFrame], keys: Dict[str, bytes]) -> str:
    return frame_region(frame.hdr, keys) if frame is not None else REGION_UNKNOWN
