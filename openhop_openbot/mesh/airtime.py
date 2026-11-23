"""Estimated LoRa time on air for what the bot sends.

The companion reports the radio it transmits on (frequency, bandwidth,
spreading factor, coding rate) in SELF_INFO; the preamble length is not
reported, so it comes from ``node.preamble_symbols``. The packet size is
worked out from the text, since the companion builds the packet itself:

* channel text: header (1) | transport codes (4, when scoped) | path_len (1) |
  channel hash (1) | MAC (2) | AES blocks over ts (4) | flags (1) | "name: text"
* DM: header (1) | path_len (1) | dest (1) | src (1) | MAC (2) | AES blocks over
  ts (4) | flags (1) | text | NUL. A DM sent along a known route also carries
  that route's hop hashes, which the bot can not see; it is counted as none.
* advert: header (1) | path_len (1) | pubkey (32) | ts (4) | signature (64) |
  flags (1) | lat, lon (8, when set) | name

These are the bot's own transmissions only. Repeaters forward a flood again,
so the mesh carries each one several times over.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

DEFAULT_PREAMBLE = 16          # MeshCore modems; openHop's SX1262 driver defaults to 12
PREAMBLE_RANGE = (4, 64)


def _blocks(n: int) -> int:
    return -(-n // 16) * 16


def text_packet_bytes(text: str, *, node_name: str = "", dm: bool = False,
                      scoped: bool = False) -> int:
    body = len(text.encode("utf-8"))
    if dm:
        return 1 + 1 + 1 + 1 + 2 + _blocks(5 + body + 1)
    body += len(("%s: " % node_name).encode("utf-8")) if node_name else 0
    return 1 + (4 if scoped else 0) + 1 + 1 + 2 + _blocks(5 + body)


def advert_packet_bytes(name: str, has_position: bool) -> int:
    return 1 + 1 + 32 + 4 + 64 + 1 + (8 if has_position else 0) + len(name.encode("utf-8"))


def radio_known(radio: Optional[Mapping[str, Any]]) -> bool:
    return bool(radio) and 5 <= int(radio.get("sf") or 0) <= 12 \
        and int(radio.get("bw_hz") or 0) > 0 and int(radio.get("cr") or 0) > 0


def airtime_ms(nbytes: int, radio: Optional[Mapping[str, Any]],
               preamble: int = DEFAULT_PREAMBLE) -> Optional[float]:
    """Time on air for ``nbytes`` with the companion's radio settings, or None
    when they are not known yet."""
    if not radio_known(radio):
        return None
    from openhop_core.protocol.packet_utils import calculate_lora_airtime_ms

    return float(calculate_lora_airtime_ms(nbytes, int(radio["sf"]), int(radio["bw_hz"]),
                                           int(radio["cr"]), int(preamble)))
