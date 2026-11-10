import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from openhop_openbot.engine.builtins import fn_path
from openhop_openbot.services.corescope import CoreScope
from openhop_openbot.services.shortener import (
    SERVICES,
    Shortener,
    ShortenError,
    _default_get,
    make_shortener,
)
from openhop_openbot.store import Store

PKT = "956db1890eba6b1d"
LONG = "https://cs.example/#/map?packet=956DB1890EBA6B1D&hops=90e2,fe27"


class FakeGet:
    def __init__(self, status=200, body="https://v.gd/AbC123"):
        self.status, self.body = status, body
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers, timeout))
        return self.status, self.body


def shortened_url(call_url):
    q = parse_qs(urlsplit(call_url).query)
    return q["url"][0], q["format"][0]


# ------------------------------------------------------------- shortener --

def test_shorten_calls_vgd_with_the_encoded_url():
    get = FakeGet()
    assert Shortener("v.gd", user_agent="ua", get=get).shorten(LONG) == "https://v.gd/AbC123"
    url, headers, timeout = get.calls[0]
    assert url.startswith("https://v.gd/create.php?")
    assert "#" not in url and "&hops" not in url              # the long URL is encoded whole
    assert shortened_url(url) == (LONG, "simple")
    assert headers["User-Agent"] == "ua" and timeout <= 5


def test_shorten_caches_by_long_url():
    get = FakeGet()
    s = Shortener("v.gd", get=get)
    assert s.shorten(LONG) == s.shorten(LONG)
    assert len(get.calls) == 1


@pytest.mark.parametrize("status,body", [
    (406, "Error: rate limit exceeded"), (200, "Error: Please specify a URL to shorten."),
    (502, ""), (200, "javascript:alert(1)"), (200, "https://evil.example/x"),
    (200, "https://v.gd/has space"),
])
def test_shorten_failures_raise(status, body):
    with pytest.raises(ShortenError):
        Shortener("v.gd", get=FakeGet(status, body)).shorten(LONG)


def test_shorten_failure_is_not_cached():
    get = FakeGet(406, "Error: rate limit exceeded")
    s = Shortener("v.gd", get=get)
    for _ in range(2):
        with pytest.raises(ShortenError):
            s.shorten(LONG)
    assert len(get.calls) == 2


def test_network_error_becomes_shorten_error():
    def boom(url, headers, timeout):
        raise OSError("no route")
    with pytest.raises(ShortenError):
        Shortener("v.gd", get=boom).shorten(LONG)


@pytest.mark.parametrize("service,api,short", [
    ("v.gd", "https://v.gd/create.php?format=simple&url=", "https://v.gd/R709K6"),
    ("is.gd", "https://is.gd/create.php?format=simple&url=", "https://is.gd/R709K6"),
    ("tinyurl.com", "https://tinyurl.com/api-create.php?url=", "https://tinyurl.com/2x5ab8kq"),
    ("da.gd", "https://da.gd/s?url=", "https://da.gd/aBc-9"),
])
def test_each_service_request_and_reply(service, api, short):
    get = FakeGet(body=short + "\n")
    assert Shortener(service, get=get).shorten(LONG) == short
    assert get.calls[0][0].startswith(api)
    assert parse_qs(urlsplit(get.calls[0][0]).query)["url"] == [LONG]


def test_a_link_from_another_host_is_rejected():
    with pytest.raises(ShortenError):
        Shortener("is.gd", get=FakeGet(body="https://v.gd/R709K6")).shorten(LONG)


def test_service_names_and_none():
    assert set(SERVICES) == {"v.gd", "is.gd", "tinyurl.com", "da.gd"}
    assert make_shortener("none") is None and make_shortener("") is None
    assert make_shortener("is.gd").service == "is.gd"
    with pytest.raises(ValueError):
        Shortener("bit.ly")
    with pytest.raises(ValueError):
        make_shortener("bit.ly")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if "ok" in self.path:
            code, body = 200, b"https://v.gd/R709K6\n"
        elif "big" in self.path:
            code, body = 200, b"x" * 10_000
        else:
            code, body = 406, b"Error: rate limit"
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def test_default_get_reads_text_and_error_status(server):
    assert _default_get(server + "/ok", {}, 5) == (200, "https://v.gd/R709K6")
    assert _default_get(server + "/err", {}, 5) == (406, "Error: rate limit")


