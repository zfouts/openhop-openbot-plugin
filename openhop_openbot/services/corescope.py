"""Optional CoreScope lookups: hop candidates for `!path`, observers for `!mpath`.

CoreScope (a MeshCore analyzer with an open REST API) knows every node its
observers have heard, far more than one radio's roster. Its hop resolver
returns, per hop hash, every node whose public key starts with it:

    GET /api/resolve-hops?hops=7da9,ab12
    -> 200 {"resolved": {"7da9": {"name": ..., "pubkey": ...,
                                  "candidates": [{"name", "pubkey", "lat", "lon"}, ...]},
                         ...}}

Only the candidates are used. Which candidate each hop really is gets decided
locally (engine/pathresolve.py), by geography along the chain to this node,
so a same-prefix repeater 400 km away is not picked just because CoreScope
lists it first.

Its packet view lists every copy of one packet its observers heard, each with
the hop chain that copy carried:

    GET /api/packets/9ee40a871b8f697b
    -> 200 {"packet": {...}, "observation_count": 3,
            "observations": [{"observer_id", "observer_name", "snr",
                              "path_json": "[\\"9370\\",\\"BD7D\\"]", "direction"}, ...]}
    -> 404 when no observer heard it

For the Scope tab's region list it reads two views of MeshCore regions
(transport-code scopes, ``#name``). Neither is the observer-area list at
/api/config/regions, which holds IATA codes:

    GET /api/scope-stats?window=7d
    -> 200 {"byRegion": [{"name": "#tx-dfw", "count": 1203}, ...], ...}
       packets its observers matched to each region it holds a key for
    GET /api/scope-audit?window=1h
    -> 200 {"repeaters": [{"publicKey", "declaredRegions": ["tx-dfw", ...]}, ...]}
       what each repeater says it forwards (the window scopes only the
       forwarding counts, which are not used, and 1h is the cheap one)

Only hop hashes and packet hashes are sent: no names, no positions. That is
why plain http:// is accepted, for an instance on the LAN.
"""

from __future__ import annotations

import json
import logging
import math
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

log = logging.getLogger("openbot.corescope")

TIMEOUT_S = 5.0
MAX_BYTES = 1024 * 1024
MAX_CANDIDATES_PER_HOP = 50
MAX_HOPS = 64
MAX_OBSERVATIONS = 500
NAME_MAX = 32
MAX_REGIONS = 200
REGION_NAME_MAX = 32
AUDIT_TIMEOUT_S = 15.0      # the audit is computed on demand; seconds on a big instance

_HOP_RE = re.compile(r"^(?:[0-9a-f]{2}){1,3}$")
_KEY_RE = re.compile(r"^(?:[0-9a-f]{2}){4,32}$")
_PACKET_RE = re.compile(r"^[0-9a-f]{16}$")

Get = Callable[[str, dict, float], Tuple[int, Any]]


class CoreScopeError(RuntimeError):
    pass


def _read_json(fp) -> Any:
    raw = fp.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise CoreScopeError("response larger than %d bytes" % MAX_BYTES)
    try:
        return json.loads(raw) if raw else None
    except ValueError as exc:
        raise CoreScopeError("response is not JSON") from exc


def _default_get(url: str, headers: dict, timeout: float) -> Tuple[int, Any]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - operator-set URL
            return r.status, _read_json(r)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, _read_json(exc)
        except CoreScopeError:
            return exc.code, None


def _coord(v: Any, limit: float) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) and abs(f) <= limit else None


def _candidate(row: Any, prefix: str) -> Optional[Dict[str, Any]]:
    """One CoreScope candidate -> the resolver's candidate shape (None = skip)."""
    if not isinstance(row, dict):
        return None
    pk = row.get("pubkey")
    if not isinstance(pk, str):
        return None
    pk = pk.strip().lower()
    if not _KEY_RE.match(pk) or not pk.startswith(prefix):
        return None
    name = row.get("name")
    name = name.strip()[:NAME_MAX] or None if isinstance(name, str) else None
    lat, lon = _coord(row.get("lat"), 90.0), _coord(row.get("lon"), 180.0)
    if lat is None or lon is None or (lat == 0 and lon == 0):   # 0,0 = never set
        lat = lon = None
    # CoreScope's hop resolver carries no role or last-heard time.
    return {"pubkey": pk, "name": name, "type": None, "lat": lat, "lon": lon,
            "last_heard": None}


