"""Paced transmit queue: every frame the bot sends goes through here.

A companion that has just transmitted drops the next frame if it arrives
too soon (2 s proved too short for follow-ups, so extras wait 5 s). Airtime
is shared with everyone else on the mesh, so there is a hard per-minute cap
on top of the gap, and a shallow queue: a reply that cannot go out within a
minute is stale and is dropped, not delivered late.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Deque, Optional

logger = logging.getLogger("openbot.tx")

EXTRA_GAP_S = 5.0
MAX_DEPTH = 12
MAX_AGE_S = 60.0


@dataclass
class TxJob:
    text: str
    channel_idx: Optional[int] = None      # channel send
    dm_prefix: Optional[bytes] = None      # or direct message
    scope: Optional[bytes] = None          # flood region (see CompanionClient)
    label: str = ""
    not_before: float = 0.0                # monotonic
    queued_at: float = field(default_factory=time.monotonic)


Sender = Callable[[TxJob], Awaitable[bool]]


class TxQueue:
    def __init__(self, send: Sender, *, gap_s: float = 2.5, max_per_min: int = 8,
                 on_result: Optional[Callable[[TxJob, bool, str], None]] = None):
        self._send = send
        self.gap_s = gap_s
        self.max_per_min = max_per_min
        self.on_result = on_result
        self._jobs: Deque[TxJob] = deque()
        self._sent: Deque[float] = deque()
        self._last = 0.0
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self.sent = 0
        self.dropped = 0

    def __len__(self) -> int:
        return len(self._jobs)

    def put(self, job: TxJob) -> bool:
        if len(self._jobs) >= MAX_DEPTH:
            self.dropped += 1
            self._report(job, False, "queue full")
            return False
        self._jobs.append(job)
        self._wake.set()
        return True

    def _report(self, job: TxJob, ok: bool, why: str) -> None:
        if self.on_result:
            with contextlib.suppress(Exception):
                self.on_result(job, ok, why)

    def _next_ready(self, now: float) -> Optional[TxJob]:
        for job in list(self._jobs):
            if now - job.queued_at > MAX_AGE_S:
                self._jobs.remove(job)
                self.dropped += 1
                self._report(job, False, "stale")
        for job in self._jobs:
            if job.not_before <= now:
                return job
        return None

    async def run(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            while self._sent and now - self._sent[0] > 60:
                self._sent.popleft()
            job = self._next_ready(now)
            wait = 1.0
            if job is not None:
                gap_left = self.gap_s - (now - self._last)
                if len(self._sent) >= self.max_per_min:
                    wait = 60 - (now - self._sent[0])
                elif gap_left > 0:
                    wait = gap_left
                else:
                    self._jobs.remove(job)
                    try:
                        ok = await self._send(job)
                    except Exception as exc:
                        logger.error("send failed: %s", exc)
                        ok = False
                    self._last = time.monotonic()
                    self._sent.append(self._last)
                    if ok:
                        self.sent += 1
                    else:
                        self.dropped += 1
                    self._report(job, ok, "" if ok else "companion refused")
                    continue
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=max(0.05, min(wait, 5.0)))

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
