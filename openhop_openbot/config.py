"""Config: shipped defaults deep-merged with the operator's config.json.

Under the plugin manager, ``$OPENHOP_PLUGIN_DATA/config.json`` is the file
the Plugins page edits. Objects merge key by key; lists replace (a command
list in config.json is the whole command list, not additions to the
defaults). Validation is lenient by design: a bad entry is reported and
skipped, never fatal, so one typo in the UI cannot take the bot off the air.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import re
import site
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .engine.schedule import Schedule

logger = logging.getLogger("openbot.config")

PLUGIN_ID = "openhop.openbot"
_DATA_REL = Path("share/openhop/plugins") / PLUGIN_ID / "config.default.json"
_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")


def _default_paths() -> List[Path]:
    candidates = [Path(__file__).resolve().parent.parent / "config.default.json"]
    for base in (sys.prefix, getattr(sys, "base_prefix", None), site.USER_BASE):
        if base:
            candidates.append(Path(base) / _DATA_REL)
    return candidates


def defaults() -> Dict[str, Any]:
    for candidate in _default_paths():
        try:
            return json.loads(candidate.read_text())
        except (OSError, ValueError):
            continue
    logger.warning("shipped config.default.json not found; starting from an empty config")
    return {}


def deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def data_dir() -> Path:
    return Path(os.environ.get("OPENHOP_PLUGIN_DATA") or ".").resolve()


def default_config_path() -> Path:
    return data_dir() / "config.json"


def read_json(path: Path) -> Dict[str, Any]:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must hold a JSON object")
    return raw


def validate(cfg: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Normalise ``cfg`` in place-ish; return (cfg, problems)."""
    problems: List[str] = []
    cfg = copy.deepcopy(cfg)

    chans = []
    seen_idx = set()
    for ch in cfg.get("channels") or []:
        try:
            idx = int(ch.get("idx"))
        except (TypeError, ValueError, AttributeError):
            problems.append(f"channel {ch!r}: idx must be a number")
            continue
        name = str(ch.get("name") or "").strip()
        secret = str(ch.get("secret") or "").strip()
        if not 1 <= idx <= 39:
            problems.append(f"channel {name or idx}: idx must be 1-39 (0 is Public)")
            continue
        if idx in seen_idx:
            problems.append(f"channel {name}: slot {idx} used twice")
            continue
        if not name or (not name.startswith("#") and not _HEX32.match(secret)):
            problems.append(f"channel {name or idx}: needs a #name or a 32-hex secret")
            continue
        seen_idx.add(idx)
        chans.append({"idx": idx, "name": name, **({"secret": secret} if secret else {})})
    cfg["channels"] = chans

    cmds = []
    seen_ids, seen_trig = set(), set()
    for i, c in enumerate(cfg.get("commands") or []):
        if not isinstance(c, dict):
            problems.append(f"command #{i + 1}: not an object")
            continue
        trig = str(c.get("trigger") or "").strip()
        cid = str(c.get("id") or trig.lstrip("!/") or f"cmd{i + 1}")
        if not trig:
            problems.append(f"command {cid}: empty trigger")
            continue
        if cid in seen_ids or trig.lower() in seen_trig:
            problems.append(f"command {cid}: duplicate id or trigger {trig}")
            continue
        try:
            cooldown = max(0.0, float(c.get("cooldown_s") or 0))
        except (TypeError, ValueError):
            problems.append(f"command {cid}: bad cooldown_s")
            cooldown = 10.0
        seen_ids.add(cid)
        seen_trig.add(trig.lower())
        cmds.append({**c, "id": cid, "trigger": trig, "cooldown_s": cooldown})
    # Aliases second, so an alias can never take a later command's trigger.
    for c in cmds:
        aliases = []
        for alias in _str_list(c.get("aliases")):
            if alias.lower() in seen_trig:
                problems.append(f"command {c['id']}: alias {alias} is already a trigger or "
                                "alias; dropped")
                continue
            seen_trig.add(alias.lower())
            aliases.append(alias)
        c["aliases"] = aliases
    cfg["commands"] = cmds

    anns = []
    for i, a in enumerate(cfg.get("announcements") or []):
        aid = str(a.get("id") or f"ann{i + 1}")
        try:
            Schedule.parse(str(a.get("schedule") or ""))
        except ValueError as exc:
            problems.append(f"announcement {aid}: {exc}")
            continue
        if not str(a.get("channel") or "").strip():
            problems.append(f"announcement {aid}: no channel")
            continue
        anns.append({**a, "id": aid})
    cfg["announcements"] = anns

    _validate_moderation(cfg, problems)

    hb = (cfg.get("node") or {}).get("path_hash_bytes")
    if hb not in (None, 1, 2, 3):
        problems.append(f"node.path_hash_bytes must be 1, 2 or 3 (got {hb!r})")
        cfg.setdefault("node", {})["path_hash_bytes"] = None

    from .mesh.airtime import DEFAULT_PREAMBLE, PREAMBLE_RANGE
    if isinstance(cfg.get("node"), dict):
        _number(cfg["node"], "preamble_symbols", DEFAULT_PREAMBLE, *PREAMBLE_RANGE,
                "node.preamble_symbols", problems, integer=True)

    name = str((cfg.get("node") or {}).get("name") or "").strip()
    if len(name.encode("utf-8")) > 31:
        problems.append("node.name is %d bytes; the radio keeps at most 31, so it is not "
                        "applied to the radio" % len(name.encode("utf-8")))

    from .services.shortener import CHOICES, CUSTOM, custom_base
    cs = cfg.get("corescope") or {}
    sh = cs.get("shortener")
    if sh not in (None, "") and str(sh).strip().lower() not in CHOICES:
        problems.append(f"corescope.shortener must be one of {', '.join(CHOICES)} (got {sh!r})")
    elif str(sh or "").strip().lower() == CUSTOM:
        try:
            custom_base(cs.get("shortener_url"))
        except ValueError as exc:
            problems.append(f"corescope.shortener_url: {exc}")

    from .engine.mpath import DEFAULT_WAIT_S, MAX_WAIT_S, MIN_WAIT_S, parse_wait
    w = cs.get("mpath_wait_s")
    if w not in (None, "") and parse_wait(w) is None:
        problems.append(f"corescope.mpath_wait_s must be {MIN_WAIT_S}-{MAX_WAIT_S} seconds "
                        f"(got {w!r}); using {DEFAULT_WAIT_S}")
    return cfg, problems


