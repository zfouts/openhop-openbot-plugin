"""Optional CoreScope lookup: candidate nodes for a `!path` hop chain.

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

Only the hop hashes are sent: no names, no positions. That is why plain
http:// is accepted, for an instance on the LAN.
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
NAME_MAX = 32

_HOP_RE = re.compile(r"^(?:[0-9a-f]{2}){1,3}$")
_KEY_RE = re.compile(r"^(?:[0-9a-f]{2}){4,32}$")

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
        url = "%s/api/resolve-hops?hops=%s" % (self.base, ",".join(norm))
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        try:
            status, data = self._get(url, headers, TIMEOUT_S)
        except CoreScopeError:
            raise
        except Exception as exc:
            raise CoreScopeError(str(exc) or type(exc).__name__) from exc
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
