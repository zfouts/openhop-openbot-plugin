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

    hb = (cfg.get("node") or {}).get("path_hash_bytes")
    if hb not in (None, 1, 2, 3):
        problems.append(f"node.path_hash_bytes must be 1, 2 or 3 (got {hb!r})")
        cfg.setdefault("node", {})["path_hash_bytes"] = None

    name = str((cfg.get("node") or {}).get("name") or "").strip()
    if len(name.encode("utf-8")) > 31:
        problems.append("node.name is %d bytes; the radio keeps at most 31, so it is not "
                        "applied to the radio" % len(name.encode("utf-8")))

    from .services.shortener import CHOICES
    sh = (cfg.get("corescope") or {}).get("shortener")
    if sh not in (None, "") and str(sh).strip().lower() not in CHOICES:
        problems.append(f"corescope.shortener must be one of {', '.join(CHOICES)} (got {sh!r})")
    return cfg, problems


def load(path: Path) -> Tuple[Dict[str, Any], List[str]]:
    return validate(deep_merge(defaults(), read_json(path)))
