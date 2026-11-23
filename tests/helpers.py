"""Builders for on-wire frames shaped like what the radio logs."""

from __future__ import annotations

import os

from openhop_openbot.mesh.companion import Message
from openhop_openbot.mesh.frames import (
    channel_hash_byte,
    hashtag_secret,
    region_key,
    text_cipher_lengths,
    transport_code,
)

GRP_TXT = 5


def chan_hash(name: str) -> int:
    return channel_hash_byte(hashtag_secret(name))


TS = 1_700_000_000


def grp_payload(text: str, channel: str = "#bot", ts: int = TS) -> bytes:
    """A genuinely encrypted GRP_TXT payload, as firmware sends it."""
    from openhop_core.protocol.crypto import CryptoUtils

    key = hashtag_secret(channel).ljust(32, b"\x00")
    plain = ts.to_bytes(4, "little") + b"\x00" + text.encode("utf-8")
    return bytes([chan_hash(channel)]) + CryptoUtils.encrypt_then_mac(key[:16], key, plain)


def grp_frame(text: str, channel: str = "#bot", *, path: bytes = b"", hash_size: int = 2,
              region: str | None = None, route: int | None = None,
              real: bool = False, ts: int = TS) -> tuple[bytes, int]:
    """A GRP_TXT frame for ``text`` on ``channel``. Returns (raw, path_len byte).

    ``real`` encrypts the text with the channel's key (sent at ``ts``);
    otherwise the ciphertext is random bytes of the right length."""
    if real:
        payload = grp_payload(text, channel, ts)
    else:
        cipher_len = text_cipher_lengths(len(text.encode("utf-8")))[0]
        payload = bytes([chan_hash(channel)]) + b"\xaa\xbb" + os.urandom(cipher_len)
    if route is None:
        route = 0 if region else 1
    header = route | (GRP_TXT << 2)
    tc = b""
    if route in (0, 3):
        code = transport_code(region_key(region), GRP_TXT, payload) if region else 0x1234
        tc = code.to_bytes(2, "little") + b"\x00\x00"
    path_len = ((hash_size - 1) << 6) | (len(path) // hash_size)
    return bytes([header]) + tc + bytes([path_len]) + path + payload, path_len


def channel_msg(text: str, path_len: int, idx: int = 1, snr: float = 6.5) -> Message:
    return Message(text=text, timestamp=TS, snr=snr, path_len=path_len,
                   channel_idx=idx)
