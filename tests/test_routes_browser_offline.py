"""The proxy must be fully usable with aw-app-browser stopped.

aw-app-browser is a separate Tier-2 app with its own lifecycle, and it is off
more often than on (``auto_start: false``). ``cookie_store`` — not the live
Chromium session — is the source of truth, so both write paths have to succeed
against an unreachable CDP and simply report ``browser_reachable: false``.

Run: .venv/aw/bin/python -m pytest tests/test_routes_browser_offline.py
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
from proxy_app.crypto import decrypt  # noqa: E402

from tests.test_cookie_store import FakeCtx  # noqa: E402


@pytest.fixture
def offline(monkeypatch):
    """No browser anywhere: every CDP reachability probe comes back empty."""
    monkeypatch.setattr(cdp, "cdp_ws_url", lambda _url: None)

    def _boom(*_a, **_kw):
        raise AssertionError("opened a CDP socket while the browser was offline")

    monkeypatch.setattr(cdp, "open_ws", _boom)


@pytest.fixture
def client_store(offline):
    ctx = FakeCtx()
    store = CookieStore(ctx)
    return TestClient(routes.build_app(ctx, store)), ctx, store


def test_sync_persists_every_cookie_with_browser_offline(client_store):
    client, ctx, store = client_store
    resp = client.post("/sync-cookies", json={"cookies": [
        {"name": "aw_jwt", "value": "v1", "domain": ".google.com"},
        {"name": "SID", "value": "v2", "domain": "accounts.google.com"},
    ]})

    assert resp.status_code == 200
    assert resp.json() == {"persisted": 2, "injected": 0, "failed": 0,
                           "browser_reachable": False}
    rows = {r["name"]: r for r in store.all_rows()}
    assert set(rows) == {"aw_jwt", "SID"}
    assert decrypt(ctx, rows["aw_jwt"]["value_enc"]) == "v1"


def test_clear_all_purges_the_store_with_browser_offline(client_store):
    """Regression: clear used to 502 when the browser was down AND leave the
    store intact when it wasn't — so the reconnect loop re-injected whatever
    had just been 'cleared'."""
    client, _ctx, store = client_store
    client.post("/sync-cookies", json={"cookies": [
        {"name": "aw_jwt", "value": "v1", "domain": ".google.com"},
    ]})

    resp = client.post("/clear-cookies", json={"all": True})

    assert resp.status_code == 200
    assert resp.json()["purged"] == 1
    assert resp.json()["browser_reachable"] is False
    assert store.all_rows() == []


def test_clear_named_purges_only_those_names(client_store):
    client, _ctx, store = client_store
    client.post("/sync-cookies", json={"cookies": [
        {"name": "aw_jwt", "value": "v1", "domain": ".google.com"},
        {"name": "SID", "value": "v2", "domain": "accounts.google.com"},
    ]})

    resp = client.post("/clear-cookies", json={"cookies": [{"name": "SID"}]})

    assert resp.status_code == 200
    assert resp.json()["purged"] == 1
    assert store.list_names() == ["aw_jwt"]


def test_restore_is_a_noop_when_browser_offline(client_store):
    """The reconnect loop must not blow up on an unreachable browser — it just
    reports nothing done and waits for the next poll."""
    _client, ctx, store = client_store
    store.upsert({"name": "aw_jwt", "value_enc": "enc", "domain": ".google.com"})
    assert routes.restore_persisted_cookies(ctx, store) == (0, 0)
