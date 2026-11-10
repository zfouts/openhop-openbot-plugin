"""Local state: who this node has heard, what was said, and command cooldowns.

The bot only knows what its own radio has heard, so it keeps a small SQLite
roster built from verified adverts, the companion's contact table and channel
traffic.

One connection, shared across the event loop and the worker threads that
render templates, serialised by a lock. Every method is short; nothing holds
the lock across I/O.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    pubkey      TEXT PRIMARY KEY,      -- lowercase hex, full 64 chars when known
    name        TEXT NOT NULL DEFAULT '',
    type        TEXT NOT NULL DEFAULT 'unknown',
    lat         REAL,
    lon         REAL,
    first_heard INTEGER NOT NULL,
    last_heard  INTEGER NOT NULL,
    last_snr    REAL,
    last_hops   INTEGER,
    last_path   TEXT,
    adverts     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS nodes_name ON nodes (name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS nodes_heard ON nodes (last_heard);

CREATE TABLE IF NOT EXISTS msgs (
    id       INTEGER PRIMARY KEY,
    ts       INTEGER NOT NULL,
    channel  TEXT NOT NULL,            -- '#bot', or 'dm'
    sender   TEXT NOT NULL,
    text     TEXT NOT NULL,
    region   TEXT,
    hops_n   INTEGER,
    path     TEXT
);
CREATE INDEX IF NOT EXISTS msgs_ts ON msgs (ts);
CREATE INDEX IF NOT EXISTS msgs_sender ON msgs (sender COLLATE NOCASE, ts);

-- Relay adjacency seen in hop chains: a forwarded b (hash prefixes).
CREATE TABLE IF NOT EXISTS edges (
    a     TEXT NOT NULL,
    b     TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    last  INTEGER NOT NULL,
    PRIMARY KEY (a, b)
);

CREATE TABLE IF NOT EXISTS fires (
    cmd_id     TEXT NOT NULL,
    scope      TEXT NOT NULL,
    last_fired REAL NOT NULL,
    PRIMARY KEY (cmd_id, scope)
);

CREATE TABLE IF NOT EXISTS usage (
    id      INTEGER PRIMARY KEY,
    ts      REAL NOT NULL,        -- epoch seconds the request was heard
    cmd_id  TEXT NOT NULL,
    outcome TEXT NOT NULL,        -- 'answered' | 'skipped'
    reason  TEXT,                 -- skip category, NULL when answered
    channel TEXT NOT NULL,        -- '#bot' etc., or 'dm'
    region  TEXT,
    sender  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_ts ON usage (ts);
"""

MSG_RETENTION_S = 7 * 86400
EDGE_RETENTION_S = 14 * 86400
NODE_RETENTION_S = 60 * 86400
USAGE_RETENTION_S = 366 * 86400


