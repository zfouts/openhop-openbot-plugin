"""The command engine: match a request to a command, check its scope, render.

Pure apart from the store (cooldowns, roster) and whatever the built-ins
fetch. It never transmits; the hub sends what ``handle`` returns. That keeps
the UI's dry-run ``test`` action on exactly the same code path as the radio.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..mesh.frames import REGION_DIRECT, REGION_UNKNOWN, REGION_UNSCOPED, canonical_region
from .builtins import BUILTINS
from .templates import render

DM = "dm"


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

    @property
    def is_dm(self) -> bool:
        return self.channel == DM


@dataclass
class Result:
    matched: Optional[str] = None   # command id
    reply: str = ""
    extra: List[str] = field(default_factory=list)
    reason: str = ""


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

    # ------------------------------------------------------------ config ----

    @property
    def scope(self) -> Mapping[str, Any]:
        return self.cfg.get("scope") or {}

    def commands(self) -> List[Mapping[str, Any]]:
        return [c for c in self.cfg.get("commands") or [] if c.get("enabled", True)]

    def find_command(self, text: str) -> Optional[Mapping[str, Any]]:
        """Longest trigger that the text starts with, ending at a word boundary."""
        low = text.lower()
        best = None
        for cmd in self.commands():
            trig = str(cmd.get("trigger") or "").strip().lower()
            if not trig or not low.startswith(trig):
                continue
            nxt = low[len(trig):len(trig) + 1]
            if nxt and not nxt.isspace():
                continue
            if best is None or len(trig) > len(best["trigger"].strip()):
                best = cmd
        return best

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
        return "%s|%s|%s" % (req.sender.lower(), chan_norm(req.channel), req.region)

    def handle(self, req: Request, *, dry_run: bool = False,
               now: Optional[float] = None) -> Result:
        now = time.time() if now is None else now
        me = (self.node.get("name") or "").strip().lower()
        if not req.sender or (me and req.sender.strip().lower() == me):
            return Result(reason="own message")
        cmd = self.find_command(req.text)
        if cmd is None:
            return Result(reason="no command matched")
        cid = str(cmd.get("id") or cmd.get("trigger"))
        ok, why = self.scope_check(cmd, req)
        if not ok:
            return Result(matched=cid, reason=why)

        held: List[Tuple[str, str]] = []
        if not dry_run:
            floor = float((self.cfg.get("limits") or {}).get("user_cooldown_s") or 0)
            if floor > 0:
                if not self.store.claim("_user", req.sender.lower(), floor, now):
                    return Result(matched=cid, reason="requester cooldown")
                held.append(("_user", req.sender.lower()))
            scope = self.cooldown_scope(req)
            if not self.store.claim(cid, scope, float(cmd.get("cooldown_s") or 0), now):
                for h in held:
                    self.store.release(*h)
                return Result(matched=cid, reason="command cooldown")
            held.append((cid, scope))

        trig = str(cmd.get("trigger") or "").strip()
        ctx = self.context(req, args=req.text[len(trig):].strip())
        reply = render(str(cmd.get("reply") or ""), ctx, BUILTINS)
        if not reply:
            # declined: give the cooldowns back so the next try can answer
            for h in held:
                self.store.release(*h)
            return Result(matched=cid, reason="reply rendered empty")
        return Result(matched=cid, reply=reply, extra=list(ctx["extra"]))

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