def test_default_get_rejects_oversize_body(server):
    with pytest.raises(ShortenError):
        _default_get(server + "/big", {}, 5)


# ------------------------------------------------------------------ !path --

class FakeShortener:
    def __init__(self, short="https://v.gd/AbC123", fail=False):
        self.short, self.fail = short, fail
        self.calls = []

    def shorten(self, url):
        self.calls.append(url)
        if self.fail:
            raise ShortenError("rate limited")
        return self.short


class LookupGet:
    """A CoreScope getter that records calls: !path must not make any."""
    def __init__(self):
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append(url)
        return 200, {"resolved": {}}


def ctx(**kw):
    lookups = LookupGet()
    base = {"store": Store(":memory:"), "corescope": CoreScope("https://cs.example/", get=lookups),
            "shortener": FakeShortener(), "packet_hash": PKT, "sender": "bob", "hops_n": 2,
            "hops": ["90e2", "fe27"], "hash_size": 2, "self_pos": (32.8, -97.2),
            "units": "imperial", "extra": []}
    base.update(kw)
    return base, lookups


def test_path_shows_hashes_and_a_short_corescope_link():
    c, lookups = ctx()
    assert fn_path(c, None) == "@[bob] [2h] 90e2,fe27 https://v.gd/AbC123"
    assert c["shortener"].calls == [LONG]
    assert not lookups.calls                                   # no hop-name lookup any more


def test_path_uses_the_long_link_when_shortening_fails_and_it_fits():
    c, _ = ctx(shortener=FakeShortener(fail=True))
    assert fn_path(c, None) == "@[bob] [2h] 90e2,fe27 " + LONG


def test_path_drops_the_link_when_shortening_fails_and_it_does_not_fit():
    hops = ["%04x" % (0x1000 + i) for i in range(16)]
    c, _ = ctx(shortener=FakeShortener(fail=True), hops=hops, hops_n=16)
    out = fn_path(c, None)
    assert "http" not in out and out.startswith("@[bob] [16h] 1000,1001,")
    assert len(out.encode()) <= 160


def test_path_without_packet_hash_or_corescope_is_hashes_only():
    for kw in ({"packet_hash": None}, {"corescope": None}):
        c, _ = ctx(**kw)
        assert fn_path(c, None) == "@[bob] [2h] 90e2,fe27"
        assert not c["shortener"].calls


def test_long_chain_keeps_both_ends_and_the_link_within_one_frame():
    hops = ["%04x" % (0xa000 + i) for i in range(40)]
    c, _ = ctx(hops=hops, hops_n=40, sender="Roger \U0001f5a5️ - cmch.cc")
    out = fn_path(c, None)
    assert len(out.encode()) <= 160
    assert out.endswith(" https://v.gd/AbC123")
    assert out.startswith("@[Roger \U0001f5a5️ - cmch.cc] [40h] a000,")
    assert ",a027 https://v.gd/" in out and "…" in out


def test_link_carries_uppercase_packet_hash_and_lowercase_hops():
    c, _ = ctx(hops=["90E2", "FE27"])
    fn_path(c, None)
    assert c["shortener"].calls == [LONG]


def test_direct_and_unmatched_replies_are_unchanged():
    c, _ = ctx(hops_n=0, hops=[], snr=5.5)
    assert fn_path(c, None) == "@[bob] heard you direct, no relays (snr 5.5)"
    c, _ = ctx(hops=[])
    assert fn_path(c, None) == "@[bob] [2h] hop hashes unavailable for that copy"
    assert not c["shortener"].calls


def test_no_shortener_selected_uses_the_long_link_when_it_fits(caplog):
    c, _ = ctx(shortener=None)
    assert fn_path(c, None) == "@[bob] [2h] 90e2,fe27 " + LONG
    assert "failed" not in caplog.text