def _row(r: sqlite3.Row) -> Dict[str, Any]:
    return {k: r[k] for k in r.keys()}


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _q(self, sql: str, args: Iterable[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, tuple(args)).fetchall()

    def _x(self, sql: str, args: Iterable[Any] = ()) -> int:
        with self._lock:
            return self._db.execute(sql, tuple(args)).rowcount

    # ------------------------------------------------------------ roster ----

    def upsert_node(
        self,
        pubkey: str,
        *,
        name: str = "",
        type: str = "unknown",
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        heard: Optional[int] = None,
        snr: Optional[float] = None,
        hops: Optional[int] = None,
        path: Optional[str] = None,
        advert: bool = False,
    ) -> None:
        """Insert or refresh a node. Empty/None fields never erase known ones.

        (0, 0) is "no position" in MeshCore, never a real fix.
        """
        pubkey = (pubkey or "").lower()
        if not pubkey:
            return
        if lat is not None and lon is not None and abs(lat) < 1e-6 and abs(lon) < 1e-6:
            lat = lon = None
        now = int(heard or time.time())
        self._x(
            """INSERT INTO nodes (pubkey, name, type, lat, lon, first_heard, last_heard,
                                  last_snr, last_hops, last_path, adverts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (pubkey) DO UPDATE SET
                 name = CASE WHEN excluded.name <> '' THEN excluded.name ELSE nodes.name END,
                 type = CASE WHEN excluded.type <> 'unknown' THEN excluded.type
                             ELSE nodes.type END,
                 lat = COALESCE(excluded.lat, nodes.lat),
                 lon = COALESCE(excluded.lon, nodes.lon),
                 last_heard = MAX(nodes.last_heard, excluded.last_heard),
                 last_snr = COALESCE(excluded.last_snr, nodes.last_snr),
                 last_hops = COALESCE(excluded.last_hops, nodes.last_hops),
                 last_path = COALESCE(excluded.last_path, nodes.last_path),
                 adverts = nodes.adverts + excluded.adverts""",
            (pubkey, name or "", type or "unknown", lat, lon, now, now, snr, hops, path,
             1 if advert else 0),
        )

    def candidates(self, prefix: str) -> List[Dict[str, Any]]:
        """Nodes whose pubkey starts with a hop hash, for the path resolver."""
        prefix = prefix.lower()
        rows = self._q(
            """SELECT pubkey, name, type, lat, lon, last_heard FROM nodes
               WHERE substr(pubkey, 1, ?) = ? ORDER BY last_heard DESC LIMIT 50""",
            (len(prefix), prefix),
        )
        return [_row(r) for r in rows]

    def find_name(self, needle: str) -> Optional[Dict[str, Any]]:
        """Best node whose name contains ``needle``: exact match first, then newest."""
        needle = needle.strip()
        if not needle:
            return None
        rows = self._q(
            """SELECT * FROM nodes WHERE name LIKE '%' || ? || '%' ESCAPE '\\'
               ORDER BY (lower(name) = lower(?)) DESC, last_heard DESC LIMIT 1""",
            (needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"), needle),
        )
        return _row(rows[0]) if rows else None

    def position_of(self, name: str) -> Optional[tuple[float, float]]:
        """A node's position by exact name, only when exactly one pubkey has it.

        Two nodes sharing a name would let one steer the other's distance.
        """
        if not name:
            return None
        rows = self._q(
            "SELECT lat, lon FROM nodes WHERE name = ? COLLATE NOCASE", (name,)
        )
        if len(rows) != 1 or rows[0]["lat"] is None:
            return None
        return (rows[0]["lat"], rows[0]["lon"])

    def whois(self, prefix: str, limit: int = 3) -> List[str]:
        rows = self._q(
            """SELECT DISTINCT name FROM nodes WHERE substr(pubkey, 1, ?) = ? AND name <> ''
               ORDER BY last_heard DESC LIMIT ?""",
            (len(prefix), prefix.lower(), limit),
        )
        return [r["name"] for r in rows]

    def recent_nodes(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._q(
            "SELECT * FROM nodes ORDER BY last_heard DESC LIMIT ?", (limit,)
        )
        return [_row(r) for r in rows]

    def counts(self) -> Dict[str, int]:
        r = self._q(
            """SELECT (SELECT count(*) FROM nodes) AS nodes,
                      (SELECT count(*) FROM nodes WHERE type = 'repeater') AS repeaters,
                      (SELECT count(*) FROM msgs WHERE ts > ?) AS msgs24""",
            (int(time.time()) - 86400,),
        )[0]
        return {"nodes": r["nodes"], "repeaters": r["repeaters"], "msgs24": r["msgs24"]}

    def heard_since(self, since: int, type: Optional[str] = None) -> int:
        if type:
            return self._q(
                "SELECT count(*) AS n FROM nodes WHERE last_heard >= ? AND type = ?",
                (since, type),
            )[0]["n"]
        return self._q("SELECT count(*) AS n FROM nodes WHERE last_heard >= ?", (since,))[0]["n"]

    def new_since(self, since: int) -> List[str]:
        rows = self._q(
            """SELECT name FROM nodes WHERE first_heard >= ? AND name <> ''
               ORDER BY first_heard DESC LIMIT 10""",
            (since,),
        )
        return [r["name"] for r in rows]

    # ------------------------------------------------------------- traffic --

    def record_msg(
        self,
        *,
        channel: str,
        sender: str,
        text: str,
        region: Optional[str] = None,
        hops_n: Optional[int] = None,
        path: Optional[str] = None,
        ts: Optional[int] = None,
    ) -> None:
        self._x(
            """INSERT INTO msgs (ts, channel, sender, text, region, hops_n, path)
               VALUES (?,?,?,?,?,?,?)""",
            (int(ts or time.time()), channel, sender, text, region, hops_n, path),
        )

    def record_usage(
        self,
        *,
        ts: float,
        cmd_id: str,
        outcome: str,
        reason: Optional[str],
        channel: str,
        region: Optional[str],
        sender: str,
    ) -> None:
        self._x(
            """INSERT INTO usage (ts, cmd_id, outcome, reason, channel, region, sender)
               VALUES (?,?,?,?,?,?,?)""",
            (ts, cmd_id, outcome, reason, channel, region, sender),
        )

    def usage_rows(self, since: float, until: float) -> List[Dict[str, Any]]:
        rows = self._q(
            """SELECT ts, cmd_id, outcome, reason, channel, region, sender FROM usage
               WHERE ts >= ? AND ts < ? ORDER BY ts, id""",
            (since, until),
        )
        return [_row(r) for r in rows]

    def last_msg(self, sender: str) -> Optional[Dict[str, Any]]:
        rows = self._q(
            """SELECT * FROM msgs WHERE sender = ? COLLATE NOCASE
               ORDER BY ts DESC LIMIT 1""",
            (sender.strip(),),
        )
        return _row(rows[0]) if rows else None

    def top_talkers(self, since: int, n: int = 3) -> List[tuple[str, int]]:
        rows = self._q(
            """SELECT sender, count(*) AS c FROM msgs WHERE ts >= ?
               GROUP BY sender COLLATE NOCASE ORDER BY c DESC LIMIT ?""",
            (since, n),
        )
        return [(r["sender"], r["c"]) for r in rows]

    def record_chain(self, hops: List[str], ts: Optional[int] = None) -> None:
        """Record relay adjacency from one hop chain (travel order)."""
        now = int(ts or time.time())
        for a, b in zip(hops, hops[1:]):
            self._x(
                """INSERT INTO edges (a, b, count, last) VALUES (?,?,1,?)
                   ON CONFLICT (a, b) DO UPDATE SET count = count + 1, last = excluded.last""",
                (a.lower(), b.lower(), now),
            )
        if hops:
            # the last relay forwarded to us
            self._x(
                """INSERT INTO edges (a, b, count, last) VALUES (?, '', 1, ?)
                   ON CONFLICT (a, b) DO UPDATE SET count = count + 1, last = excluded.last""",
                (hops[-1].lower(), now),
            )

    def top_relays(self, since: int, n: int = 3) -> List[tuple[str, int]]:
        rows = self._q(
            """SELECT a, sum(count) AS c FROM edges WHERE last >= ?
               GROUP BY a ORDER BY c DESC LIMIT ?""",
            (since, n),
        )
        out = []
        for r in rows:
            names = self.whois(r["a"], 1)
            out.append((names[0] if names else r["a"], r["c"]))
        return out

    # ----------------------------------------------------------- cooldowns --

    def claim(self, cmd_id: str, scope: str, cooldown_s: float,
              now: Optional[float] = None) -> bool:
        """Take one cooldown window for (command, scope). Atomic under the lock."""
        now = time.time() if now is None else now
        with self._lock:
            rows = self._db.execute(
                "SELECT last_fired FROM fires WHERE cmd_id = ? AND scope = ?", (cmd_id, scope)
            ).fetchall()
            if rows and now - rows[0][0] < cooldown_s:
                return False
            self._db.execute(
                """INSERT INTO fires (cmd_id, scope, last_fired) VALUES (?,?,?)
                   ON CONFLICT (cmd_id, scope) DO UPDATE SET last_fired = excluded.last_fired""",
                (cmd_id, scope, now),
            )
            return True

    def release(self, cmd_id: str, scope: str) -> None:
        self._x("DELETE FROM fires WHERE cmd_id = ? AND scope = ?", (cmd_id, scope))

    def clear_fires(self) -> int:
        return self._x("DELETE FROM fires")

    # ---------------------------------------------------------- retention --

    def prune(self, now: Optional[int] = None) -> None:
        now = int(now or time.time())
        self._x("DELETE FROM msgs WHERE ts < ?", (now - MSG_RETENTION_S,))
        self._x("DELETE FROM edges WHERE last < ?", (now - EDGE_RETENTION_S,))
        self._x("DELETE FROM nodes WHERE last_heard < ?", (now - NODE_RETENTION_S,))
        self._x("DELETE FROM fires WHERE last_fired < ?", (now - 7 * 86400,))
        self._x("DELETE FROM usage WHERE ts < ?", (now - USAGE_RETENTION_S,))
