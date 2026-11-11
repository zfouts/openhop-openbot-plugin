"""`!mpath`: every route one message took, as CoreScope's observers heard it.

This radio hears one copy of a message. CoreScope's observers hear many: a
flood reaches each of them over its own chain of relays, and copies keep
arriving for tens of seconds after the first one lands. So `{mpath}` cannot
answer at once. It acknowledges, waits `corescope.mpath_wait_s` for the flood
to settle, then asks CoreScope for every copy of the request's packet and
sends one frame: the counts, and a link to CoreScope's page for the packet,
which lists each observer and the path its copy took.

Three counting rules:

* A route is one observer and one distinct hop chain. An observer that heard
  three chains contributes three routes.
* A copy heard with no relays is a direct hearing: a route with 0 hops,
  counted as "direct".
* A copy whose chain is unknown is dropped, not counted as direct
  (services/corescope.py drops it).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .templates import MAX_TEXT_BYTES, clamp_bytes

log = logging.getLogger("openbot.mpath")

DEFAULT_WAIT_S = 30
MIN_WAIT_S = 5
MAX_WAIT_S = 300


def parse_wait(v: Any) -> Optional[int]:
    """Whole seconds in MIN_WAIT_S..MAX_WAIT_S, or None."""
    if isinstance(v, bool):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if MIN_WAIT_S <= n <= MAX_WAIT_S else None


def wait_s(cfg: Mapping[str, Any]) -> int:
    """`corescope.mpath_wait_s`, or the default when unset or out of range."""
    n = parse_wait((cfg.get("corescope") or {}).get("mpath_wait_s"))
    return DEFAULT_WAIT_S if n is None else n


def ack(sender: str, wait: int) -> str:
    return "@[%s] asking CoreScope's observers, back in %ds" % (sender, wait)


def routes(observations: Iterable[Dict[str, Any]]) -> List[Tuple[str, Tuple[str, ...]]]:
    """Distinct (observer, hop chain) pairs, in first-seen order."""
    seen: Dict[Tuple[str, Tuple[str, ...]], None] = {}
    for o in observations:
        seen.setdefault((o["observer"], tuple(o["hops"])), None)
    return list(seen)


def _n(count: int, word: str) -> str:
    return "%d %s%s" % (count, word, "" if count == 1 else "s")


def summary(observations: Iterable[Dict[str, Any]]) -> Optional[str]:
    """``9 routes to 6 observers, 1-5 hops, 2 direct``; None for no routes."""
    rs = routes(observations)
    if not rs:
        return None
    lens = [len(hops) for _, hops in rs]
    lo, hi = min(lens), max(lens)
    out = "%s to %s, %s hops" % (_n(len(rs), "route"), _n(len({o for o, _ in rs}), "observer"),
                                 lo if lo == hi else "%d-%d" % (lo, hi))
    direct = lens.count(0)
    if direct:
        out += ", %d direct" % direct
    return out


def _fits(text: str) -> bool:
    return len(text.encode("utf-8")) <= MAX_TEXT_BYTES


def report(corescope, shortener, sender: str, packet_hash: str) -> str:
    """The follow-up frame: ``@[sender] <summary> <link>``.

    The link is the short one when a shortener is set and answers, else the
    full one. A link is all or nothing: one that does not fit beside the
    summary is dropped whole, never cut."""
    head = "@[%s]" % sender
    try:
        obs = corescope.observations(packet_hash)
    except Exception as exc:
        log.warning("CoreScope packet lookup failed: %s", exc)
        return "%s CoreScope lookup failed, try again later" % head
    text = summary(obs or [])
    if text is None:
        return "%s no CoreScope observer reported hearing that" % head
    long_url = corescope.packet_url(packet_hash)
    links = []
    if shortener is not None:
        try:
            links.append(shortener.shorten(long_url))
        except Exception as exc:
            log.warning("link shortening failed, trying the long link: %s", exc)
    links.append(long_url)
    for link in links:
        out = "%s %s %s" % (head, text, link)
        if _fits(out):
            return out
    return clamp_bytes("%s %s" % (head, text))
