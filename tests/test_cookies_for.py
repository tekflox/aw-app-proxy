"""``GET /cookies-for`` — the one route that returns cookie values.

Two halves, tested separately because they fail differently: the matcher
(pure, used only when the browser is down) and the route's source
selection (live browser preferred, persisted store as the fallback).

Run: python -m pytest tests/test_cookies_for.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from proxy_app import cdp, cookie_match, routes  # noqa: E402
from proxy_app.cookie_store import CookieStore  # noqa: E402
from proxy_app.crypto import encrypt  # noqa: E402

from tests.test_cookie_store import FakeCtx  # noqa: E402


# --------------------------------------------------------------------------
# matcher
# --------------------------------------------------------------------------

@pytest.mark.parametrize("cookie_domain,host,expected", [
    (".google.com", "google.com", True),        # dot means "and subdomains",
    (".google.com", "mail.google.com", True),   # not "subdomains only"
    (".google.com", "notgoogle.com", False),    # suffix, not substring
    ("google.com", "mail.google.com", False),   # host-only cookie
    ("google.com", "google.com", True),
    ("google.com", "GOOGLE.COM", True),         # host comparison is case-insensitive
    ("", "google.com", False),
])
def test_domain_matches(cookie_domain, host, expected):
    assert cookie_match.domain_matches(cookie_domain, host) is expected


@pytest.mark.parametrize("cookie_path,request_path,expected", [
    ("/", "/anything", True),
    ("/app", "/app", True),
    ("/app", "/app/x", True),
    ("/app", "/applesauce", False),   # the naive startswith bug
    ("/app/", "/app/x", True),
    ("/app/x", "/app", False),
])
def test_path_matches(cookie_path, request_path, expected):
    assert cookie_match.path_matches(cookie_path, request_path) is expected


def test_secure_cookie_never_leaves_over_plain_http():
    cookie = {"domain": "example.com", "path": "/", "secure": True}
    assert cookie_match.matches(cookie, "https://example.com/x") is True
    assert cookie_match.matches(cookie, "http://example.com/x") is False


def test_non_http_scheme_matches_nothing():
    cookie = {"domain": "example.com", "path": "/", "secure": False}
    assert cookie_match.matches(cookie, "file:///etc/passwd") is False


# --------------------------------------------------------------------------
# route
# --------------------------------------------------------------------------

@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(cdp, "cdp_ws_url", lambda _url: None)

    def _boom(*_a, **_kw):
        raise AssertionError("opened a CDP socket while the browser was offline")

    monkeypatch.setattr(cdp, "open_ws", _boom)


@pytest.fixture
def seeded():
    ctx = FakeCtx()
    store = CookieStore(ctx)
    store.ensure_table()
    for name, domain, value in [("SID", ".google.com", "g-secret"),
                                ("other", ".example.com", "e-secret")]:
        store.upsert({"name": name, "value_enc": encrypt(ctx, value),
                      "domain": domain, "path": "/", "secure": False,
                      "http_only": True, "same_site": "Lax", "expires": None})
    return ctx, store


def test_falls_back_to_store_and_filters_by_url(offline, seeded):
    ctx, store = seeded
    client = TestClient(routes.build_app(ctx, store))

    body = client.get("/cookies-for", params={"url": "https://mail.google.com/"}).json()

    assert body["source"] == "store"
    assert body["browser_reachable"] is False
    assert [c["name"] for c in body["cookies"]] == ["SID"]
    assert body["cookies"][0]["value"] == "g-secret"


def test_store_fallback_does_not_leak_other_origins(offline, seeded):
    ctx, store = seeded
    client = TestClient(routes.build_app(ctx, store))

    body = client.get("/cookies-for", params={"url": "https://evil.test/"}).json()

    assert body["cookies"] == []
    assert body["count"] == 0


def test_live_browser_wins_over_the_store(monkeypatch, seeded):
    ctx, store = seeded
    monkeypatch.setattr(cdp, "cdp_ws_url", lambda _url: "ws://browser/devtools/page/1")
    monkeypatch.setattr(cdp, "open_ws", lambda _url: _FakeSock())
    seen = {}

    def _fake_send(_sock, _id, method, params):
        seen["method"], seen["params"] = method, params
        return {"result": {"cookies": [{"name": "live", "value": "v",
                                        "domain": ".google.com", "path": "/"}]}}

    monkeypatch.setattr(cdp, "send_recv", _fake_send)
    client = TestClient(routes.build_app(ctx, store))

    body = client.get("/cookies-for", params={"url": "https://mail.google.com/"}).json()

    # Chrome does its own matching — we hand it the URL rather than filtering.
    assert seen["method"] == "Network.getCookies"
    assert seen["params"] == {"urls": ["https://mail.google.com/"]}
    assert body["source"] == "browser"
    assert [c["name"] for c in body["cookies"]] == ["live"]


@pytest.mark.parametrize("url", ["not-a-url", "file:///etc/passwd", "ftp://x/y"])
def test_rejects_non_http_urls(offline, seeded, url):
    ctx, store = seeded
    client = TestClient(routes.build_app(ctx, store))
    assert client.get("/cookies-for", params={"url": url}).status_code == 400


class _FakeSock:
    def close(self):
        pass
