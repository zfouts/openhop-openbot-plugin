"""Resolve a MeshCore `!path` hop-hash chain to node names, locally.

A hop hash is only the leading 1-3 bytes of a repeater's public key, so one
hash can match several nodes. The hops are an ordered RF chain though:
consecutive repeaters are within LoRa range of each other, the chain ends at
this node (the receiver) and may start at the requester's known position.
Picking, per hop, the candidate that minimises total geographic path length
(Viterbi over the candidate sets, anchored at whichever ends we know) breaks
most prefix collisions with far-away same-prefix nodes.

Port of meshscope's resolver for a single node: synchronous, no database, and
no mesh attribution (there is only one mesh here). Kept: geography, the
far-segment penalty, a small recency penalty (a live repeater beats a stale
ghost) and the non-relay penalty (middle hops are almost always repeaters).
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable

MAX_HOPS = 64
# Per-hop candidate cap, most recently heard first.
MAX_CANDIDATES = 25

# Cost shaping, in km-equivalent units.
LINK_MAX_KM = 100.0
FAR_SEGMENT_FACTOR = 8.0
RECENCY_PENALTY_KM_PER_DAY = 0.5
RECENCY_PENALTY_MAX_KM = 30.0
NON_RELAY_PENALTY_KM = 150.0

_RELAY_TYPES = frozenset({"repeater", "unknown", "", None})
_HASH_RE = re.compile(r"^(?:[0-9a-f]{2}){1,3}$")

Point = tuple[float, float]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rad = math.radians
    h = (math.sin(rad(lat2 - lat1) / 2) ** 2
         + math.cos(rad(lat1)) * math.cos(rad(lat2))
         * math.sin(rad(lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def split_chain(hops_hex: str, hop_count: int) -> list[str]:
    """Split a concatenated hex chain into `hop_count` equal-width hashes.

    Raises ValueError when the chain is not hex, the count is out of range,
    or the length does not divide into 1-3 byte hashes."""
    if not isinstance(hops_hex, str) or not isinstance(hop_count, int) \
            or isinstance(hop_count, bool):
        raise ValueError("hops_hex must be str and hop_count int")
    if not 1 <= hop_count <= MAX_HOPS:
        raise ValueError(f"hop_count must be 1..{MAX_HOPS}")
    s = hops_hex.strip().lower()
    if not s or len(s) % 2 or any(c not in "0123456789abcdef" for c in s):
        raise ValueError("hop chain is not valid hex")
    if len(s) % hop_count:
        raise ValueError("hop chain length does not match hop count")
    width = len(s) // hop_count
    if width not in (2, 4, 6):
        raise ValueError("hop hashes must be 1-3 bytes")
    return [s[i:i + width] for i in range(0, len(s), width)]


def _seg_cost(dist_km: float) -> float:
    if dist_km > LINK_MAX_KM:
        return LINK_MAX_KM + (dist_km - LINK_MAX_KM) * FAR_SEGMENT_FACTOR
    return dist_km


def _has_pos(c: dict) -> bool:
    lat, lon = c.get("lat"), c.get("lon")
    if lat is None or lon is None:
        return False
    return not (lat == 0 and lon == 0)  # MeshCore's "not set" sentinel


def _heard(c: dict, now: float) -> float:
    h = c.get("last_heard")
    return min(float(h), now) if h else 0.0


def _load(prefix: str, candidates: Callable[[str], list[dict]], now: float) -> list[dict]:
    """Candidates for one hop: deduped by pubkey, freshest first, capped."""
    seen: dict[str, dict] = {}
    for c in candidates(prefix) or []:
        pk = str(c.get("pubkey") or "").lower()
        if not pk.startswith(prefix):
            continue
        c = dict(c, pubkey=pk)
        if pk not in seen or _heard(c, now) > _heard(seen[pk], now):
            seen[pk] = c
    out = sorted(seen.values(), key=lambda c: -_heard(c, now))
    return out[:MAX_CANDIDATES]


def _viterbi(hops: list[dict], known: list[int], start: Point | None,
             end: Point | None, now: float) -> None:
    """Set hops[i]["picked"] for each hop index in `known` (hops that have
    located candidates). Unlocated hops are skipped; neighbours connect
    directly."""
    for i in known:
        for c in hops[i]["located"]:
            heard = _heard(c, now)
            age_days = max(0.0, (now - heard) / 86_400) if heard else 365.0
            recency = min(age_days * RECENCY_PENALTY_KM_PER_DAY, RECENCY_PENALTY_MAX_KM)
            role = 0.0 if c.get("type") in _RELAY_TYPES else NON_RELAY_PENALTY_KM
            c["_pen"] = recency + role

    prev_costs: list[float] = []
    for ki, i in enumerate(known):
        cands = hops[i]["located"]
        costs, backs = [], []
        for ci, c in enumerate(cands):
            tie = ci * 1e-3 + c["_pen"]  # index term breaks exact ties
            if ki == 0:
                base = (_seg_cost(haversine_km(start[0], start[1], c["lat"], c["lon"]))
                        if start else 0.0)
                costs.append(base + tie)
                backs.append(-1)
            else:
                best, best_pi = float("inf"), -1
                for pi, p in enumerate(hops[known[ki - 1]]["located"]):
                    v = prev_costs[pi] + _seg_cost(
                        haversine_km(p["lat"], p["lon"], c["lat"], c["lon"]))
                    if v < best:
                        best, best_pi = v, pi
                costs.append(best + tie)
                backs.append(best_pi)
        hops[i]["_backs"] = backs
        prev_costs = costs

    if known:
        last = hops[known[-1]]["located"]
        totals = [
            prev_costs[k] + (_seg_cost(haversine_km(end[0], end[1], c["lat"], c["lon"]))
                             if end else 0.0)
            for k, c in enumerate(last)
        ]
        pick = min(range(len(totals)), key=totals.__getitem__)
        for ki in range(len(known) - 1, -1, -1):
            h = hops[known[ki]]
            h["picked"] = h["located"][pick]
            pick = h["_backs"][pick]


def resolve(hops: list[str], candidates: Callable[[str], list[dict]], *,
            start: Point | None = None, end: Point | None = None,
            now: float | None = None) -> dict:
    """Resolve hop hashes (travel order, sender first) to nodes.

    `candidates(prefix)` returns dicts {pubkey, name, type, lat, lon,
    last_heard} whose pubkey starts with `prefix`. `start` is the requester's
    position, `end` this node's. Raises ValueError for an empty chain, more
    than MAX_HOPS hops, or malformed / mixed-width hashes.
    """
    if not 1 <= len(hops) <= MAX_HOPS:
        raise ValueError(f"hop count must be 1..{MAX_HOPS}")
    norm = [str(h).strip().lower() for h in hops]
    if any(not _HASH_RE.match(h) for h in norm) or len({len(h) for h in norm}) != 1:
        raise ValueError("hops must be equal-width 1-3 byte hex hashes")
    now = time.time() if now is None else now

    state = []
    for h in norm:
        cands = _load(h, candidates, now)
        state.append({"hash": h, "cands": cands,
                      "located": [c for c in cands if _has_pos(c)]})
    known = [i for i, h in enumerate(state) if h["located"]]
    _viterbi(state, known, start, end, now)

    out_hops: list[dict] = []
    for h in state:
        n = len(h["cands"])
        sel = h.get("picked")
        if sel is None and n == 1:
            sel = h["cands"][0]  # lone match: resolves by name, no position
        if sel is None:
            out_hops.append({"hash": h["hash"], "name": None, "pubkey": None,
                             "lat": None, "lon": None, "candidates": n,
                             "ambiguous": n > 1})
            continue
        located = _has_pos(sel)
        out_hops.append({
            "hash": h["hash"],
            "name": sel.get("name") or sel["pubkey"],
            "pubkey": sel["pubkey"],
            "lat": sel["lat"] if located else None,
            "lon": sel["lon"] if located else None,
            "candidates": n,
            "ambiguous": n > 1,
        })

    pts: list[Point] = []
    if start:
        pts.append(start)
    pts += [(h["lat"], h["lon"]) for h in out_hops if h["lat"] is not None]
    if end:
        pts.append(end)
    seg = sum(haversine_km(a[0], a[1], b[0], b[1]) for a, b in zip(pts, pts[1:], strict=False))
    all_located = all(h["lat"] is not None for h in out_hops)
    result = {
        "hops": out_hops,
        "resolved": sum(1 for h in out_hops if h["name"] is not None),
        "total_km": None,
    }
    if len(pts) >= 2:
        if all_located:
            result["total_km"] = round(seg, 1)
        else:
            result["partial_km"] = round(seg, 1)
    elif not all_located:
        result["partial_km"] = None
    return result
