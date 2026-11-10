"""The hub: one companion session, everything the bot does around it.

    companion ──► on_raw_frame ──► correlator (hop chain, region) + roster
              └─► on_message ───► Request ──► Engine (worker thread) ──► TxQueue ──► companion
    config.json ──(mtime poll)──► reload + `_cmd` mailbox
    runtime.json ◄── snapshot every few seconds and on change

Nothing here blocks the event loop: template rendering (which may call NWS)
runs in a worker thread, and the companion reader only ever enqueues.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os
import tempfile
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from . import __version__
from .config import PLUGIN_ID, load
from .engine.bot import DM, Engine, Request, chan_norm
from .engine.builtins import BUILTINS, builtin_help
from .engine.pathresolve import split_chain
from .engine.schedule import Schedule
from .engine.templates import render
from .engine.usage import parse_window, skip_category, summarize
from .mesh.companion import Advert, CompanionClient, Message
from .mesh.correlate import FrameCorrelator, region_of
from .mesh.frames import (
    REGION_UNKNOWN,
    canonical_region,
    channel_hash_byte,
    decode_advert_frame,
    hashtag_secret,
    region_key,
)
from .mesh.txqueue import EXTRA_GAP_S, TxJob, TxQueue
from .services.geocode import Geocoder
from .services.openmeteo import OpenMeteo
from .services.corescope import CoreScope
from .services.shortener import DEFAULT as DEFAULT_SHORTENER
from .services.shortener import make_shortener
from .store import Store

logger = logging.getLogger("openbot")

ACTIVITY_MAX = 100
CONFIG_POLL_S = 2.0
RUNTIME_HEARTBEAT_S = 5.0
CONTACTS_INTERVAL_S = 300
ANNOUNCE_TICK_S = 15


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, default=str)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def split_sender(text: str) -> tuple[str, str]:
    """Channel text arrives as '<sender>: <body>'."""
    name, sep, body = text.partition(": ")
    if not sep or not name or len(name) > 32:
        return "", text
    return name.strip(), body.strip()


class Hub:
    def __init__(self, config_path: Path, data_dir: Path):
        self.config_path = config_path
        self.data_dir = data_dir
        self.runtime_path = data_dir / "runtime.json"
        self.started_at = int(time.time())

        self.cfg: Dict[str, Any] = {}
        self.problems: List[str] = []
        self._cfg_sig: Optional[tuple] = None
        self._reload_config(initial=True)

        self.store = Store(data_dir / "openbot.db")
        self.node: Dict[str, Any] = {"started_at": self.started_at}
        self.engine = Engine(self.cfg, self.store, node=self.node)
        self._build_services()

        comp = self.cfg.get("companion") or {}
        self.client = CompanionClient(comp.get("host", "127.0.0.1"), int(comp.get("port", 5001)))
        self.client.on_connected = self._on_connected
        self.client.on_message = self._on_message
        self.client.on_advert = self._on_advert
        self.client.on_raw_frame = self._on_raw_frame

        self.correlator = FrameCorrelator()
        self.region_keys: Dict[str, bytes] = {}
        self._rebuild_regions()
        self.slots: Dict[int, Dict[str, Any]] = {}       # idx -> {name, ok}
        self._contacts: Dict[bytes, str] = {}            # 6-byte prefix -> name
        self._last_rssi: Optional[int] = None

        limits = self.cfg.get("limits") or {}
        self.tx = TxQueue(self._transmit, gap_s=float(limits.get("tx_gap_s", 2.5)),
                          max_per_min=int(limits.get("tx_max_per_min", 8)),
                          on_result=self._on_tx_result)

        self.counters = Counter()
        self.regions_seen: Counter = Counter()
        self.activity: Deque[Dict[str, Any]] = deque(maxlen=ACTIVITY_MAX)
        self.cmd_stats: Dict[str, Dict[str, Any]] = {}
        self.mailbox_done: Deque[str] = deque(maxlen=50)
        self.mailbox_results: Dict[str, Any] = {}
        self._dirty = asyncio.Event()
        self._stop = asyncio.Event()
        self._ann_minute: Dict[str, str] = {}

    # ------------------------------------------------------------ config ----

    def _cfg_signature(self) -> Optional[tuple]:
        try:
            st = self.config_path.stat()
            return (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            return None

    def _reload_config(self, initial: bool = False) -> bool:
        sig = self._cfg_signature()
        if not initial and sig == self._cfg_sig:
            return False
        self._cfg_sig = sig
        try:
            cfg, problems = load(self.config_path)
        except (OSError, ValueError) as exc:
            logger.error("config.json unreadable, keeping the last good config: %s", exc)
            self.problems = [f"config.json unreadable: {exc}"]
            return False
        for p in problems:
            logger.warning("config: %s", p)
        old = self.cfg
        self.cfg, self.problems = cfg, problems
        logging.getLogger().setLevel(str(cfg.get("log_level") or "INFO").upper())
        if not initial:
            self.engine.cfg = cfg
            self._rebuild_regions()
            limits = cfg.get("limits") or {}
            self.tx.gap_s = float(limits.get("tx_gap_s", 2.5))
            self.tx.max_per_min = int(limits.get("tx_max_per_min", 8))
            if (old.get("weather"), old.get("corescope")) != \
                    (cfg.get("weather"), cfg.get("corescope")):
                self._build_services()
            if (old.get("node") or {}).get("name") != (cfg.get("node") or {}).get("name"):
                asyncio.ensure_future(self._apply_node_name())
            if self._strip_cmd(old.get("channels")) != self._strip_cmd(cfg.get("channels")):
                if self.client.is_connected():
                    asyncio.ensure_future(self._provision_channels())
            logger.info("config reloaded (%d commands)", len(cfg.get("commands") or []))
            self._dirty.set()
        return True

    def _build_services(self) -> None:
        """Open-Meteo and Nominatim clients from `weather`, the optional
        CoreScope client from `corescope`, and the link shortener
        (`corescope.shortener`) for the CoreScope map links in `!path` replies.

        `weather.contact` goes into the User-Agent: Nominatim's policy asks
        heavy users to be reachable, and it costs nothing to say who we are.
        """
        w = self.cfg.get("weather") or {}
        contact = str(w.get("contact") or "").strip()
        ua = "openhop-openbot/%s (https://github.com/zfouts/openhop-openbot-plugin%s)" % (
            __version__, "; " + contact if contact else "")
        self.engine.weather = OpenMeteo(ua)
        self.engine.geocoder = Geocoder(ua, default_country=str(w.get("default_country") or ""))
        cs = self.cfg.get("corescope") or {}
        self.engine.corescope = None
        if cs.get("enabled"):
            try:
                self.engine.corescope = CoreScope(str(cs.get("base_url") or ""), user_agent=ua)
            except ValueError as exc:
                logger.error("corescope disabled: %s", exc)
        try:
            self.engine.shortener = make_shortener(cs.get("shortener", DEFAULT_SHORTENER),
                                                   user_agent=ua)
        except ValueError as exc:
            self.engine.shortener = None
            logger.error("no link shortener: %s", exc)
        if self.engine.corescope is not None:
            logger.info("!path links to CoreScope's map at %s (shortener: %s)",
                        self.engine.corescope.base,
                        self.engine.shortener.service if self.engine.shortener else "none")

    @staticmethod
    def _strip_cmd(v: Any) -> str:
        return json.dumps(v, sort_keys=True)

    def _rebuild_regions(self) -> None:
        keys = {}
        for name in self.cfg.get("regions") or []:
            canon = canonical_region(str(name))
            try:
                keys[canon] = region_key(canon)
            except ValueError:
                logger.warning("region %r has no auto key (private '$' regions are not "
                               "supported); it can never match", name)
        self.region_keys = keys

    # ----------------------------------------------------------- lifecycle --

    async def run(self) -> None:
        logger.info("openbot %s starting: companion %s:%s, data %s", __version__,
                    self.client.host, self.client.port, self.data_dir)
        tasks = [
            asyncio.create_task(self.client.run(), name="companion"),
            asyncio.create_task(self.tx.run(), name="txqueue"),
            asyncio.create_task(self._config_loop(), name="config"),
            asyncio.create_task(self._runtime_loop(), name="runtime"),
            asyncio.create_task(self._announce_loop(), name="announce"),
            asyncio.create_task(self._maintenance_loop(), name="maintenance"),
        ]
        try:
            await self._stop.wait()
        finally:
            self.tx.stop()
            await self.client.stop()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._write_runtime()
            self.store.close()

    def stop(self) -> None:
        self._stop.set()

    async def _sleep(self, secs: float) -> bool:
        """Sleep unless stopped; True if stopping."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=secs)
        return self._stop.is_set()

    # ---------------------------------------------------------- activity ----

    def log_activity(self, kind: str, **fields: Any) -> None:
        entry = {"ts": int(time.time()), "kind": kind}
        entry.update({k: v for k, v in fields.items() if v not in (None, "")})
        self.activity.append(entry)
        self._dirty.set()

    # ---------------------------------------------------------- companion ---

    async def _on_connected(self) -> None:
        si = self.client.self_info
        node = self.cfg.get("node") or {}
        if si is not None:
            self.node["name"] = si.name
            self.node["pubkey"] = si.public_key.hex()
        lat, lon = node.get("latitude"), node.get("longitude")
        if lat is None and si is not None and (si.latitude or si.longitude):
            lat, lon = si.latitude, si.longitude
        self.node["self_pos"] = (float(lat), float(lon)) if lat is not None and lon is not None \
            else None
        try:
            ok = await self.client.set_device_time()
            logger.info("device clock sync -> %s", "ok" if ok else "REFUSED")
        except Exception as exc:
            logger.error("device clock sync failed: %s", exc)
        hb = node.get("path_hash_bytes")
        if hb in (1, 2, 3):
            try:
                ok = await self.client.set_path_hash_mode(int(hb))
                logger.info("path hash: %d-byte -> %s", hb, "ok" if ok else "REFUSED")
            except Exception as exc:
                logger.error("path hash mode failed: %s", exc)
        await self._apply_node_name()
        await self._provision_channels()
        await self._refresh_contacts()
        self.log_activity("tx", detail="companion connected as %s" % self.node.get("name", "?"))

    async def _apply_node_name(self) -> None:
        """Make the radio's name match `node.name` from the plugin page.

        Empty means "leave the radio's name alone". openHop saves a rename to
        the companion's node_name in config.yaml, so it survives restarts."""
        want = str((self.cfg.get("node") or {}).get("name") or "").strip()
        have = self.node.get("name")
        if not want or want == have or not self.client.is_connected():
            return
        try:
            ok = await self.client.set_advert_name(want)
        except Exception as exc:
            logger.error("rename %r -> %r failed: %s", have, want, exc)
            return
        logger.info("node name %r -> %r: %s", have, want, "ok" if ok else "REFUSED")
        if ok:
            self.node["name"] = want
            self.log_activity("tx", detail="renamed %s -> %s" % (have, want))

    async def _provision_channels(self) -> List[Dict[str, Any]]:
        """Write configured slots to the companion, then read every slot back.

        Additive: slots not in config are left exactly as they are, so an
        operator's hand-added channels survive. Slot 0 (Public) is never set.
        """
        for entry in self.cfg.get("channels") or []:
            idx, name = int(entry["idx"]), str(entry["name"])
            try:
                secret = bytes.fromhex(entry["secret"]) if entry.get("secret") \
                    else hashtag_secret(name)
                self.correlator.channel_hashes[idx] = channel_hash_byte(secret)
                ok = await self.client.set_channel(idx, name, secret)
                logger.info("channel slot %s = %r -> %s", idx, name, "ok" if ok else "REFUSED")
            except Exception as exc:
                ok = False
                logger.error("channel slot %s (%r) rejected: %s", idx, name, exc)
            self.slots[idx] = {"name": name, "ok": bool(ok)}
        for idx in range(40):
            if idx in self.slots:
                continue
            with contextlib.suppress(Exception):
                name = await self.client.get_channel(idx)
                if name:
                    self.slots[idx] = {"name": name, "ok": True}
                    if name.startswith("#"):
                        self.correlator.channel_hashes.setdefault(
                            idx, channel_hash_byte(hashtag_secret(name)))
        self._dirty.set()
        return [{"idx": i, **s} for i, s in sorted(self.slots.items())]

    async def _refresh_contacts(self) -> None:
        try:
            contacts = await self.client.get_contacts()
        except Exception as exc:
            logger.warning("contact walk failed: %s", exc)
            return
        types = {1: "chat", 2: "repeater", 3: "room", 4: "sensor"}
        for c in contacts:
            self.store.upsert_node(
                c.public_key.hex(), name=c.name, type=types.get(c.type, "unknown"),
                lat=c.latitude or None, lon=c.longitude or None,
                heard=c.last_advert or None,
            )
        self._contacts = {c.public_key[:6]: c.name for c in contacts}

    async def _on_advert(self, advert: Advert) -> None:
        # bare pubkey push; the rich advert comes through the raw frame
        self.store.upsert_node(advert.public_key.hex())

    async def _on_raw_frame(self, raw: bytes, snr: float, rssi: int) -> None:
        self.counters["rx_frames"] += 1
        self._last_rssi = rssi
        with contextlib.suppress(Exception):
            self.correlator.remember(raw, snr, rssi)
        try:
            adv = decode_advert_frame(raw)
        except Exception:
            adv = None
        if adv is not None:
            hs = adv.get("hash_size") or 1
            path = adv.get("path") or b""
            hops = [path[i:i + hs].hex() for i in range(0, len(path), hs)]
            self.store.upsert_node(
                adv["pubkey"].hex(), name=adv.get("node_name", ""),
                type=adv.get("contact_type", "unknown"),
                lat=adv.get("latitude"), lon=adv.get("longitude"),
                snr=snr, hops=len(hops), path=">".join(hops) or None, advert=True,
            )
            if hops:
                self.store.record_chain(hops)

    async def _on_message(self, msg: Message) -> None:
        self.counters["rx_messages"] += 1
        try:
            req = self._request_from(msg)
        except Exception as exc:
            logger.error("could not decode message: %s", exc, exc_info=True)
            return
        if req is None:
            return
        self.regions_seen[req.region] += 1
        self.store.record_msg(channel=req.channel, sender=req.sender, text=req.text,
                              region=req.region, hops_n=req.hops_n,
                              path=">".join(req.hops) or None, ts=req.ts)
        if req.hops:
            self.store.record_chain(req.hops)
        if not self.engine.find_command(req.text):
            return
        self.counters["commands"] += 1
        self.log_activity("rx", channel=req.channel, region=req.region, sender=req.sender,
                          text=req.text)
        result = await asyncio.to_thread(self.engine.handle, req)
        if result.matched:
            answered = bool(result.reply)
            try:
                self.store.record_usage(
                    ts=req.heard_at or time.time(), cmd_id=result.matched,
                    outcome="answered" if answered else "skipped",
                    reason=None if answered else skip_category(result.reason),
                    channel=req.channel, region=req.region, sender=req.sender)
            except Exception as exc:
                logger.error("could not record usage: %s", exc, exc_info=True)
        if not result.reply:
            self.counters["skipped"] += 1
            self.log_activity("skip", channel=req.channel, region=req.region,
                              sender=req.sender, detail="%s: %s" % (result.matched or "-",
                                                                    result.reason))
            return
        st = self.cmd_stats.setdefault(result.matched, {"fires": 0, "last_fired": None})
        st["fires"] += 1
        st["last_fired"] = int(time.time())
        self.counters["replies"] += 1
        scope = None if req.is_dm else self.engine.reply_scope(req.region)
        for i, text in enumerate([result.reply] + result.extra):
            self.tx.put(TxJob(
                text=text,
                channel_idx=None if req.is_dm else req.channel_idx,
                dm_prefix=req.sender_prefix if req.is_dm else None,
                scope=scope, label="%s:%s" % (result.matched, req.region),
                not_before=time.monotonic() + EXTRA_GAP_S * i,
            ))
        self.log_activity("reply", channel=req.channel, region=req.region, sender=req.sender,
                          text=result.reply, detail=result.matched)

    def _request_from(self, msg: Message) -> Optional[Request]:
        frame = self.correlator.claim(msg)
        # When the radio heard it: the frame's receive time (monotonic) moved
        # onto the wall clock, or now when the frame could not be matched.
        heard_at = time.time()
        if frame is not None:
            heard_at -= max(0.0, time.monotonic() - frame.t)
        hops_n = 0 if msg.path_len in (0, 0xFF) else msg.path_len & 0x3F
        hops: List[str] = []
        hash_size = None
        if frame is not None:
            hash_size = frame.hash_size
            if frame.path:
                with contextlib.suppress(ValueError):
                    hops = split_chain(frame.path.hex(), len(frame.path) // frame.hash_size)
        region = region_of(frame, self.region_keys)
        snr = msg.snr if msg.snr else (frame.snr if frame else None)
        rssi = frame.rssi if frame else None
        if msg.channel_idx is None:
            sender = self._contacts.get(bytes(msg.sender_prefix[:6]))
            if not sender:
                sender = msg.sender_prefix.hex()[:8]
            return Request(sender=sender, text=msg.text.strip(), channel=DM, region=region,
                           hops_n=hops_n, hops=hops, hash_size=hash_size, snr=snr, rssi=rssi,
                           ts=msg.timestamp, sender_prefix=bytes(msg.sender_prefix[:6]),
                           packet_hash=frame.hash if frame else None, heard_at=heard_at)
        sender, body = split_sender(msg.text)
        if not sender:
            return None
        slot = self.slots.get(msg.channel_idx)
        channel = slot["name"] if slot else str(msg.channel_idx)
        if msg.channel_idx == 0 and not slot:
            channel = "Public"
        return Request(sender=sender, text=body, channel=channel, channel_idx=msg.channel_idx,
                       region=region, hops_n=hops_n, hops=hops, hash_size=hash_size, snr=snr,
                       rssi=rssi, ts=msg.timestamp, packet_hash=frame.hash if frame else None,
                       heard_at=heard_at)

    # ---------------------------------------------------------- transmit ----

    async def _transmit(self, job: TxJob) -> bool:
        if not self.client.is_connected():
            return False
        if job.dm_prefix is not None:
            return await self.client.send_text_message(job.dm_prefix, job.text)
        return await self.client.send_channel_message(job.channel_idx, job.text, scope=job.scope)

    def _on_tx_result(self, job: TxJob, ok: bool, why: str) -> None:
        if not ok:
            self.log_activity("error", text=job.text, detail="tx %s: %s" % (job.label, why))

    def resolve_channel(self, token: Any) -> Optional[int]:
        if isinstance(token, int) or (isinstance(token, str) and token.strip().isdigit()):
            idx = int(token)
            return idx if idx in self.slots or idx == 0 else None
        want = chan_norm(token)
        for idx, slot in self.slots.items():
            if chan_norm(slot["name"]) == want:
                return idx
        return None

    def scope_for(self, region: str) -> Optional[bytes]:
        """Scope for an announcement/alert/manual send: '' default, '*' unscoped."""
        region = (region or "").strip()
        if not region:
            return None
        if region == "*":
            return b""
        try:
            return region_key(region)
        except ValueError:
            return None

    def send_text(self, channel: Any, text: str, region: str = "", label: str = "") -> bool:
        idx = self.resolve_channel(channel)
        if idx is None:
            self.log_activity("error", text=text, detail="no channel %r" % channel)
            return False
        return self.tx.put(TxJob(text=text, channel_idx=idx, scope=self.scope_for(region),
                                 label=label or "send"))

    # ------------------------------------------------------- announcements --

    def _announce(self, ann: Dict[str, Any]) -> bool:
        text = render(str(ann.get("text") or ""), self.engine.context(), BUILTINS)
        if not text:
            return False
        ok = self.send_text(ann.get("channel"), text, ann.get("region") or "",
                            label="announce:%s" % ann.get("id"))
        if ok:
            self.log_activity("announce", channel=ann.get("channel"),
                              region=ann.get("region"), text=text, detail=ann.get("id"))
        return ok

    def _local_now(self) -> datetime:
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo((self.cfg.get("node") or {}).get("timezone") or "UTC"))
        except Exception:
            return datetime.now()

    async def _announce_loop(self) -> None:
        while not await self._sleep(ANNOUNCE_TICK_S):
            if not self.client.is_connected():
                continue
            now = self._local_now()
            minute = now.strftime("%Y%m%d%H%M")
            for ann in self.cfg.get("announcements") or []:
                if not ann.get("enabled", True):
                    continue
                try:
                    sched = Schedule.parse(str(ann.get("schedule")))
                    if sched.every_s:
                        due = self.store.claim("_ann:" + ann["id"], "*", sched.every_s)
                    else:
                        due = sched.matches(now) and self._ann_minute.get(ann["id"]) != minute
                        if due:
                            self._ann_minute[ann["id"]] = minute
                    if due:
                        await asyncio.to_thread(self._announce, ann)
                except Exception as exc:
                    logger.error("announcement %s failed: %s", ann.get("id"), exc)

    # ------------------------------------------------------- maintenance ----

    async def _maintenance_loop(self) -> None:
        while not await self._sleep(CONTACTS_INTERVAL_S):
            if self.client.is_connected():
                await self._refresh_contacts()
            with contextlib.suppress(Exception):
                self.store.prune()

    # ----------------------------------------------------------- mailbox ----

    async def _config_loop(self) -> None:
        while not await self._sleep(CONFIG_POLL_S):
            try:
                if self._reload_config():
                    await self._run_mailbox()
            except Exception as exc:
                logger.error("config reload failed: %s", exc, exc_info=True)

    async def _run_mailbox(self) -> None:
        cmd = self.cfg.get("_cmd")
        if not isinstance(cmd, dict) or not cmd.get("id"):
            return
        cid = str(cmd["id"])
        if cid in self.mailbox_done:
            return
        self.mailbox_done.append(cid)
        try:
            result = await self.execute(str(cmd.get("action")), cmd.get("args") or {})
            self.mailbox_results[cid] = {"ok": True, **result}
        except Exception as exc:
            logger.warning("mailbox %s failed: %s", cmd.get("action"), exc)
            self.mailbox_results[cid] = {"ok": False, "error": str(exc)}
        while len(self.mailbox_results) > 20:
            self.mailbox_results.pop(next(iter(self.mailbox_results)))
        self._dirty.set()

    async def execute(self, action: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if action == "test":
            return await asyncio.to_thread(self.dry_run, args)
        if action == "usage":
            tz = (self.cfg.get("node") or {}).get("timezone") or "America/Chicago"
            window = parse_window(args, time.time(), tz)
            rows = await asyncio.to_thread(self.store.usage_rows, window["from"], window["to"])
            return summarize(rows, window, tz, self.engine.commands())
        if action == "advert":
            flood = args.get("flood", True)
            if not isinstance(flood, bool):
                flood = str(flood).strip().lower() in ("1", "true", "yes", "on")
            if not self.client.is_connected():
                raise RuntimeError("companion not connected")
            sent = bool(await self.client.send_self_advert(flood=flood))
            self.log_activity("tx", detail="advert to %s -> %s" % (
                "the whole mesh" if flood else "neighbours", "ok" if sent else "refused"))
            return {"sent": sent, "flood": flood}
        if action == "send":
            text = str(args.get("text") or "").strip()
            if not text:
                raise ValueError("empty text")
            if not self.send_text(args.get("channel"), text, args.get("region") or "",
                                  label="manual"):
                raise ValueError("not queued (unknown channel or queue full)")
            self.log_activity("tx", channel=str(args.get("channel")), text=text,
                              detail="manual send")
            return {"queued": True}
        if action == "announce_now":
            ann = next((a for a in self.cfg.get("announcements") or []
                        if a.get("id") == args.get("id")), None)
            if ann is None:
                raise ValueError("no announcement %r" % args.get("id"))
            return {"queued": await asyncio.to_thread(self._announce, ann)}
        if action == "clear_cooldowns":
            return {"cleared": self.store.clear_fires()}
        if action == "provision_channels":
            if not self.client.is_connected():
                raise ValueError("companion not connected")
            return {"channels": await self._provision_channels()}
        raise ValueError("unknown action %r" % action)

    def dry_run(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Render what the bot would answer, on the real code path, without sending."""
        text = str(args.get("text") or "")
        channel = str(args.get("channel") or "")
        chain = str(args.get("hops") or "").replace(">", "").replace(",", "").strip().lower()
        hops: List[str] = []
        hash_size = None
        if chain:
            hash_size = int(args.get("hash_size") or (self.cfg.get("node") or {})
                            .get("path_hash_bytes") or 2)
            hops = split_chain(chain, len(chain) // (2 * hash_size))
        hops_n = args.get("hops_n")
        req = Request(
            sender=str(args.get("sender") or "tester"), text=text,
            channel=DM if channel.lower() == DM else (channel or "#bot"),
            region=canonical_region(str(args.get("region") or "*")) or REGION_UNKNOWN,
            hops_n=int(hops_n) if hops_n is not None else len(hops),
            hops=hops, hash_size=hash_size, snr=args.get("snr"),
        )
        res = self.engine.handle(req, dry_run=True)
        return {"matched": res.matched, "reply": res.reply, "extra": res.extra,
                "reason": res.reason,
                "bytes": len(res.reply.encode("utf-8"))}

    # ----------------------------------------------------------- runtime ----

    def runtime_payload(self) -> Dict[str, Any]:
        counts = self.store.counts()
        recent = []
        for r in self.store.recent_nodes(50):
            recent.append({"pubkey": r["pubkey"][:8], "name": r["name"], "type": r["type"],
                           "last_heard": r["last_heard"], "hops": r["last_hops"],
                           "snr": r["last_snr"], "latitude": r["lat"], "longitude": r["lon"]})
        pos = self.node.get("self_pos")
        return {
            "id": os.environ.get("OPENHOP_PLUGIN_ID", PLUGIN_ID),
            "version": __version__,
            "timestamp": int(time.time()),
            "started_at": self.started_at,
            "status": {
                "companion": "connected" if self.client.is_connected() else "connecting",
                "node_name": self.node.get("name"),
                "pubkey": self.node.get("pubkey"),
                "latitude": pos[0] if pos else None,
                "longitude": pos[1] if pos else None,
                "config_problems": self.problems,
            },
            "counters": {**{k: self.counters.get(k, 0) for k in
                            ("rx_messages", "rx_frames", "commands", "replies", "skipped")},
                         "tx_sent": self.tx.sent, "tx_dropped": self.tx.dropped,
                         "tx_queue": len(self.tx)},
            "channels": [{"idx": i, **s} for i, s in sorted(self.slots.items())],
            "regions_seen": dict(self.regions_seen),
            "activity": list(self.activity),
            "roster": {"nodes": counts["nodes"], "repeaters": counts["repeaters"],
                       "recent": recent},
            "builtins": builtin_help(),
            "commands": [{"id": k, **v} for k, v in self.cmd_stats.items()],
            "mailbox": {"processed": list(self.mailbox_done),
                        "results": copy.deepcopy(self.mailbox_results)},
        }

    def _write_runtime(self) -> None:
        try:
            atomic_write_json(self.runtime_path, self.runtime_payload())
        except Exception as exc:
            logger.error("runtime.json write failed: %s", exc)

    async def _runtime_loop(self) -> None:
        while not self._stop.is_set():
            self._write_runtime()
            self._dirty.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._dirty.wait(), timeout=RUNTIME_HEARTBEAT_S)
            await asyncio.sleep(0.5)    # coalesce bursts
