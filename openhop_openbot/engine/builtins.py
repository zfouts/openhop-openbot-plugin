"""Built-in template functions.

Each takes ``(ctx, arg)`` and returns a short string. ``ctx`` carries the
request (sender, args, channel, region, hop chain, snr), the node's own
state, the local ``store``, and the ``weather`` (Open-Meteo), ``geocoder``
(Nominatim) and optional ``corescope`` (CoreScope hop lookup) clients.
Built-ins run on a worker thread, so blocking HTTP is fine; they must not
touch asyncio.

Built-ins answer from what this one radio has heard:

* ``{path}`` (alias ``{pathbot}``) answers with the hop hashes and, with
  CoreScope configured, a short link to the route on CoreScope's map.
  ``{pathnames}`` resolves the chain to names and distance instead, against
  this node's roster (plus CoreScope's nodes, when configured).
* ``{seen}``, ``{stats}``, ``{top_relays}`` and the rest count only what this
  radio heard.
"""

from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from .. import __version__
from . import pathresolve
from .templates import MAX_TEXT_BYTES, clamp_bytes

log = logging.getLogger("openbot.builtins")

# The CLI command is the part that has to survive
# the frame limit, so a long sender name costs the prose, not the fix.
HASH_HINT_COOLDOWN_S = 900
HASH_HINT = ("1-byte path hashes leave hops ambiguous. Please enable 2-byte"
             " (companion Settings > Experimental) or CLI: set path.hash.mode 1")
HASH_HINT_SHORT = "please enable 2-byte path hashes. CLI: set path.hash.mode 1"

EIGHTBALL = ["it is certain", "without a doubt", "most likely", "outlook good",
             "ask again later", "cannot predict now", "don't count on it",
             "very doubtful", "my sources say no", "signs point to yes"]


def fits(text: str) -> bool:
    return len(text.encode("utf-8")) <= MAX_TEXT_BYTES


def fmt_dist(km: Optional[float], units: str) -> str:
    if km is None:
        return ""
    return ("%.1fkm" % km) if units == "metric" else ("%.1fmi" % (km * 0.621371))


def rel_age(epoch: Optional[float], now: Optional[float] = None) -> str:
    if not epoch:
        return "never"
    d = max(0, (now or time.time()) - epoch)
    if d < 90:
        return "%ds ago" % d
    if d < 5400:
        return "%dm ago" % (d / 60)
    if d < 172800:
        return "%dh ago" % (d / 3600)
    return "%dd ago" % (d / 86400)