def _str_list(v: Any) -> List[str]:
    """A list of non-empty strings, from a list or a comma-separated string."""
    if isinstance(v, str):
        v = v.split(",")
    if not isinstance(v, (list, tuple)):
        return []
    return [str(x).strip() for x in v if str(x).strip()]


def _number(obj: Dict[str, Any], key: str, default: float, lo: float, hi: float,
            what: str, problems: List[str], integer: bool = False) -> float:
    """``obj[key]`` as a number in [lo, hi]; the default (with a problem) otherwise."""
    v = obj.get(key)
    if v is None or v == "":
        obj[key] = default
        return default
    try:
        if isinstance(v, bool):
            raise ValueError
        n = float(v)
        if not math.isfinite(n) or (integer and n != int(n)):
            raise ValueError
    except (TypeError, ValueError):
        n = None
    if n is None or not lo <= n <= hi:
        problems.append(f"{what} must be {lo:g}-{hi:g} (got {v!r}); using {default:g}")
        n = default
    obj[key] = int(n) if integer else n
    return obj[key]


def _validate_moderation(cfg: Dict[str, Any], problems: List[str]) -> None:
    """bans, limits.user_max_requests / user_window_s, auto_ban and nudges."""
    from .engine import moderation as mod
    from .engine.bot import chan_norm

    def section(key: str, kind: type, what: str) -> Any:
        v = cfg.get(key)
        if v is None and kind is list:
            return []                   # null is as good as an empty list
        if not isinstance(v, kind):
            problems.append(f"{key} must be {what}; ignored")
            return kind()
        return v

    # Every entry keeps its place, usable or not, so "ban #3" is the third
    # entry the operator wrote; compile_bans skips the unusable ones.
    bans = []
    for i, b in enumerate(section("bans", list, "a list"), 1):
        if isinstance(b, str):
            b = {"pattern": b}
        if not isinstance(b, dict):
            problems.append(f"ban #{i}: not an object; skipped")
            bans.append({"pattern": "", "enabled": False})
            continue
        rule = {**b, "pattern": str(b.get("pattern") or ""),
                "ignore_case": b.get("ignore_case", True) is not False,
                "enabled": b.get("enabled", True) is not False}
        why = mod.rule_problem(rule)
        if why is not None:
            problems.append(f"ban #{i} {rule['pattern']!r}: {why}; skipped")
        bans.append(rule)
    cfg["bans"] = bans

    limits = cfg["limits"] = dict(section("limits", dict, "an object"))
    _number(limits, "user_max_requests", mod.USER_MAX_REQUESTS, 0, 1000,
            "limits.user_max_requests", problems, integer=True)
    _number(limits, "user_window_s", mod.USER_WINDOW_S, *mod.WINDOW_S_RANGE,
            "limits.user_window_s", problems)

    ab = {**mod.AUTO_BAN_DEFAULTS, **section("auto_ban", dict, "an object")}
    ab["enabled"] = ab.get("enabled") is True
    _number(ab, "strikes", mod.AUTO_BAN_DEFAULTS["strikes"], *mod.STRIKES_RANGE,
            "auto_ban.strikes", problems, integer=True)
    _number(ab, "window_s", mod.AUTO_BAN_DEFAULTS["window_s"], *mod.WINDOW_S_RANGE,
            "auto_ban.window_s", problems)
    _number(ab, "ban_h", mod.AUTO_BAN_DEFAULTS["ban_h"], *mod.BAN_H_RANGE,
            "auto_ban.ban_h", problems)
    cfg["auto_ban"] = ab
    if ab["enabled"] and not limits.get("user_max_requests"):
        problems.append("auto_ban counts refusals by the per-sender cap, which is off "
                        "(limits.user_max_requests 0): nobody will be banned")

    protected = {chan_norm(c) for c in _str_list((cfg.get("scope") or {}).get(
        "protected_channels"))}
    nudges, seen = [], set()
    for i, n in enumerate(section("nudges", list, "a list"), 1):
        if not isinstance(n, dict):
            problems.append(f"nudge #{i}: not an object")
            continue
        ch = str(n.get("channel") or "").strip()
        if not ch:
            problems.append(f"nudge #{i}: no channel")
            continue
        if chan_norm(ch) in protected:
            problems.append(f"nudge for {ch}: protected channel, the bot never sends there; "
                            "skipped")
            continue
        if chan_norm(ch) in seen:
            problems.append(f"nudge for {ch}: a channel can have only one; skipped")
            continue
        rule = {**mod.NUDGE_DEFAULTS, **n, "channel": ch,
                "enabled": n.get("enabled", True) is not False}
        rule["text"] = str(rule.get("text") or "").strip()
        if not rule["text"]:
            problems.append(f"nudge for {ch}: empty text; skipped")
            continue
        region = str(rule.get("region") or "").strip()
        if region not in ("", "*"):
            from .mesh.frames import canonical_region, region_key
            try:
                region_key(region)
                region = canonical_region(region)
            except ValueError:
                problems.append(f"nudge for {ch}: region {region!r} has no key; using the "
                                "companion default")
                region = ""
        rule["region"] = region
        what = f"nudge for {ch}:"
        _number(rule, "per_sender", mod.NUDGE_DEFAULTS["per_sender"], *mod.PER_SENDER_RANGE,
                what + " per_sender", problems, integer=True)
        _number(rule, "per_channel", mod.NUDGE_DEFAULTS["per_channel"],
                *mod.PER_CHANNEL_RANGE, what + " per_channel", problems, integer=True)
        _number(rule, "window_h", mod.NUDGE_DEFAULTS["window_h"], *mod.WINDOW_H_RANGE,
                what + " window_h", problems)
        seen.add(chan_norm(ch))
        nudges.append(rule)
    cfg["nudges"] = nudges


def load(path: Path) -> Tuple[Dict[str, Any], List[str]]:
    return validate(deep_merge(defaults(), read_json(path)))
