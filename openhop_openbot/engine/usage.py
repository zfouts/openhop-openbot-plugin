"""Command-usage reporting: pure helpers that turn recorded rows into a report.

No I/O here. ``parse_window`` turns mailbox args into an epoch window and
``summarize`` folds the store's usage rows into the shape the plugin page draws.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Mapping, Sequence
from zoneinfo import ZoneInfo

RANGES = {
    "24h": (86400, "Last 24 hours"),
    "7d": (7 * 86400, "Last 7 days"),
    "30d": (30 * 86400, "Last 30 days"),
    "90d": (90 * 86400, "Last 90 days"),
    "1y": (365 * 86400, "Last year"),
}
MAX_SPAN_DAYS = 366
HOURLY_MAX_S = 48 * 3600
TOP_REQUESTERS = 10
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def skip_category(reason: str) -> str:
    low = (reason or "").lower()
    if "cooldown" in low:
        return "cooldown"
    if ("not in scope" in low or "protected channel" in low or "dms not allowed" in low
            or "dm" in low.split() or "hops >" in low):
        return "out of scope"
    if "reply rendered empty" in low:
        return "empty reply"
    return "other"


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except Exception:
        raise ValueError("unknown timezone %r" % tz) from None


def _parse_date(v: Any, what: str) -> date:
    try:
        return date.fromisoformat(str(v).strip())
    except ValueError:
        raise ValueError("%s date %r is not YYYY-MM-DD" % (what, v)) from None


def _midnight(d: date, zone: ZoneInfo) -> float:
    return datetime(d.year, d.month, d.day, tzinfo=zone).timestamp()


def parse_window(args: Mapping[str, Any], now: float, tz: str) -> Dict[str, Any]:
    zone = _zone(tz)
    if args.get("range"):
        key = str(args["range"]).strip().lower()
        if key not in RANGES:
            raise ValueError("unknown range %r (use %s)" % (args["range"], ", ".join(RANGES)))
        secs, label = RANGES[key]
        start, end = now - secs, now
    else:
        if not args.get("from") or not args.get("to"):
            raise ValueError("give a range or both from and to dates")
        d0, d1 = _parse_date(args["from"], "from"), _parse_date(args["to"], "to")
        if d0 > d1:
            raise ValueError("from date is after to date")
        if (d1 - d0).days + 1 > MAX_SPAN_DAYS:
            raise ValueError("span is over %d days" % MAX_SPAN_DAYS)
        start = _midnight(d0, zone)
        if start > now:
            raise ValueError("from date is in the future")
        end = min(_midnight(d1 + timedelta(days=1), zone), now)
        label = d0.isoformat() if d0 == d1 else "%s – %s" % (d0.isoformat(), d1.isoformat())
    return {"from": start, "to": end, "label": label,
            "bucket": "hour" if end - start <= HOURLY_MAX_S else "day"}


def _bucket_starts(start: float, end: float, bucket: str, zone: ZoneInfo) -> List[float]:
    out: List[float] = []
    first = datetime.fromtimestamp(start, zone)
    if bucket == "hour":
        t = first.replace(minute=0, second=0, microsecond=0).timestamp()
        while t < end:
            out.append(t)
            t += 3600
        return out
    d = first.date()
    while True:
        t = _midnight(d, zone)
        if t >= end:
            break
        out.append(t)
        d += timedelta(days=1)
    return out


def _label(t: float, bucket: str, zone: ZoneInfo) -> str:
    dt = datetime.fromtimestamp(t, zone)
    if bucket == "hour":
        return "%02d:00" % dt.hour
    return "%s %d" % (_MONTHS[dt.month - 1], dt.day)


def _ranked(counter: Counter) -> List[Dict[str, Any]]:
    return [{"name": k, "requests": v}
            for k, v in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))]


def summarize(rows: Sequence[Mapping[str, Any]], window: Mapping[str, Any], tz: str,
              commands: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    zone = _zone(tz)
    bucket = window["bucket"]
    starts = _bucket_starts(window["from"], window["to"], bucket, zone)
    counts: List[Dict[str, int]] = [{} for _ in starts]
    per_cmd: Dict[str, Dict[str, int]] = {}
    requesters: Counter = Counter()
    channels: Counter = Counter()
    regions: Counter = Counter()
    skipped: Counter = Counter()
    answered = 0
    for r in rows:
        ok = r["outcome"] == "answered"
        c = per_cmd.setdefault(r["cmd_id"], {"answered": 0, "skipped": 0})
        c["answered" if ok else "skipped"] += 1
        answered += ok
        if not ok:
            skipped[r.get("reason") or "other"] += 1
        requesters[r["sender"]] += 1
        channels[r["channel"]] += 1
        regions[r.get("region") or "?"] += 1
        i = bisect_right(starts, r["ts"]) - 1
        if 0 <= i < len(starts):
            counts[i][r["cmd_id"]] = counts[i].get(r["cmd_id"], 0) + 1
    triggers = {c.get("id"): c.get("trigger") for c in commands}
    cmds = [{"id": cid, "trigger": triggers.get(cid) or cid, **v} for cid, v in per_cmd.items()]
    cmds.sort(key=lambda c: (-(c["answered"] + c["skipped"]), c["id"]))
    total = len(rows)
    return {
        "tz": tz, "from": window["from"], "to": window["to"],
        "label": window["label"], "bucket": bucket,
        "totals": {"requests": total, "answered": answered, "skipped": total - answered},
        "commands": cmds,
        "series": [{"t": t, "label": _label(t, bucket, zone), "counts": c}
                   for t, c in zip(starts, counts)],
        "requesters": _ranked(requesters)[:TOP_REQUESTERS],
        "channels": _ranked(channels),
        "regions": _ranked(regions),
        "skipped": [{"reason": k, "count": v}
                    for k, v in sorted(skipped.items(), key=lambda kv: (-kv[1], kv[0]))],
    }