def region_name(v: Any) -> Optional[str]:
    """A region name as CoreScope reports it -> ``#name``, or None.

    The rules are CoreScope's own (regionNameAcceptable): structural, never
    about meaning. ``*`` is the flood wildcard, not a region; whitespace,
    commas, a second ``#`` or non-ASCII cannot be part of a name a radio
    hashes. Case is kept: ``#TX-DFW`` and ``#tx-dfw`` are different keys.
    ``$`` regions are private and have no key the bot can derive."""
    if not isinstance(v, str):
        return None
    name = v.strip()
    name = name[1:] if name.startswith("#") else name
    if not name or name == "*" or name.startswith("$") or len(name) > REGION_NAME_MAX:
        return None
    if any(c <= " " or c >= "\x7f" or c in ",#" for c in name):
        return None
    return "#" + name


def _count(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def _observation(row: Any) -> Optional[Dict[str, Any]]:
    """One CoreScope observation -> {observer, name, hops, snr} (None = skip).

    Skipped: an observer's own transmission (direction "tx"; it relayed the
    packet rather than heard it), a row naming no observer, and a row whose
    hop chain is missing or malformed. An unknown path is not a direct
    hearing, so it is dropped rather than counted as zero hops."""
    if not isinstance(row, dict) or str(row.get("direction") or "").lower() == "tx":
        return None
    oid = row.get("observer_id")
    name = row.get("observer_name")
    name = name.strip()[:NAME_MAX] or None if isinstance(name, str) else None
    observer = oid.strip().lower() if isinstance(oid, str) and oid.strip() else name
    if not observer:
        return None
    raw = row.get("path_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, list) or len(raw) > MAX_HOPS:
        return None
    hops = [h.strip().lower() for h in raw if isinstance(h, str)]
    if len(hops) != len(raw) or any(not _HOP_RE.match(h) for h in hops):
        return None
    snr = row.get("snr")
    snr = float(snr) if isinstance(snr, (int, float)) and not isinstance(snr, bool) \
        and math.isfinite(snr) else None
    return {"observer": observer, "name": name, "hops": hops, "snr": snr}


class CoreScope:
    def __init__(self, base_url: str, *, user_agent: str = "", get: Optional[Get] = None):
        base = (base_url or "").strip().rstrip("/")
        parts = urlsplit(base)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("CoreScope base_url must be an http:// or https:// URL")
        self.base = base
        self.user_agent = user_agent or "openhop-openbot"
        self._get = get or _default_get

    def resolve_hops(self, hops: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        """Candidates per hop hash, for the whole chain in one request.

        Raises ValueError for a malformed chain (before any request) and
        CoreScopeError for any network, HTTP or payload failure."""
        norm = [str(h).strip().lower() for h in hops]
        if not 1 <= len(norm) <= MAX_HOPS or any(not _HOP_RE.match(h) for h in norm) \
                or len({len(h) for h in norm}) != 1:
            raise ValueError("hops must be 1-%d equal-width 1-3 byte hex hashes" % MAX_HOPS)
        status, data = self._fetch("/api/resolve-hops?hops=" + ",".join(norm))
        resolved = data.get("resolved") if isinstance(data, dict) else None
        if status != 200 or not isinstance(resolved, dict):
            err = data.get("error") if isinstance(data, dict) else None
            raise CoreScopeError("HTTP %s %s" % (status, err or "unexpected response"))

        out: Dict[str, List[Dict[str, Any]]] = {}
        for h in norm:
            entry = resolved.get(h)
            rows = entry.get("candidates") if isinstance(entry, dict) else None
            if not isinstance(rows, list):
                rows = [entry] if isinstance(entry, dict) else []
            seen, cands = set(), []
            for row in rows:
                c = _candidate(row, h)
                if c and c["pubkey"] not in seen:
                    seen.add(c["pubkey"])
                    cands.append(c)
                    if len(cands) >= MAX_CANDIDATES_PER_HOP:
                        break
            out[h] = cands
        return out

    def packet_url(self, packet_hash: str) -> str:
        """CoreScope's page for one packet: every observer that heard it and
        the path each copy took."""
        return "%s/#/packet/%s" % (self.base, str(packet_hash).strip().upper())

    def observations(self, packet_hash: str) -> Optional[List[Dict[str, Any]]]:
        """Every copy of one packet CoreScope's observers heard, as
        ``[{observer, name, hops, snr}, ...]``; None when CoreScope has no
        record of the packet (HTTP 404).

        Raises ValueError for a malformed packet hash (before any request)
        and CoreScopeError for any other network, HTTP or payload failure."""
        h = str(packet_hash or "").strip().lower()
        if not _PACKET_RE.match(h):
            raise ValueError("packet hash must be 16 hex characters")
        status, data = self._fetch("/api/packets/" + h)
        if status == 404:
            return None
        if status != 200 or not isinstance(data, dict) or not isinstance(data.get("packet"), dict):
            err = data.get("error") if isinstance(data, dict) else None
            raise CoreScopeError("HTTP %s %s" % (status, err or "unexpected response"))
        rows = data.get("observations")
        if not isinstance(rows, list) or not rows:
            # No per-copy detail: the packet row carries the copy it was
            # first stored from.
            rows = [data["packet"]]
        out = []
        for row in rows[:MAX_OBSERVATIONS]:
            obs = _observation(row)
            if obs is not None:
                out.append(obs)
        return out

    def regions(self) -> Dict[str, Any]:
        """The MeshCore regions CoreScope knows, as
        ``{"regions": [{name, packets, repeaters}, ...], "partial": None | why}``.

        ``packets``: how many its observers matched to the region in the last
        7 days. ``repeaters``: how many repeaters declare it. Sorted by
        packets, then repeaters, then name; at most MAX_REGIONS.

        Traffic stats are required: without them (an older CoreScope, or any
        network or HTTP failure) this raises CoreScopeError. Declarations are
        not: when the audit fails the list carries traffic alone and
        ``partial`` is why (repeater counts are then all 0)."""
        status, data = self._fetch("/api/scope-stats?window=7d")
        rows = data.get("byRegion") if isinstance(data, dict) else None
        if status != 200 or not isinstance(rows, list):
            err = data.get("error") if isinstance(data, dict) else None
            raise CoreScopeError("region stats: HTTP %s %s"
                                 % (status, err or "unexpected response"))
        packets: Dict[str, int] = {}
        for row in rows:
            name = region_name(row.get("name")) if isinstance(row, dict) else None
            if name is not None:
                packets[name] = packets.get(name, 0) + _count(row.get("count"))

        repeaters: Dict[str, int] = {}
        partial = None
        try:
            status, data = self._fetch("/api/scope-audit?window=1h", AUDIT_TIMEOUT_S)
            reps = data.get("repeaters") if isinstance(data, dict) else None
            if status != 200 or not isinstance(reps, list):
                err = data.get("error") if isinstance(data, dict) else None
                raise CoreScopeError("HTTP %s %s" % (status, err or "unexpected response"))
            for rep in reps:
                declared = rep.get("declaredRegions") if isinstance(rep, dict) else None
                names = {region_name(n) for n in declared} if isinstance(declared, list) else ()
                for name in names:
                    if name is not None:
                        repeaters[name] = repeaters.get(name, 0) + 1
        except CoreScopeError as exc:
            partial = str(exc) or type(exc).__name__

        out = [{"name": n, "packets": packets.get(n, 0), "repeaters": repeaters.get(n, 0)}
               for n in set(packets) | set(repeaters)]
        out.sort(key=lambda r: (-r["packets"], -r["repeaters"], r["name"]))
        return {"regions": out[:MAX_REGIONS], "partial": partial}

    def _fetch(self, path: str, timeout: float = TIMEOUT_S) -> Tuple[int, Any]:
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        try:
            return self._get(self.base + path, headers, timeout)
        except CoreScopeError:
            raise
        except Exception as exc:
            raise CoreScopeError(str(exc) or type(exc).__name__) from exc
