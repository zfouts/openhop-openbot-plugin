"""Who the bot ignores, how hard one sender may push it, and replies to
senders heard without a region.

Bans come from two places. The operator's rules (``bans`` in config) are
regular expressions searched in the sender's name. Automatic bans are kept in
the store: with ``auto_ban.enabled``, a sender refused by the rate limits
``auto_ban.strikes`` times within ``auto_ban.window_s`` is ignored for
``auto_ban.ban_h`` hours.

A channel sender is only a name (whatever precedes ``: `` in the decrypted
text), so a ban is a ban on a name. On a DM the name is the contact's name,
or the first 8 hex digits of its public key when the contact is unknown.

Nudges (``nudges`` in config) are opt-in per channel: a reply to someone the
radio heard send a plain, unscoped flood on that channel, at most
``per_sender`` times per sender and ``per_channel`` times for the channel in
any ``window_h`` hours.

Pure: no I/O. The engine applies these against the store.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Mapping, Optional

# Node names are at most 31 bytes; nothing longer is a real node name, and a
# cap keeps a careless pattern from backtracking for long on a crafted one.
NAME_MAX = 64

USER_MAX_REQUESTS = 8        # answered requests per sender per window; 0 = off
USER_WINDOW_S = 600
WINDOW_S_RANGE = (10, 86400)

AUTO_BAN_DEFAULTS = {"enabled": False, "strikes": 5, "window_s": 600, "ban_h": 24}
STRIKES_RANGE = (1, 1000)
BAN_H_RANGE = (0.1, 8760)

NUDGE_TEXT = ("@[{sender}] your message had no region, so it flooded the whole mesh. "
              "Set a region in your app to keep it local.")
NUDGE_DEFAULTS = {"enabled": True, "region": "", "per_sender": 1, "per_channel": 6,
                  "window_h": 24, "text": NUDGE_TEXT}
PER_SENDER_RANGE = (1, 100)
PER_CHANNEL_RANGE = (1, 1000)
WINDOW_H_RANGE = (0.1, 720)


def sender_key(name: Any) -> str:
    """The identity limits and automatic bans are kept under."""
    return str(name or "").strip().lower()


@dataclass(frozen=True)
class BanRule:
    index: int               # position in config ``bans``, from 1
    pattern: str
    regex: "re.Pattern[str]"
    note: str = ""


def compile_rule(rule: Mapping[str, Any]) -> "re.Pattern[str]":
    """Compile one ``bans`` entry. Raises ``re.error`` on a bad pattern."""
    flags = re.IGNORECASE if rule.get("ignore_case", True) else 0
    return re.compile(str(rule.get("pattern") or ""), flags)


def compile_bans(rules: Iterable[Mapping[str, Any]]) -> List[BanRule]:
    """The enabled rules, compiled. Rules that do not compile are skipped
    (config validation has already reported them)."""
    out: List[BanRule] = []
    for i, rule in enumerate(rules or [], 1):
        if not isinstance(rule, Mapping) or rule.get("enabled") is False:
            continue
        pattern = str(rule.get("pattern") or "")
        if not pattern:
            continue
        try:
            regex = compile_rule(rule)
        except re.error:
            continue
        out.append(BanRule(i, pattern, regex, str(rule.get("note") or "")))
    return out


def match_ban(rules: Iterable[BanRule], name: Any) -> Optional[BanRule]:
    """The first rule whose pattern is found anywhere in ``name``. Anchor the
    pattern (``^bob$``) to match a whole name."""
    text = str(name or "").strip()[:NAME_MAX]
    for rule in rules:
        if rule.regex.search(text):
            return rule
    return None
