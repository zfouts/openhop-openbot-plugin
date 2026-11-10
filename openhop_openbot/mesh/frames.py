"""On-wire MeshCore frame decoding: headers, regions, adverts, channel keys.

Everything here is pure and keyless except advert signature verification,
which uses openhop_core's Identity.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Dict, Mapping, Optional

# --------------------------------------------------------------------
# Channel secrets
# --------------------------------------------------------------------


def hashtag_secret(name: str) -> bytes:
    """The auto-derived key for a ``#``-prefixed public channel.

    MeshCore derives it from the name alone -- ``sha256(name)[:16]`` -- which
    is what lets any node join ``#bot`` without exchanging a secret. Verified
    live: a message sent with this key was decrypted and answered by a bot on
    the mesh. A name without ``#`` is not a hashtag channel and gets no key.
    """

    if not name.startswith("#"):
        raise ValueError(f"{name!r} is not a hashtag channel; supply an explicit secret")
    return hashlib.sha256(name.encode("ascii")).digest()[:16]


# --------------------------------------------------------------------
# Advert decoding from raw frames (§5.7)
# --------------------------------------------------------------------
#
# The companion protocol's advert push (PUSH_CODE_ADVERT) carries only the
# 32-byte public key -- no name, SNR, timestamp or path. Everything the
# contract needs is instead recovered by decoding the raw frame delivered by
# PUSH_CODE_LOG_RX_DATA, which also supplies the SNR/RSSI it was heard at.
#
# Adverts are signed, not encrypted, so this needs no keys.

_PAYLOAD_TYPE_ADVERT = 4
PAYLOAD_TYPE_TXT_MSG = 2
PAYLOAD_TYPE_GRP_TXT = 5
_PAYLOAD_TYPE_TRACE = 9

# Header bits[1:0]. The two TRANSPORT_* routes carry 4 bytes of transport
# codes between the header and path_len; the others do not.
_ROUTE_TRANSPORT_FLOOD = 0
_ROUTE_TRANSPORT_DIRECT = 3

# Text payloads: AES-128-ECB over ts(4) | flags(1) | text, zero-padded to the
# block, behind a 2-byte MAC (openhop_core.protocol.crypto, matching firmware).
CIPHER_BLOCK = 16
CIPHER_MAC = 2


def decode_frame_header(raw: bytes) -> Optional[Dict[str, Any]]:
    """Split an on-wire frame into header fields, path and payload.

    Layout: ``header(1) | [transport codes(4)] | path_len(1) | path | payload``
    where ``path_len`` packs ``hash_count`` into bits[5:0] and ``hash_size-1``
    into bits[7:6]. ``path`` is returned at full width: a 2-byte mesh gives
    two bytes per hop. Returns None for a frame too short to hold its header.
    """
    if len(raw) < 2:
        return None
    header = raw[0]
    route = header & 0x03
    payload_type = (header >> 2) & 0x0F
    pos = 1
    transport_codes = None
    if route in (_ROUTE_TRANSPORT_FLOOD, _ROUTE_TRANSPORT_DIRECT):
        if len(raw) < 5:
            return None
        transport_codes = (
            int.from_bytes(raw[1:3], "little"),
            int.from_bytes(raw[3:5], "little"),
        )
        pos += 4
    if len(raw) <= pos:
        return None
    path_len = raw[pos]
    hash_size = (path_len >> 6) + 1
    hop_count = path_len & 0x3F
    start = pos + 1
    end = start + hop_count * hash_size
    if len(raw) < end:
        return None
    return {
        "route": route,
        "transport_codes": transport_codes,
        "payload_type": payload_type,
        "path_len": path_len,
        "hash_size": hash_size,
        "hop_count": hop_count,
        "path": bytes(raw[start:end]),
        "payload": bytes(raw[end:]),
    }


def packet_hash(hdr: Mapping[str, Any]) -> str:
    """A frame's packet hash, from :func:`decode_frame_header`'s fields.

    Mirrors the firmware's ``Packet::calculatePacketHash``: SHA-256 over the
    payload type and the payload, first 8 bytes as hex, and for a TRACE its
    ``path_len`` too, as the uint16 the firmware keeps it in. The path is not
    hashed, so every relay copy of one transmission shares the value. It is
    the ``hash`` community brokers stamp on a packet.
    """
    h = hashlib.sha256(bytes([hdr["payload_type"]]))
    if hdr["payload_type"] == _PAYLOAD_TYPE_TRACE:
        h.update(int(hdr["path_len"]).to_bytes(2, "little"))
    h.update(hdr["payload"])
    return h.digest()[:8].hex()


def channel_hash_byte(secret: bytes) -> int:
    """The 1-byte channel hash a GRP_TXT payload starts with.

    Mirrors openhop_core's PacketBuilder / GroupTextHandler: sha256 of the
    key material, where a 32-byte key whose second half is zero hashes as
    its first 16 bytes.
    """

    key = bytes(secret)
    if len(key) >= 32 and key[16:32] == b"\x00" * 16:
        material = key[:16]
    else:
        material = key[:32] if len(key) > 32 else key
    return hashlib.sha256(material).digest()[0]


def text_cipher_lengths(text_bytes: int, trailing_nul: bool = False) -> range:
    """Ciphertext lengths a text of ``text_bytes`` bytes can encrypt to.

    Plaintext is ts(4) | flags(1) | text, and openhop_core appends a NUL to a
    direct message that firmware may not, so the range spans both.
    """
    lo = -(-(5 + text_bytes) // CIPHER_BLOCK) * CIPHER_BLOCK
    hi = -(-(6 + text_bytes) // CIPHER_BLOCK) * CIPHER_BLOCK if trailing_nul else lo
    return range(lo, hi + 1, CIPHER_BLOCK)


_ADV_FLAG_TYPE = 0x0F
_ADV_FLAG_HAS_LATLON = 0x10
_ADV_FLAG_HAS_NAME = 0x80

_ADV_TYPE_BY_ID = {0: "unknown", 1: "chat", 2: "repeater", 3: "room", 4: "sensor"}


def verify_advert_signature(body: bytes) -> bool:
    """Ed25519-verify an advert payload, exactly as openhop_core does.

    ``signed_region = pubkey + timestamp + appdata`` (i.e. everything except
    the signature itself), checked with the advertiser's own public key via
    openhop_core's Identity so MeshCore-specific key handling matches.

    This matters because LOG_RX_DATA hands us frames BEFORE openhop_core's
    own advert handler has verified them. Without this check an RF attacker
    could advert a real neighbour's pubkey with a forged name/position and it
    would become that node's retained heard/ row (second audit #1).
    """
    if len(body) < 101:
        return False
    try:
        from openhop_core.protocol import Identity

        return bool(Identity(body[0:32]).verify(body[0:36] + body[100:], body[36:100]))
    except Exception:
        return False


def decode_advert_frame(raw: bytes, *, verify: bool = True) -> Optional[Dict[str, Any]]:
    """Decode an on-wire advert frame, or None if it is not one.

    Returns None for a frame whose signature does not verify, unless
    ``verify=False`` (tests only). Adverts are signed, not encrypted, so
    verification needs no keys -- only the pubkey the advert itself carries.

    Layout (§5.7), offsets into the frame:

        0   header    1B   bits[1:0] route, bits[5:2] payload type, bits[7:6] ver
        1   path_len  1B   hash_size = (b >> 6) + 1;  hash_count = b & 0x3F
        2   path      hash_count * hash_size bytes, travel order
            --- advert payload ---
        +0    pubkey     32B
        +32   timestamp   4B  LE uint32, the advertiser's own clock
        +36   signature  64B
        +100  app flags   1B  0x0F type, 0x10 has lat/lon, 0x80 has name
        +101  latitude    4B  LE int32, degrees x 1e6   (iff 0x10)
        +105  longitude   4B  LE int32                  (iff 0x10)
        +109  name        rest                          (iff 0x80)

    The timestamp's absolute offset shifts with hop count, which is why the
    path has to be measured rather than assumed.
    """
    hdr = decode_frame_header(raw)
    if hdr is None or hdr["payload_type"] != _PAYLOAD_TYPE_ADVERT:
        return None

    body = hdr["payload"]
    if len(body) < 101:
        return None

    if verify and not verify_advert_signature(body):
        return None

    pubkey = body[0:32]
    timestamp = int.from_bytes(body[32:36], "little")
    flags = body[100]

    out: Dict[str, Any] = {
        "pubkey": pubkey,
        "contact_type": _ADV_TYPE_BY_ID.get(flags & _ADV_FLAG_TYPE, "unknown"),
        "timestamp": timestamp,
        # Full-width hop hashes, in travel order. On a 2-byte mesh each hop
        # is two bytes of the relay's pubkey; truncating to one byte would
        # give a chain that cannot be resolved.
        "path": hdr["path"],
        "hash_size": hdr["hash_size"],
        "raw": raw.hex(),
    }

    offset = 101
    if flags & _ADV_FLAG_HAS_LATLON and len(body) >= offset + 8:
        lat = int.from_bytes(body[offset : offset + 4], "little", signed=True) / 1e6
        lon = int.from_bytes(body[offset + 4 : offset + 8], "little", signed=True) / 1e6
        out["latitude"] = lat
        out["longitude"] = lon
        offset += 8

    if flags & _ADV_FLAG_HAS_NAME and len(body) > offset:
        out["node_name"] = (
            body[offset:].split(b"\x00")[0].decode("utf-8", errors="replace").strip()
        )

    return out


# --------------------------------------------------------------------
# Regions (transport-code scopes)
# --------------------------------------------------------------------
#
# A region-scoped flood is sent with route TRANSPORT_FLOOD and carries
# transport_codes[0] = HMAC-SHA256(region_key, payload_type | payload)[:2]
# as a LE uint16 (0 and 0xFFFF are reserved and remapped). The region key is
# sha256("#name")[:16]. This mirrors openhop_core.protocol.transport_keys and
# firmware TransportKey::calcTransportCode; a test pins it to openhop_core.

ROUTE_TRANSPORT_FLOOD = _ROUTE_TRANSPORT_FLOOD
ROUTE_FLOOD = 1
ROUTE_DIRECT = 2
ROUTE_TRANSPORT_DIRECT = _ROUTE_TRANSPORT_DIRECT

REGION_UNSCOPED = "*"
REGION_UNKNOWN = "?"
REGION_DIRECT = "direct"


def canonical_region(name: str) -> str:
    """``tx-dfw`` and ``#tx-dfw`` are the same region; return the ``#`` form."""
    name = (name or "").strip()
    if not name or name in (REGION_UNSCOPED, REGION_UNKNOWN, REGION_DIRECT):
        return name
    return name if name.startswith(("#", "$")) else "#" + name


