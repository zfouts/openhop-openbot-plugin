"""Is a newer OpenBot out? The same answer openHop's update button gives.

openHop's plugin manager checks GitHub Releases for the repository it
recorded at install (``state.json``, next to the plugin's data folder) and
offers the newest stable release that carries a wheel. Its HTTP API needs the
operator's login, which a plugin never has, so the bot asks GitHub itself,
with the same rules:

    GET https://api.github.com/repos/<owner>/<name>/releases?per_page=30
    -> drafts, pre-releases (``edge``) and releases without a .whl are skipped;
       the highest version wins, ordered the way pip orders them

Unauthenticated: GitHub allows 60 requests an hour per IP; the bot asks a
few times a day.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

API = "https://api.github.com"
DEFAULT_REPO = "zfouts/openhop-openbot-plugin"
TIMEOUT_S = 10.0
MAX_BYTES = 2 * 1024 * 1024

_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_VER_RE = re.compile(
    r"^(\d+(?:\.\d+){0,3})(?:[-.]?(dev|a|alpha|b|beta|c|rc|pre|preview)[-.]?(\d*))?$",
    re.IGNORECASE)
_PRE_RANK = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "c": 3, "rc": 3, "pre": 3,
             "preview": 3}

Get = Callable[[str, dict, float], Tuple[int, Any]]


class GitHubError(RuntimeError):
    pass


def version_key(v: Any) -> Optional[tuple]:
    """A sortable key for ``1.6.0``, ``v1.6.0``, ``1.6.0-rc.1``, ``1.6.0+edge.3``;
    None when it is not a version. Pre-releases sort below the release, a
    ``+local`` build above it, as in pip."""
    s = str(v or "").strip()
    s = s[1:] if s[:1] in ("v", "V") else s
    base, plus, local = s.partition("+")
    m = _VER_RE.match(base)
    if not m or (plus and not local):
        return None
    nums = tuple(int(x) for x in m.group(1).split("."))
    nums += (0,) * (4 - len(nums))
    pre = (1, 0, 0) if not m.group(2) else (0, _PRE_RANK[m.group(2).lower()], int(m.group(3) or 0))
    return (nums, pre, 1 if local else 0, local)


def newer(a: Any, b: Any) -> bool:
    """Is version ``a`` newer than ``b``? False when either is not a version."""
    ka, kb = version_key(a), version_key(b)
    return ka is not None and kb is not None and ka > kb


def repository(data_dir: Path) -> str:
    """The repository openHop updates this plugin from, or the default."""
    try:
        state = json.loads((Path(data_dir).parent / "state.json").read_text(encoding="utf-8"))
        repo = str(state.get("repository") or "").strip()
    except (OSError, ValueError, AttributeError):
        return DEFAULT_REPO
    return repo if _REPO_RE.match(repo) and ".." not in repo else DEFAULT_REPO


def _default_get(url: str, headers: dict, timeout: float) -> Tuple[int, Any]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https host
            status, raw = r.status, r.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise GitHubError("response larger than %d bytes" % MAX_BYTES)
    try:
        return status, json.loads(raw) if raw else None
    except ValueError as exc:
        raise GitHubError("response is not JSON") from exc


def latest_release(repo: str, *, user_agent: str = "openhop-openbot",
                   get: Optional[Get] = None) -> Optional[Dict[str, str]]:
    """``{"version", "tag", "url"}`` of the newest stable release with a wheel,
    or None when there is none. Raises GitHubError on any failure."""
    headers = {"User-Agent": user_agent, "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    try:
        status, data = (get or _default_get)("%s/repos/%s/releases?per_page=30" % (API, repo),
                                             headers, TIMEOUT_S)
    except GitHubError:
        raise
    except Exception as exc:
        raise GitHubError(str(exc) or type(exc).__name__) from exc
    if status != 200 or not isinstance(data, list):
        msg = data.get("message") if isinstance(data, dict) else None
        raise GitHubError("HTTP %s %s" % (status, msg or "unexpected response"))
    best, best_key = None, None
    for r in data:
        if not isinstance(r, dict) or r.get("draft") or r.get("prerelease"):
            continue
        assets = r.get("assets") if isinstance(r.get("assets"), list) else []
        if not any(isinstance(a, dict) and str(a.get("name") or "").endswith(".whl")
                   for a in assets):
            continue
        tag = str(r.get("tag_name") or "")
        key = version_key(tag)
        if key is not None and (best_key is None or key > best_key):
            url = r.get("html_url")
            best, best_key = {"version": tag[1:] if tag[:1] in "vV" else tag, "tag": tag,
                              "url": url if isinstance(url, str) else ""}, key
    return best
