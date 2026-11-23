"""The command engine: match a request to a command, check its scope, render.

Pure apart from the store (cooldowns, roster) and whatever the built-ins
fetch. It never transmits; the hub sends what ``handle`` returns. That keeps
the UI's dry-run ``test`` action on exactly the same code path as the radio.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from ..mesh.frames import REGION_DIRECT, REGION_UNKNOWN, REGION_UNSCOPED, canonical_region
from . import moderation as mod
from .builtins import BUILTINS
from .templates import render

DM = "dm"

# A reply that comes later: (seconds to wait, blocking fn returning the text).
FollowUp = Tuple[float, Callable[[], str]]


def chan_norm(name: Any) -> str:
    """'#Bot', 'bot ' and 'BOT' are the same channel."""
    return str(name or "").strip().lstrip("#").strip().lower()


@dataclass
class Request:
    sender: str
    text: str                       # message body, sender prefix removed
    channel: str                    # channel name, or "dm"
    channel_idx: Optional[int] = None
    region: str = REGION_UNKNOWN
    hops_n: Optional[int] = None
    hops: List[str] = field(default_factory=list)
    hash_size: Optional[int] = None
    snr: Optional[float] = None
    rssi: Optional[int] = None
    ts: Optional[int] = None
    sender_prefix: bytes = b""      # DMs only: 6-byte pubkey prefix to reply to
    packet_hash: Optional[str] = None  # the request frame's packet hash (16 hex)
    heard_at: Optional[float] = None   # wall-clock time this node heard the request
    # How the frame was scoped, for traffic analytics (mesh.frames.frame_scope)
    scope: str = REGION_UNKNOWN
    # The frame was a plain flood AND decrypts to exactly this message
    unscoped_verified: bool = False

    @property
    def is_dm(self) -> bool:
        return self.channel == DM


@dataclass
class Result:
    matched: Optional[str] = None   # command id
    reply: str = ""
    extra: List[str] = field(default_factory=list)
    later: List[FollowUp] = field(default_factory=list)
    reason: str = ""
    auto_banned: bool = False       # this refusal started an automatic ban


def _list(v: Any) -> List[str]:
    if isinstance(v, str):
        v = [x for x in v.split(",")]
    return [str(x).strip() for x in (v or []) if str(x).strip()]


class Engine:
    def __init__(self, cfg: Mapping[str, Any], store, *, weather=None, geocoder=None,
                 corescope=None, shortener=None, node: Optional[Dict[str, Any]] = None):
        self.cfg = cfg
        self.store = store
        self.weather = weather
        self.geocoder = geocoder
        self.corescope = corescope
        self.shortener = shortener
        # live node facts the hub keeps current: name, self_pos, started_at
        self.node = node if node is not None else {}
        self._bans_src: Any = None
        self._bans: List[mod.BanRule] = []

    # ------------------------------------------------------------ config ----

    @property
    def scope(self) -> Mapping[str, Any]:
        return self.cfg.get("scope") or {}

    def commands(self) -> List[Mapping[str, Any]]:
        return [c for c in self.cfg.get("commands") or [] if c.get("enabled", True)]

    def match_command(self, text: str) -> Tuple[Optional[Mapping[str, Any]], str]:
        """(command, the trigger or alias it matched by): the longest trigger or
        alias the text starts with, ending at a word boundary."""
        low = text.lower()
        best, best_trig = None, ""
        for cmd in self.commands():
            for trig in [cmd.get("trigger"), *(cmd.get("aliases") or [])]:
                trig = str(trig or "").strip().lower()
                if not trig or not low.startswith(trig):
                    continue
                nxt = low[len(trig):len(trig) + 1]
                if nxt and not nxt.isspace():
                    continue
                if len(trig) > len(best_trig):
                    best, best_trig = cmd, trig
        return best, best_trig

    def find_command(self, text: str) -> Optional[Mapping[str, Any]]:
        return self.match_command(text)[0]

    # ------------------------------------------------------- moderation ----

    def ban_rules(self) -> List[mod.BanRule]:
        src = self.cfg.get("bans") or []
        if src is not self._bans_src:
            self._bans_src, self._bans = src, mod.compile_bans(src)
        return self._bans

    @staticmethod
    def identity(req: Request) -> str:
        """Who limits and automatic bans count against: a DM sender by their
        key, a channel sender by name (all a channel message carries)."""
        if req.is_dm and req.sender_prefix:
            return mod.dm_key(req.sender_prefix)
        return mod.sender_key(req.sender)

    def ban_reason(self, sender: str, now: Optional[float] = None,
                   key: Optional[str] = None) -> str:
        """Why ``sender`` is ignored ('' when they are not): the matching rule,
        or the automatic ban on ``key`` (default: the name) and when it ends."""
        rule = mod.match_ban(self.ban_rules(), sender)
        if rule is not None:
            return "ban #%d %s" % (rule.index, rule.pattern)
        until = self.store.auto_ban_until(key or mod.sender_key(sender), now)
        if until is not None:
            return "automatic ban until %s" % self._stamp(until)
        return ""

    def _stamp(self, ts: float) -> str:
        """``ts`` as local date and time in the node's timezone."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        try:
            zone = ZoneInfo((self.cfg.get("node") or {}).get("timezone") or "UTC")
        except Exception:
            zone = ZoneInfo("UTC")
        try:
            return datetime.fromtimestamp(ts, zone).strftime("%Y-%m-%d %H:%M %Z")
        except (OverflowError, OSError, ValueError):
            return "a time out of range (%r)" % ts

    def _capped(self, cid: str, req: Request, key: str, why: str, now: float) -> Result:
        """A refusal by the per-sender cap; with auto_ban on, it is a strike.

        Only the cap counts toward a ban: landing in a cooldown is what an
        honest retry over a lossy link looks like. The cap is reached only by
        someone who has already had several answers."""
        ab = self.cfg.get("auto_ban") or {}
        if not ab.get("enabled"):
            return Result(matched=cid, reason=why)
        strikes = int(ab.get("strikes") or mod.AUTO_BAN_DEFAULTS["strikes"])
        window = float(ab.get("window_s") or mod.AUTO_BAN_DEFAULTS["window_s"])
        if self.store.add_strike(key, window, now) < strikes:
            return Result(matched=cid, reason=why)
        hours = float(ab.get("ban_h") or mod.AUTO_BAN_DEFAULTS["ban_h"])
        who = "" if key == mod.sender_key(req.sender) else " (%s)" % req.sender
        self.store.auto_ban(key, now + hours * 3600, "%d refusals by the per-sender cap in "
                            "%gs%s" % (strikes, window, who), now)
        return Result(matched=cid, reason="%s; banned automatically for %gh" % (why, hours),
                      auto_banned=True)

    def nudge_rule(self, channel: str) -> Optional[Mapping[str, Any]]:
        want = chan_norm(channel)
        for rule in self.cfg.get("nudges") or []:
            if rule.get("enabled", True) and chan_norm(rule.get("channel")) == want:
                return rule
        return None

    def _nudge_text(self, req: Request, now: float) -> Tuple[Optional[Mapping[str, Any]], str]:
        """The rule and rendered text a nudge for ``req`` would use, ignoring the
        caps; (None, '') when no nudge applies."""
        if req.is_dm:
            return None, ""
        rule = self.nudge_rule(req.channel)
        if rule is None:
            return None, ""
        me = (self.node.get("name") or "").strip().lower()
        if not req.sender or (me and req.sender.strip().lower() == me):
            return None, ""
        scope = self.scope
        if chan_norm(req.channel) in {chan_norm(c) for c in _list(scope.get("protected_channels"))}:
            return None, ""
        max_hops = scope.get("max_hops")
        if max_hops is not None and req.hops_n is not None and req.hops_n > int(max_hops):
            return None, ""
        if self.ban_reason(req.sender, now, self.identity(req)):
            return None, ""
        return rule, render(str(rule.get("text") or ""), self.context(req), BUILTINS)

    def nudge_preview(self, req: Request) -> str:
        """What a nudge for ``req`` would say, if it was heard unscoped (dry run)."""
        return self._nudge_text(req, time.time())[1]

    def nudge(self, req: Request, now: Optional[float] = None
              ) -> Tuple[Optional[Mapping[str, Any]], str, Optional[int]]:
        """(rule, text, row id) to reply to a sender heard unscoped, or
        (None, '', None).

        Only for a channel with an enabled ``nudges`` entry, a message verified
        as a plain flood (``req.unscoped_verified``), a sender who is not
        banned, and within the rule's caps: per sender on this channel, and
        for the channel. Taking a nudge counts it against both; give the row
        id to ``store.drop_nudge`` if it can not be sent."""
        if not req.unscoped_verified:
            return None, "", None
        now = time.time() if now is None else now
        rule, text = self._nudge_text(req, now)
        if rule is None or not text:
            return None, "", None
        window_s = float(rule.get("window_h") or mod.NUDGE_DEFAULTS["window_h"]) * 3600
        row = self.store.take_nudge(
            chan_norm(req.channel), mod.sender_key(req.sender),
            int(rule.get("per_sender") or 1), int(rule.get("per_channel") or 1),
            window_s, now)
        if row is None:
            return None, "", None
        return rule, text, row

    def scope_check(self, cmd: Mapping[str, Any], req: Request) -> Tuple[bool, str]:
        """Does ``cmd`` answer ``req`` here? Returns (ok, reason-if-not)."""
        scope = self.scope
        if req.is_dm:
            allow = cmd.get("dm")
            if allow is None:
                allow = scope.get("allow_dm", True)
            if not allow:
                return False, "DMs not allowed"
        else:
            ch = chan_norm(req.channel)
            if ch in {chan_norm(c) for c in _list(scope.get("protected_channels"))}:
                return False, "protected channel %s" % req.channel
            chans = _list(cmd.get("channels")) or _list(scope.get("channels"))
            if chans and ch not in {chan_norm(c) for c in chans}:
                return False, "channel %s not in scope" % req.channel

        regions = _list(cmd.get("regions")) or _list(scope.get("regions"))
        if regions and req.region != REGION_DIRECT:
            allowed = {canonical_region(r).lower() for r in regions}
            if req.region == REGION_UNKNOWN or req.region.lower() not in allowed:
                return False, "region %s not in scope" % req.region

        max_hops = scope.get("max_hops")
        if max_hops is not None and req.hops_n is not None and req.hops_n > int(max_hops):
            return False, "%d hops > max %s" % (req.hops_n, max_hops)
        return True, ""

    # ----------------------------------------------------------- context ----

    def context(self, req: Optional[Request] = None, **extra) -> Dict[str, Any]:
        node = self.cfg.get("node") or {}
        ctx: Dict[str, Any] = {
            "store": self.store,
            "weather": self.weather,
            "geocoder": self.geocoder,
            "corescope": self.corescope,
            "shortener": self.shortener,
            "self_pubkey": self.node.get("pubkey"),
            "cfg": self.cfg,
            "commands": self.commands(),
            "gateway": self.node.get("name") or node.get("name") or "",
            "self_pos": self.node.get("self_pos"),
            "started_at": self.node.get("started_at"),
            "tz": node.get("timezone") or "UTC",
            "units": node.get("units") or "imperial",
            "extra": [],
            "later": [],
        }
        if req is not None:
            ctx.update(
                sender=req.sender, channel=req.channel, region=req.region,
                hops_n=req.hops_n, hops=req.hops, hash_size=req.hash_size,
                snr=req.snr, rssi=req.rssi, rx_ts=req.ts, packet_hash=req.packet_hash,
                sent_ts=req.ts, heard_at=req.heard_at,
            )
        ctx.update(extra)
        return ctx

    # ------------------------------------------------------------ handle ----

    def cooldown_scope(self, req: Request) -> str:
        """Command cooldowns follow the sender across channels and regions."""
        return self.identity(req)

    def handle(self, req: Request, *, dry_run: bool = False,
               now: Optional[float] = None) -> Result:
        now = time.time() if now is None else now
        me = (self.node.get("name") or "").strip().lower()
        if not req.sender or (me and req.sender.strip().lower() == me):
            return Result(reason="own message")
        cmd, trig = self.match_command(req.text)
        if cmd is None:
            return Result(reason="no command matched")
        cid = str(cmd.get("id") or cmd.get("trigger"))
        banned = self.ban_reason(req.sender, now, self.identity(req))
        if banned:
            return Result(matched=cid, reason="banned (%s)" % banned)
        ok, why = self.scope_check(cmd, req)
        if not ok:
            return Result(matched=cid, reason=why)

        held: List[Tuple[str, str]] = []
        hit: Optional[int] = None
        if not dry_run:
            key = self.identity(req)
            limits = self.cfg.get("limits") or {}
            floor = float(limits.get("user_cooldown_s") or 0)
            if floor > 0:
                if not self.store.claim("_user", key, floor, now):
                    return Result(matched=cid, reason="requester cooldown")
                held.append(("_user", key))
            scope = self.cooldown_scope(req)
            if not self.store.claim(cid, scope, float(cmd.get("cooldown_s") or 0), now):
                for h in held:
                    self.store.release(*h)
                return Result(matched=cid, reason="command cooldown")
            held.append((cid, scope))
            max_n = int(limits.get("user_max_requests") or 0)
            if max_n > 0:
                window = float(limits.get("user_window_s") or mod.USER_WINDOW_S)
                hit = self.store.take_hit(key, max_n, window, now)
                if hit is None:
                    for h in held:
                        self.store.release(*h)
                    return self._capped(cid, req, key, "requester rate limit (%d per %gs)"
                                        % (max_n, window), now)

        ctx = self.context(req, args=req.text[len(trig):].strip())
        reply = render(str(cmd.get("reply") or ""), ctx, BUILTINS)
        if not reply:
            # declined: give the cooldowns back so the next try can answer
            for h in held:
                self.store.release(*h)
            if hit is not None:
                self.store.drop_hit(hit)
            return Result(matched=cid, reason="reply rendered empty")
        return Result(matched=cid, reply=reply, extra=list(ctx["extra"]),
                      later=list(ctx["later"]))

    def reply_scope(self, region: str) -> Optional[bytes]:
        """Flood scope for a reply to a request that arrived under ``region``.

        None = the companion's default scope; b"" = force unscoped.
        """
        from ..mesh.frames import region_key

        if not (self.cfg.get("limits") or {}).get("reply_in_request_region", True):
            return None
        if region == REGION_UNSCOPED:
            return b""
        if region.startswith("#"):
            try:
                return region_key(region)
            except ValueError:
                return None
        return None
