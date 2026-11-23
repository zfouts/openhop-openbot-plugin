"""Operator notifications: tell the people who run the bot what it noticed.

Off until the operator lists at least one target and turns an event on
(``notify`` in config.json). A target is a channel slot (hashtag or private;
optionally sent in a region) or an admin's DM, picked from the companion's
contacts and kept by public key, since a name can be copied. Each event goes
to every target as one message that fits one frame.

Events: ``update`` (a newer release is out), ``ignored`` (a banned sender's
command was ignored, or an automatic ban began), ``started`` (the bot came
up) and ``problems`` (config problems, CoreScope failing, transmissions
failing). Callers pass a ``key`` and ``every_s`` to hold back repeats.
``max_per_hour`` caps frames on the air (an event to three targets is three),
because each one costs airtime on a shared channel; an event goes to every
target or to none. ``ignored`` also has a small budget of its own, so senders
rotating names that match a ban cannot crowd out the other notices.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Tuple

from .templates import clamp_bytes

EVENTS = ("update", "ignored", "started", "problems")
MAX_PER_HOUR = 6
MAX_PER_HOUR_RANGE = (1, 60)
KEYS_MAX = 1000
EVENT_PER_HOUR = {"ignored": 3}     # events, on top of the frame cap

# send(target, text) -> None when queued, else why not
Send = Callable[[Dict[str, Any], str], Optional[str]]


def target_label(t: Mapping[str, Any]) -> str:
    if t.get("kind") == "dm":
        return str(t.get("name") or str(t.get("pubkey") or "")[:12] or "admin")
    return str(t.get("channel") or "channel")


class Notifier:
    def __init__(self, send: Send, clock: Callable[[], float] = time.time) -> None:
        self._send = send
        self._clock = clock
        self.targets: List[Dict[str, Any]] = []
        self.events: Dict[str, bool] = {e: False for e in EVENTS}
        self.max_per_hour = MAX_PER_HOUR
        self._recent: Deque[float] = deque()        # one entry per frame queued
        self._per_event: Dict[str, Deque[float]] = {e: deque() for e in EVENT_PER_HOUR}
        self._last: Dict[Tuple[str, str], float] = {}
        self.sent = 0
        self.dropped = 0

    def configure(self, cfg: Mapping[str, Any]) -> None:
        """Take the validated ``notify`` block (config.validate cleans it)."""
        cfg = cfg if isinstance(cfg, Mapping) else {}
        self.targets = [dict(t) for t in cfg.get("targets") or [] if isinstance(t, Mapping)]
        ev = cfg.get("events") if isinstance(cfg.get("events"), Mapping) else {}
        self.events = {e: ev.get(e) is True for e in EVENTS}
        n = cfg.get("max_per_hour")
        self.max_per_hour = n if isinstance(n, int) and not isinstance(n, bool) \
            and MAX_PER_HOUR_RANGE[0] <= n <= MAX_PER_HOUR_RANGE[1] else MAX_PER_HOUR

    def wants(self, event: str) -> bool:
        return bool(self.targets) and self.events.get(event, False)

    def notify(self, event: str, text: str, *, key: Optional[str] = None,
               every_s: float = 0.0) -> Dict[str, Any]:
        """Send ``text`` to every target if ``event`` is on. Returns
        ``{"sent": targets queued, "failed": [...], "why": reason if none}``."""
        if not self.events.get(event, False):
            return {"sent": 0, "failed": [], "why": "off"}
        now = self._clock()
        k = (event, key) if key is not None else None
        if k is not None and now - self._last.get(k, float("-inf")) < every_s:
            return {"sent": 0, "failed": [], "why": "repeat"}
        budget = self._per_event.get(event)
        if budget is not None:
            while budget and now - budget[0] >= 3600:
                budget.popleft()
            if len(budget) >= EVENT_PER_HOUR[event]:
                self.dropped += 1
                return {"sent": 0, "failed": [], "why": "%s limit" % event}
        out = self._deliver(text, now)
        if out["sent"] and budget is not None:
            budget.append(now)
        if out["sent"] and k is not None:
            self._last[k] = now
            if len(self._last) > KEYS_MAX:
                oldest = sorted(self._last.items(), key=lambda kv: kv[1])[:len(self._last) // 2]
                for old, _ in oldest:
                    del self._last[old]
        return out

    def test(self, text: str) -> Dict[str, Any]:
        """A test message to every target, whatever the event switches say."""
        return self._deliver(text, self._clock())

    def _deliver(self, text: str, now: float) -> Dict[str, Any]:
        if not self.targets:
            return {"sent": 0, "failed": [], "why": "no targets"}
        while self._recent and now - self._recent[0] >= 3600:
            self._recent.popleft()
        if len(self._recent) + len(self.targets) > self.max_per_hour:
            self.dropped += 1
            return {"sent": 0, "failed": [], "why": "hourly cap"}
        text = clamp_bytes(text)
        sent, failed = 0, []
        for t in self.targets:
            why = self._send(t, text)
            if why is None:
                sent += 1
            else:
                failed.append("%s: %s" % (target_label(t), why))
        if sent:
            self._recent.extend([now] * sent)
            self.sent += 1
        out: Dict[str, Any] = {"sent": sent, "failed": failed}
        if not sent:
            out["why"] = "; ".join(failed) or "not sent"
        return out