def fmt_span(s: float) -> str:
    s = int(max(0, s))
    if s < 3600:
        return "%dm" % (s // 60)
    if s < 86400:
        return "%dh%02dm" % (s // 3600, (s % 3600) // 60)
    return "%dd%02dh" % (s // 86400, (s % 86400) // 3600)


def _arg(ctx: Dict[str, Any], arg: Optional[str]) -> str:
    return (arg if arg is not None else ctx.get("args") or "").strip()


def _now_local(ctx: Dict[str, Any]) -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(ctx.get("tz") or "UTC"))
    except Exception:
        return datetime.utcnow()


def hash_hint(sender: str) -> str:
    full = "@[%s] %s" % (sender, HASH_HINT)
    return full if fits(full) else clamp_bytes("@[%s] %s" % (sender, HASH_HINT_SHORT))


# ----------------------------------------------------------------- path ----

def _hop_label(hop: Dict[str, Any], width: int) -> str:
    name = hop.get("name")
    if not name:
        return hop["hash"] + ("?" if hop.get("candidates") else "")
    return name[:width] if width else name


ELLIPSIS = "\u2026"


def _path_start(ctx: Dict[str, Any]):
    """The shared start of {path} and {pathnames}.

    Returns (sender, hops_n, hops, reply). A reply that is not None answers
    the request outright: no path info, heard direct, or hashes unavailable.
    Otherwise queues the 1-byte-hash hint when it applies."""
    sender = ctx.get("sender") or "?"
    hops_n = ctx.get("hops_n")
    hops = [str(h).lower() for h in ctx.get("hops") or []]
    if hops_n is None:
        return sender, hops_n, hops, "@[%s] no path info on that message" % sender
    if hops_n == 0:
        snr = ctx.get("snr")
        return sender, hops_n, hops, "@[%s] heard you direct, no relays%s" % (
            sender, "" if snr is None else " (snr %s)" % snr)
    if not hops:
        return sender, hops_n, hops, "@[%s] [%dh] hop hashes unavailable for that copy" % (
            sender, hops_n)
    if ctx.get("hash_size") == 1 and ctx.get("extra") is not None:
        if ctx["store"].claim("_hint1b", sender, HASH_HINT_COOLDOWN_S):
            ctx["extra"].append(hash_hint(sender))
    return sender, hops_n, hops, None


def map_link(ctx: Dict[str, Any], hops: List[str]) -> Optional[str]:
    """CoreScope's map view of this request's route, or None without a
    CoreScope URL or a packet hash (the map loads the packet by its hash)."""
    cs, pkt = ctx.get("corescope"), ctx.get("packet_hash")
    if cs is None or not pkt:
        return None
    return "%s/#/map?packet=%s&hops=%s" % (cs.base, str(pkt).upper(), ",".join(hops))


def _fit_hashes(head: str, hops: List[str], link: Optional[str], elide: bool) -> Optional[str]:
    """``head h1,h2,... link`` in one frame. With ``elide``, hashes are dropped
    from the middle (first and last kept, joined by an ellipsis) until it fits.
    None when it cannot fit."""
    tail = " " + link if link else ""
    out = "%s %s%s" % (head, ",".join(hops), tail)
    if fits(out) or not elide:
        return out if fits(out) else None
    n = len(hops)
    for keep in range(n - 1, 1, -1):
        a = (keep + 1) // 2
        chain = ",".join(hops[:a]) + ELLIPSIS + ",".join(hops[n - (keep - a):])
        out = "%s %s%s" % (head, chain, tail)
        if fits(out):
            return out
    return None


def fn_path(ctx: Dict[str, Any], arg: Optional[str]) -> str:
    """The request's own RF path: hop count, hop hashes in travel order and,
    with CoreScope configured, a short link to the route on its map.

    Link fallbacks: the short link may cost hashes from the middle of the
    chain; the long URL (no shortener selected, or shortening failed) is used
    only if it fits beside every hash; otherwise the reply goes without a
    link."""
    sender, hops_n, hops, early = _path_start(ctx)
    if early is not None:
        return early
    head = "@[%s] [%dh]" % (sender, hops_n)
    attempts = []
    long_url = map_link(ctx, hops)
    shortener = ctx.get("shortener")
    if long_url and shortener is None:
        attempts.append((long_url, False))
    elif long_url:
        try:
            attempts.append((shortener.shorten(long_url), True))
        except Exception as exc:
            log.warning("link shortening failed, trying the long link: %s", exc)
            attempts.append((long_url, False))
    attempts.append((None, True))
    for link, elide in attempts:
        out = _fit_hashes(head, hops, link, elide)
        if out:
            return out
    return clamp_bytes("%s %s" % (head, ",".join(hops)))


def fn_pathnames(ctx: Dict[str, Any], arg: Optional[str]) -> str:
    """Trace the request's own RF path: hop names, hop count, distance."""
    sender, hops_n, hops, early = _path_start(ctx)
    if early is not None:
        return early
    store = ctx["store"]
    candidates = store.candidates
    if ctx.get("corescope") is not None:
        candidates = _with_corescope(ctx["corescope"], hops, store.candidates)
    res = pathresolve.resolve(
        hops, candidates,
        start=store.position_of(sender),
        end=ctx.get("self_pos"),
    )
    head = "@[%s] [%dh]" % (sender, hops_n)
    km = res.get("total_km")
    if km is not None:
        head += " ~" + fmt_dist(km, ctx.get("units", "imperial"))
    elif res.get("partial_km"):
        head += " >" + fmt_dist(res["partial_km"], ctx.get("units", "imperial"))
    # Widest labels that still fit one frame: full names, then 10, then 6
    # characters, then bare hashes.
    for width in (0, 10, 6):
        chain = ">".join(_hop_label(h, width) for h in res["hops"])
        out = "%s %s" % (head, chain)
        if fits(out):
            return out
    return clamp_bytes("%s %s" % (head, ">".join(h["hash"] for h in res["hops"])))


def _with_corescope(corescope, hops: List[str], local):
    """The local roster plus CoreScope's candidates for this chain, fetched in
    one request. What this radio heard wins on name, role and recency;
    CoreScope adds the nodes it never heard and fills positions it lacks.
    On any failure, the local roster alone."""
    try:
        remote = corescope.resolve_hops(hops)
    except Exception as exc:
        log.warning("CoreScope lookup failed, resolving from the local roster: %s", exc)
        return local

    def candidates(prefix: str) -> List[Dict[str, Any]]:
        rows = [dict(r) for r in local(prefix) or []]
        mine = {str(r.get("pubkey") or "").lower(): r for r in rows}
        for c in remote.get(prefix) or []:
            row = mine.get(c["pubkey"])
            if row is None:
                rows.append(dict(c))
                continue
            if (row.get("lat") is None or row.get("lon") is None) and c["lat"] is not None:
                row["lat"], row["lon"] = c["lat"], c["lon"]
            if not row.get("name") and c["name"]:
                row["name"] = c["name"]
        return rows

    return candidates


TOOK_MAX_S = 3600


def fn_took(ctx: Dict[str, Any], arg: Optional[str]) -> str:
    """Seconds from the sender's own timestamp on the message to when this
    node heard it, to one decimal.

    The sender's timestamp is whole seconds from the sender's clock, so this
    is good to about a second. "?" when there is no timestamp, or when the
    sender's clock is plainly wrong: ahead of ours, or over an hour behind."""
    sent, heard = ctx.get("sent_ts"), ctx.get("heard_at")
    if not sent or heard is None:
        return "?"
    took = float(heard) - float(sent)
    if took < 0 or took > TOOK_MAX_S:
        return "?"
    return "%.1f" % took


def fn_route(ctx: Dict[str, Any], arg: Optional[str]) -> str:
    hops = ctx.get("hops") or []
    if ctx.get("hops_n") == 0:
        return "direct"
    return ">".join(hops) if hops else "?"


# --------------------------------------------------------------- roster ----

def fn_seen(ctx, arg):
    name = _arg(ctx, arg)
    if not name:
        return "seen who?"
    row = ctx["store"].find_name(name)
    if not row:
        return "no node matching '%s'" % name[:24]
    extra = ""
    if row.get("last_hops") is not None:
        extra = " %dh" % row["last_hops"]
    return "%s (%s) %s%s" % (row["name"], row["type"] or "?", rel_age(row["last_heard"]), extra)


def fn_whois(ctx, arg):
    h = _arg(ctx, arg).lower()
    if not re.fullmatch(r"[0-9a-f]{2,8}", h):
        return "whois <hex-prefix>"
    names = ctx["store"].whois(h)
    return ", ".join(names) if names else h + ": no match"


def fn_stats(ctx, arg):
    c = ctx["store"].counts()
    return "%d nodes / %d repeaters / %d msgs 24h" % (c["nodes"], c["repeaters"], c["msgs24"])


def fn_online(ctx, arg):
    since = int(time.time()) - 3600
    s = ctx["store"]
    return "%d nodes heard in 1h (%d repeaters)" % (
        s.heard_since(since), s.heard_since(since, "repeater"))


def fn_new_nodes(ctx, arg):
    names = ctx["store"].new_since(int(time.time()) - 86400)
    if not names:
        return "no new nodes in 24h"
    return clamp_bytes("new 24h: " + ", ".join(names), MAX_TEXT_BYTES - 20)


def fn_lastmsg(ctx, arg):
    name = _arg(ctx, arg)
    if not name:
        return "lastmsg who?"
    m = ctx["store"].last_msg(name)
    if not m:
        return "nothing heard from '%s'" % name[:24]
    return clamp_bytes("%s %s in %s: %s" % (m["sender"], rel_age(m["ts"]), m["channel"], m["text"]))


def fn_top_talkers(ctx, arg):
    n = min(int(arg), 5) if (arg or "").strip().isdigit() else 3
    rows = ctx["store"].top_talkers(int(time.time()) - 86400, n)
    return " / ".join("%s(%d)" % r for r in rows) if rows else "no traffic 24h"


def fn_top_relays(ctx, arg):
    n = min(int(arg), 5) if (arg or "").strip().isdigit() else 3
    rows = ctx["store"].top_relays(int(time.time()) - 86400, n)
    return " / ".join("%s(%d)" % r for r in rows) if rows else "no relay data"


# -------------------------------------------------------------- weather ----

WX_USAGE = "wx <zip, city or place>"


def fn_forecast(ctx, arg):
    """`!wx 76131` / `!wx Karlsruhe` / `!wx 32.9,-97.3`: Open-Meteo forecast there."""
    w, geo = ctx.get("weather"), ctx.get("geocoder")
    if w is None or geo is None:
        return "weather disabled"
    q = _arg(ctx, arg)
    if not q:
        return WX_USAGE
    try:
        place = geo.lookup(q)
    except Exception:
        return "location lookup unavailable, try later"
    if place is None:
        return "couldn't find '%s'" % q[:32]
    from ..services.openmeteo import pick_units

    wcfg = (ctx.get("cfg") or {}).get("weather") or {}
    units = pick_units(wcfg.get("units") or "auto", place.country, ctx.get("units") or "metric")
    out = w.forecast_text(place.lat, place.lon, units=units,
                          limit_bytes=MAX_TEXT_BYTES - len(place.label.encode()) - 2)
    return "%s: %s" % (place.label, out)


# -------------------------------------------------------------- utility ----

def fn_roll(ctx, arg):
    a = _arg(ctx, arg).lower() or "1d6"
    m = re.fullmatch(r"(\d{0,2})d(\d{1,4})", a)
    if m:
        n, sides = int(m.group(1) or 1), int(m.group(2))
        if not 1 <= n <= 20 or sides < 2:
            return "roll NdM (N<=20)"
        rolls = [random.randint(1, sides) for _ in range(n)]
        return "%s: %s" % (a, rolls[0] if n == 1 else "%s = %d" % (
            "+".join(map(str, rolls)), sum(rolls)))
    if a.isdigit() and int(a) >= 2:
        return str(random.randint(1, int(a)))
    return "roll NdM or roll N"


def fn_pick(ctx, arg):
    a = _arg(ctx, arg)
    opts = [o.strip() for o in (a.split(",") if "," in a else a.split()) if o.strip()]
    return random.choice(opts) if opts else "pick a, b, c"


def fn_help(ctx, arg):
    cmds = [c.get("trigger", "") for c in ctx.get("commands") or [] if c.get("trigger")]
    if not cmds:
        return "no commands"
    out = "cmds: " + " ".join(cmds)
    return out if fits(out) else clamp_bytes(out)


def fn_uptime(ctx, arg):
    return "up " + fmt_span(time.time() - (ctx.get("started_at") or time.time()))


def _ctx(key: str, default: str = ""):
    def fn(ctx, arg):
        v = ctx.get(key)
        return default if v is None else str(v)
    return fn


def fn_region_tag(ctx, arg):
    r = ctx.get("region") or ""
    return " [%s]" % r if r.startswith(("#", "$")) else ""


BUILTINS = {
    # request context
    "sender": _ctx("sender"),
    "args": _ctx("args"),
    "channel": _ctx("channel"),
    "region": _ctx("region"),
    "region_tag": fn_region_tag,
    "gateway": _ctx("gateway"),
    "snr": _ctx("snr", "?"),
    "rssi": _ctx("rssi", "?"),
    "hops": _ctx("hops_n", "?"),
    "time": lambda ctx, a: _now_local(ctx).strftime("%H:%M %Z").strip(),
    "date": lambda ctx, a: _now_local(ctx).strftime("%Y-%m-%d"),
    # path
    "path": fn_path,
    "pathbot": fn_path,
    "pathnames": fn_pathnames,
    "took": fn_took,
    "route": fn_route,
    # roster
    "seen": fn_seen,
    "whois": fn_whois,
    "stats": fn_stats,
    "online": fn_online,
    "new_nodes": fn_new_nodes,
    "lastmsg": fn_lastmsg,
    "top_talkers": fn_top_talkers,
    "top_relays": fn_top_relays,
    # weather
    "forecast": fn_forecast,
    "wx": fn_forecast,
    # utility
    "roll": fn_roll,
    "pick": fn_pick,
    "8ball": lambda ctx, a: random.choice(EIGHTBALL),
    "help": fn_help,
    "uptime": fn_uptime,
    "version": lambda ctx, a: "openbot " + __version__,
}

HELP = {
    "sender": "Name of whoever sent the command",
    "args": "Text after the trigger",
    "channel": "Channel the request arrived on ('dm' for direct)",
    "region": "Region it arrived under: #name, * (unscoped), ? or direct",
    "region_tag": "' [#name]' when the request was region-scoped, else empty",
    "gateway": "This bot's node name",
    "snr": "SNR the request was heard at",
    "rssi": "RSSI the request was heard at",
    "hops": "Hop count of the request",
    "time": "Local time (node timezone)",
    "date": "Local date",
    "path": "Hop count and hop hashes of the request (!path); with CoreScope configured,"
            " plus a short link to the route on its map",
    "pathbot": "Alias of {path}",
    "pathnames": "Resolve the request's hop chain to names + distance; with CoreScope"
                 " configured, against every node it has heard",
    "took": "Seconds from the sender's message timestamp to when this node heard it"
            " (? if the sender's clock is off)",
    "route": "Raw hop hash chain, e.g. a1b2>c3d4",
    "seen": "{seen NAME}: when a node was last heard",
    "whois": "{whois HEX}: node names for a hop hash prefix",
    "stats": "Nodes, repeaters and messages heard in 24h",
    "online": "Nodes heard in the last hour",
    "new_nodes": "Nodes first heard in 24h",
    "lastmsg": "{lastmsg NAME}: last thing a node said",
    "top_talkers": "{top_talkers N}: busiest senders, 24h",
    "top_relays": "{top_relays N}: busiest relays in heard paths, 24h",
    "forecast": "{forecast PLACE}: Open-Meteo forecast for a postal code, city or lat,lon"
                " anywhere (!wx 76131, !wx Karlsruhe)",
    "wx": "Alias of {forecast}",
    "roll": "{roll 2d6} or {roll 100}",
    "pick": "{pick a, b, c}: choose one",
    "8ball": "Magic 8-ball",
    "help": "List enabled command triggers",
    "uptime": "Bot uptime",
    "version": "Plugin version",
}


def builtin_help() -> List[Dict[str, str]]:
    return [{"name": k, "help": HELP.get(k, "")} for k in BUILTINS]
