"""Short links for `!path` map URLs, from a choice of keyless shorteners.

Each service is one GET that answers with the short URL as plain text:

    v.gd / is.gd   https://v.gd/create.php?format=simple&url=<encoded>
                   (one operator, one API: https://v.gd/apishorteningreference.php;
                   errors are "Error: <message>" with 400/406/502/503, 406 =
                   rate limited; 200 links an hour per IP)
    tinyurl.com    https://tinyurl.com/api-create.php?url=<encoded>
    da.gd          https://da.gd/s?url=<encoded>
    custom         <base>/create.php?format=simple[&key=<key>]&url=<encoded>
                   (your own service speaking v.gd's API, optionally keyed)

Services that need an account token (Bitly, TinyURL's v2 API) are left out.
A reply is accepted only if it is a plain https link on the chosen service's
own host. The long URL (a CoreScope map link: packet hash and hop hashes) is
all that is sent, and each one is shortened once per run. A custom key never
appears in logs or error messages.
"""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import quote, urlsplit

log = logging.getLogger("openbot.shortener")

TIMEOUT_S = 5.0
MAX_BYTES = 2048
CACHE_MAX = 256
DEFAULT = "v.gd"
NONE = "none"
CUSTOM = "custom"

# service -> (API URL before the encoded long URL, accepted short-link pattern)
SERVICES: Dict[str, Tuple[str, "re.Pattern[str]"]] = {
    "v.gd": ("https://v.gd/create.php?format=simple&url=",
             re.compile(r"^https://v\.gd/[A-Za-z0-9_]+$")),
    "is.gd": ("https://is.gd/create.php?format=simple&url=",
              re.compile(r"^https://is\.gd/[A-Za-z0-9_]+$")),
    "tinyurl.com": ("https://tinyurl.com/api-create.php?url=",
                    re.compile(r"^https://tinyurl\.com/[A-Za-z0-9_-]+$")),
    "da.gd": ("https://da.gd/s?url=",
              re.compile(r"^https://da\.gd/[A-Za-z0-9_-]+$")),
}
CHOICES = tuple(SERVICES) + (CUSTOM, NONE)

Get = Callable[[str, dict, float], Tuple[int, str]]


class ShortenError(RuntimeError):
    pass


def custom_base(url: Optional[str]) -> str:
    """The normalised base URL of a custom shortener, or ValueError.

    Must be https (the short link goes out on the air for anyone to open),
    with a host and no query or fragment. A path is allowed, for a service
    that lives under one: ``https://example.org/s``."""
    base = (url or "").strip().rstrip("/")
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment \
            or parts.username or parts.password or any(c.isspace() for c in base):
        raise ValueError("custom shortener URL must be an https:// address, for example "
                         "https://short.example.org")
    return "https://" + parts.netloc.lower() + parts.path


def _read_text(fp) -> str:
    raw = fp.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ShortenError("response larger than %d bytes" % MAX_BYTES)
    return raw.decode("utf-8", errors="replace").strip()


def _default_get(url: str, headers: dict, timeout: float) -> Tuple[int, str]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https hosts
            return r.status, _read_text(r)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, _read_text(exc)
        except ShortenError:
            return exc.code, ""


class Shortener:
    def __init__(self, service: str = DEFAULT, *, user_agent: str = "",
                 get: Optional[Get] = None, base_url: str = "", key: str = ""):
        self.service = service
        self._key = ""
        if service == CUSTOM:
            base = custom_base(base_url)
            self._key = (key or "").strip()
            self._api = base + "/create.php?format=simple" + (
                "&key=" + quote(self._key, safe="") if self._key else "") + "&url="
            self._short_re = re.compile("^" + re.escape(base) + r"/[A-Za-z0-9_-]+$")
            self.label = urlsplit(base).netloc
        elif service in SERVICES:
            self._api, self._short_re = SERVICES[service]
            self.label = service
        else:
            raise ValueError("unknown shortener %r; choose one of %s"
                             % (service, ", ".join(CHOICES)))
        self.user_agent = user_agent or "openhop-openbot"
        self._get = get or _default_get
        self._cache: Dict[str, str] = {}

    def shorten(self, long_url: str) -> str:
        """The short link for ``long_url``. Raises ShortenError on any failure."""
        hit = self._cache.get(long_url)
        if hit:
            return hit
        url = self._api + quote(long_url, safe="")
        headers = {"User-Agent": self.user_agent, "Accept": "text/plain"}
        try:
            status, body = self._get(url, headers, TIMEOUT_S)
        except ShortenError as exc:
            raise ShortenError(self._redact(str(exc))) from None
        except Exception as exc:
            raise ShortenError(self._redact(str(exc) or type(exc).__name__)) from None
        body = (body or "").strip()
        if status != 200 or not self._short_re.match(body):
            raise ShortenError(self._redact("%s: HTTP %s %s" % (
                self.label, status, body[:80] or "empty response")))
        if len(self._cache) >= CACHE_MAX:
            self._cache.clear()
        self._cache[long_url] = body
        return body

    def _redact(self, text: str) -> str:
        if self._key:
            text = text.replace(quote(self._key, safe=""), "***").replace(self._key, "***")
        return text


def make_shortener(name: Optional[str], *, user_agent: str = "",
                   get: Optional[Get] = None, base_url: str = "",
                   key: str = "") -> Optional[Shortener]:
    """The configured shortener, or None for "none" (or empty). Raises
    ValueError for an unknown name, or for "custom" without a valid
    https base URL."""
    name = (name or "").strip().lower()
    if name in ("", NONE):
        return None
    return Shortener(name, user_agent=user_agent, get=get, base_url=base_url, key=key)
