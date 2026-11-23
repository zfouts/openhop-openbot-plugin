"""Which MeshCore regions this radio actually hears.

A region-scoped packet carries transport code
``HMAC-SHA256(region_key, payload_type | payload)[:2]``. The radio cannot
read the region name off the air; it can only test the code against the
keys of regions it has been told about. This tests every scoped packet
against two lists:

- enabled: the regions in the config (``regions``), tested first. A match
  here is the region the bot also uses to scope its replies.
- candidates: regions CoreScope knows that are not enabled, so a region can
  be seen live before anyone ticks it. A packet two candidates both match
  counts for neither.

The code is 16 bits, so a key matches an unrelated packet once in 65536. On
a busy mesh that is several false matches per region per week, which is why
a count alone never verifies a region. ``verified`` asks for at least
MIN_PACKETS separate packets in the window AND a count chance would produce
less than once in a million (Poisson, with the expected number of chance
matches set by every scoped packet heard). Relay copies of one packet share
its packet hash and count once.

Counts are held in memory and written in one transaction by ``flush``
(``summary`` flushes first), not one write per packet.
"""

from __future__ import annotations

import logging
import math
import time
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Mapping, Optional

from ..services.corescope import region_name
from .frames import packet_hash, region_key, transport_code

log = logging.getLogger("openbot.regions")

MAX_CANDIDATES = 64          # each costs one HMAC per scoped packet heard
WINDOW_DAYS = 7
MIN_PACKETS = 3
CHANCE_P = 1e-6
CODE_SPACE = 65536
SEEN_MAX = 4096              # packet hashes remembered, to count relay copies once
TOTAL = ""                   # store row counting every scoped packet


def chance_tail(k: int, lam: float) -> float:
    """P(X >= k) for X ~ Poisson(lam): how likely ``k`` or more chance matches are."""
    if k <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    # Sum the tail directly: 1 - CDF loses everything below ~1e-16. Terms
    # shrink once i passes lam, so a bounded walk past both is exact enough.
    total, i = 0.0, k
    while i < k + 200 or i < lam + 200:
        term = math.exp(-lam + i * math.log(lam) - math.lgamma(i + 1))
        total += term
        if i > lam and term < total * 1e-17:
            break
        i += 1
    return min(1.0, total)


def verified(packets: int, scoped_total: int) -> bool:
    """Enough separate packets matched a region that chance cannot explain them."""
    if packets < MIN_PACKETS:
        return False
    return chance_tail(packets, scoped_total / CODE_SPACE) < CHANCE_P


class RegionWatch:
    def __init__(self, store: Any) -> None:
        self.store = store
        self.enabled: Dict[str, bytes] = {}
        self.candidates: Dict[str, bytes] = {}
        self._candidate_names: List[str] = []
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._pending: Dict[tuple, List[float]] = {}     # (day, region) -> [packets, last]

    # ------------------------------------------------------------ lists ----

    def set_enabled(self, keys: Mapping[str, bytes]) -> None:
        self.enabled = dict(keys)
        self._build_candidates()

    def set_candidates(self, names: Iterable[str]) -> None:
        """CoreScope's regions, most relevant first; the first MAX_CANDIDATES
        not already enabled are tested."""
        self._candidate_names = [n for n in names if isinstance(n, str)]
        self._build_candidates()

    def _build_candidates(self) -> None:
        out: Dict[str, bytes] = {}
        for raw in self._candidate_names:
            name = region_name(raw)
            if name is None or name in self.enabled or name in out:
                continue
            out[name] = region_key(name)
            if len(out) >= MAX_CANDIDATES:
                break
        self.candidates = out

    def watching(self) -> List[str]:
        return sorted(set(self.enabled) | set(self.candidates))

    # --------------------------------------------------------- matching ----

    def match(self, hdr: Mapping[str, Any]) -> Optional[str]:
        codes = hdr.get("transport_codes")
        if not codes:
            return None
        code, pt, payload = codes[0], hdr["payload_type"], hdr["payload"]
        for name, key in self.enabled.items():
            if transport_code(key, pt, payload) == code:
                return name
        hits = [n for n, key in self.candidates.items() if transport_code(key, pt, payload) == code]
        return hits[0] if len(hits) == 1 else None

    def observe(self, hdr: Optional[Mapping[str, Any]], ts: Optional[float] = None
                ) -> Optional[str]:
        """Count one heard frame. Returns the region it was matched to, if any.

        Only frames carrying transport codes are scoped. A code of 0 or 0xFFFF
        is reserved (no key can produce it), so it is not counted at all. A
        relay copy of a packet already counted returns None."""
        codes = hdr.get("transport_codes") if hdr else None
        if not codes or codes[0] in (0, 0xFFFF):
            return None
        h = packet_hash(hdr)
        if h in self._seen:
            return None
        self._seen[h] = None
        while len(self._seen) > SEEN_MAX:
            self._seen.popitem(last=False)
        ts = time.time() if ts is None else ts
        name = self.match(hdr)
        self._count(TOTAL, ts)
        if name is not None:
            self._count(name, ts)
        return name

    def _count(self, region: str, ts: float) -> None:
        row = self._pending.setdefault((int(ts // 86400), region), [0, ts])
        row[0] += 1
        row[1] = max(row[1], ts)

    def flush(self) -> None:
        """Write the counts held since the last flush."""
        pending, self._pending = self._pending, {}
        try:
            self.store.add_region_counts((day, region, int(n), last)
                                         for (day, region), (n, last) in pending.items())
        except Exception:
            for k, (n, last) in pending.items():       # keep them for the next try
                row = self._pending.setdefault(k, [0, last])
                row[0] += n
                row[1] = max(row[1], last)
            raise

    # ---------------------------------------------------------- summary ----

    def summary(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = time.time() if now is None else now
        try:
            self.flush()
        except Exception as exc:        # counts stay pending; report what is stored
            log.warning("could not save region counts: %s", exc)
        since = (int(now // 86400) - (WINDOW_DAYS - 1)) * 86400
        counts = self.store.region_counts(since)
        total = int((counts.pop(TOTAL, None) or {}).get("packets") or 0)
        heard = [{"name": name, "packets": int(c["packets"] or 0),
                  "last_heard": int(c["last"]) if c["last"] is not None else None,
                  "verified": verified(int(c["packets"] or 0), total)}
                 for name, c in counts.items()]
        heard.sort(key=lambda r: (-r["packets"], -(r["last_heard"] or 0), r["name"]))
        return {"window_days": WINDOW_DAYS, "scoped_packets": total,
                "watching": self.watching(), "heard": heard}
