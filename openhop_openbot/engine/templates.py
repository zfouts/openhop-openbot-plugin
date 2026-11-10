"""Reply templates: plain text with ``{placeholders}``.

The rules:

* ``{name}`` is a context value or a built-in with no argument.
* ``{name some text}`` passes ``some text`` to the built-in.
* Two substitution passes, so one level of nesting works: ``{roll {args}}``.
* Unknown names stay literal; a built-in that raises renders as ``∅``.
* Substituted values have their braces neutralised, so a node that names
  itself ``{stats}`` cannot inject a placeholder into the second pass.

The result is clamped to one LoRa frame in UTF-8 bytes, never mid-codepoint.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, Mapping

log = logging.getLogger("openbot.templates")

MAX_TEXT_BYTES = 160

Builtin = Callable[[Dict[str, Any], str | None], Any]

_TMPL_RE = re.compile(r"\{(\w+)(?:[ \t]+([^{}]*))?\}")


def clamp_bytes(text: str, limit: int = MAX_TEXT_BYTES) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    cut = limit
    while cut > 0 and (raw[cut] & 0xC0) == 0x80:
        cut -= 1
    return raw[:cut].decode("utf-8", errors="ignore")


def render(tmpl: str, ctx: Dict[str, Any], fns: Mapping[str, Builtin],
           limit: int = MAX_TEXT_BYTES) -> str:
    def sub(m: re.Match) -> str:
        fn = fns.get(m.group(1))
        if fn is None:
            return m.group(0)
        try:
            val = fn(ctx, m.group(2))
            val = "" if val is None else str(val)
            return val.replace("\n", " ").replace("{", "(").replace("}", ")")
        except Exception as exc:  # a broken built-in must not drop the reply
            log.warning("built-in %s failed: %s", m.group(1), exc)
            return "∅"

    out = _TMPL_RE.sub(sub, tmpl or "")
    if "{" in out:
        out = _TMPL_RE.sub(sub, out)
    return clamp_bytes(out.strip(), limit)
