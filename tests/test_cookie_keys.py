"""GET /cookie-keys must list already-persisted cookies regardless of
whether the live browser (CDP) is reachable — the Postgres-backed
CookieStore is the source of truth, the live browser is only ever an
additive, best-effort extra. Regression coverage for the bug where this
endpoint checked CDP reachability BEFORE ever consulting the store, so a
stopped browser made the Settings > Proxy Cookies panel show an empty list
even though persisted cookies existed.

Run: .venv/aw/bin/python -m pytest tests/test_cookie_keys.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from proxy_app import cdp, routes  # noqa: E402
from proxy_app.cookie_store import CookieStore  # noqa: E402

from tests.test_cookie_store import FakeCtx  # noqa: E402


class _FakeSock:
    def close(self):
        pass


@pytest.fixture
def client_store():
    ctx = FakeCtx()
    store = CookieStore(ctx)
    store.ensure_table()
    return TestClient(routes.build_app(ctx, store)), store


def test_cdp_reachable_merges_persisted_and_live_cookies(monkeypatch, client_store):
    """(a) Existing behavior preserved: a live cookie already persisted is
    flagged persisted=true, one not yet persisted comes back persisted=false,
    no error note."""
    client, store = client_store
    store.upsert({"name": "aw_jwt", "value_enc": "enc", "domain": ".example.com",
                  "path": "/", "secure": False, "http_only": False,
                  "same_site": "Lax", "expires": None})

    monkeypatch.setattr(cdp, "cdp_ws_url", lambda _url: "ws://up")
    monkeypatch.setattr(cdp, "open_ws", lambda _url: _FakeSock())
    monkeypatch.setattr(cdp, "send_recv", lambda *a, **kw: {"result": {"cookies": [
        {"name": "aw_jwt", "domain": ".example.com"},
        {"name": "SID", "domain": "accounts.example.com"},
    ]}})

    resp = client.get("/cookie-keys")

    assert resp.status_code == 200
    body = resp.json()
    assert "error" not in body
    keys = {(k["name"], k["domain"]): k["persisted"] for k in body["keys"]}
    assert keys == {
        ("aw_jwt", ".example.com"): True,
        ("SID", "accounts.example.com"): False,
    }


def test_cdp_unreachable_returns_persisted_cookies_not_empty(monkeypatch, client_store):
    """(b) Browser down, cookies already persisted: must return them, not an
    empty list, with a non-blocking error note."""
    client, store = client_store
    store.upsert({"name": "aw_jwt", "value_enc": "enc", "domain": ".example.com",
                  "path": "/", "secure": False, "http_only": False,
                  "same_site": "Lax", "expires": None})

    monkeypatch.setattr(cdp, "cdp_ws_url", lambda _url: None)

    def _boom(*_a, **_kw):
        raise AssertionError("opened a CDP socket while the browser was offline")

    monkeypatch.setattr(cdp, "open_ws", _boom)

    resp = client.get("/cookie-keys")

    assert resp.status_code == 200
    body = resp.json()
    assert body["keys"] == [{"name": "aw_jwt", "domain": ".example.com", "persisted": True}]
    assert body["error"]


def test_cdp_unreachable_with_nothing_persisted_is_an_empty_list_not_a_crash(monkeypatch, client_store):
    """(c) Nothing persisted, browser down: empty keys list, no crash, still
    a non-blocking error note."""
    client, _store = client_store
    monkeypatch.setattr(cdp, "cdp_ws_url", lambda _url: None)

    resp = client.get("/cookie-keys")

    assert resp.status_code == 200
    body = resp.json()
    assert body["keys"] == []
    assert body["error"]


def test_cdp_reachable_but_get_all_cookies_fails_still_returns_persisted(monkeypatch, client_store):
    """(d) CDP socket opens but Network.getAllCookies itself fails
    (send_recv returns None): persisted list is still returned, with a
    non-blocking error note rather than a blocking failure."""
    client, store = client_store
    store.upsert({"name": "aw_jwt", "value_enc": "enc", "domain": ".example.com",
                  "path": "/", "secure": False, "http_only": False,
                  "same_site": "Lax", "expires": None})

    monkeypatch.setattr(cdp, "cdp_ws_url", lambda _url: "ws://up")
    monkeypatch.setattr(cdp, "open_ws", lambda _url: _FakeSock())
    monkeypatch.setattr(cdp, "send_recv", lambda *a, **kw: None)

    resp = client.get("/cookie-keys")

    assert resp.status_code == 200
    body = resp.json()
    assert body["keys"] == [{"name": "aw_jwt", "domain": ".example.com", "persisted": True}]
    assert body["error"]