def region_key(name: str) -> bytes:
    """The 16-byte transport key for a named (auto-key) region."""
    canonical = canonical_region(name)
    if not canonical or canonical.startswith("$"):
        raise ValueError(f"no auto key for region {name!r}")
    return hashlib.sha256(canonical.encode("ascii")).digest()[:16]


def transport_code(key: bytes, payload_type: int, payload: bytes) -> int:
    digest = hmac.new(key, bytes([payload_type & 0x0F]) + payload, hashlib.sha256).digest()
    code = int.from_bytes(digest[:2], "little")
    if code == 0:
        return 1
    if code == 0xFFFF:
        return 0xFFFE
    return code


def frame_region(hdr: Optional[Dict[str, Any]], keys: Dict[str, bytes]) -> str:
    """Which region a decoded frame arrived under.

    ``keys`` maps canonical region names to their transport keys. Returns a
    region name, or one of REGION_UNSCOPED / REGION_UNKNOWN / REGION_DIRECT.
    """
    if hdr is None:
        return REGION_UNKNOWN
    route = hdr["route"]
    if route in (ROUTE_DIRECT, ROUTE_TRANSPORT_DIRECT):
        return REGION_DIRECT
    if route == ROUTE_FLOOD:
        return REGION_UNSCOPED
    codes = hdr.get("transport_codes")
    if not codes:
        return REGION_UNKNOWN
    for name, key in keys.items():
        if transport_code(key, hdr["payload_type"], hdr["payload"]) == codes[0]:
            return name
    return REGION_UNKNOWN
