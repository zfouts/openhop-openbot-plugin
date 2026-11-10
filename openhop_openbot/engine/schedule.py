"""Announcement schedules: 5-field cron in the node's timezone, or ``every N``.

Small on purpose: minute hour day-of-month month day-of-week, each ``*``,
``*/n``, ``a``, ``a-b``, ``a-b/n`` or a comma list of those. Day-of-week is
0-7 with both 0 and 7 meaning Sunday. As in cron, when both day fields are
restricted a day matches if either does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import FrozenSet, Optional

_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_EVERY_RE = re.compile(r"^every\s+(\d+)\s*([smhd]?)$", re.I)
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def _field(spec: str, lo: int, hi: int) -> FrozenSet[int]:
    out = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
            if step < 1:
                raise ValueError("step must be >= 1")
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = b = int(part)
        if not (lo <= a <= hi and lo <= b <= hi and a <= b):
            raise ValueError(f"{part!r} out of range {lo}-{hi}")
        out.update(range(a, b + 1, step))
    return frozenset(out)


@dataclass(frozen=True)
class Schedule:
    every_s: Optional[int] = None
    fields: Optional[tuple] = None
    dom_any: bool = True
    dow_any: bool = True

    @classmethod
    def parse(cls, text: str) -> "Schedule":
        text = (text or "").strip()
        m = _EVERY_RE.match(text)
        if m:
            secs = int(m.group(1)) * _UNITS[m.group(2).lower()]
            if secs < 60:
                raise ValueError("every: minimum is 60 seconds")
            return cls(every_s=secs)
        parts = text.split()
        if len(parts) != 5:
            raise ValueError("schedule must be 5 cron fields or 'every N[smhd]'")
        fields = tuple(_field(p, lo, hi) for p, (lo, hi) in zip(parts, _RANGES))
        dow = frozenset(d % 7 for d in fields[4])
        return cls(fields=fields[:4] + (dow,), dom_any=parts[2] == "*", dow_any=parts[4] == "*")

    def matches(self, dt: datetime) -> bool:
        """Does local wall time ``dt`` (to the minute) fall on this cron?"""
        if self.fields is None:
            return False
        minute, hour, dom, month, dow = self.fields
        if dt.minute not in minute or dt.hour not in hour or dt.month not in month:
            return False
        cron_dow = (dt.weekday() + 1) % 7  # Python Monday=0 -> cron Sunday=0
        day_ok_dom = dt.day in dom
        day_ok_dow = cron_dow in dow
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return day_ok_dow
        if self.dow_any:
            return day_ok_dom
        return day_ok_dom or day_ok_dow
