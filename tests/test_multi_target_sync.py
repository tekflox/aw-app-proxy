"""Multi-target CDP push (the Kali cookie-sync ADR): writes (sync/clear) fan
out to every configured target with per-target failure isolation; reads stay
pinned to the primary (first) target so an automation profile's cookies
never pollute the persist flow or the Settings panel.

Run: python -m pytest tests/test_multi_target_sync.py
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

PRIMARY = "http://aw-app-browser:9223/json/list"
SECONDARY = "http://aw-app-kali-linux:9223/json/list"


class _FakeSock:
    def close(self):
        pass


@pytest.fixture
def two_targets():
    ctx = FakeCtx()
    ctx.config = {"browser_cdp_list_urls": [PRIMARY, SECONDARY]}
    store = CookieStore(ctx)
    store.ensure_table()
    return ctx, store


def test_sync_cookies_injects_into_every_reachable_target(monkeypatch, two_targets):
    ctx, store = two_targets
    monkeypatch.setattr(cdp, "cdp_ws_url", lambda url: f"ws://{url}")
    monkeypatch.setattr(cdp, "open_ws", lambda _url: _FakeSock())

    seen_urls = []

    def _fake_send(_sock, _id, method, params):
        seen_urls.append(method)
        return {"result": {"success": True}}

    monkeypatch.setattr(cdp, "send_recv", _fake_send)
    client = TestClient(routes.build_app(ctx, store))

    resp = client.post("/sync-cookies", json={
        "cookies": [{"name": "SID", "value": "v", "domain": ".example.com"}],
    })

    body = resp.json()
    assert body["browser_reachable"] is True
    assert body["injected"] == 2  # one per target
    assert body["failed"] == 0


def test_sync_cookies_one_target_down_does_not_block_the_other(monkeypatch, two_targets):
    ctx, store = two_targets
    monkeypatch.setattr(cdp, "cdp_ws_url", lambda url: ("ws://up" if url == PRIMARY else None))
    monkeypatch.setattr(cdp, "open_ws", lambda _url: _FakeSock())
    monkeypatch.setattr(cdp, "send_recv", lambda *a, **kw: {"result": {"success": True}})
    client = TestClient(routes.build_app(ctx, store))

    resp = client.post("/sync-cookies", json={
        "cookies": [{"name": "SID", "value": "v", "domain": ".example.com"}],
    })

    body = resp.json()
    assert body["browser_reachable"] is True
    assert body["injected"] == 1
    assert body["failed"] == 0


def test_sync_cookies_all_targets_down_reports_unreachable(monkeypatch, two_targets):
    ctx, store = two_targets
    monkeypatch.setattr(cdp, "cdp_ws_url", lambda _url: None)
    client = TestClient(routes.build_app(ctx, store))

    resp = client.post("/sync-cookies", json={
        "cookies": [{"name": "SID", "value": "v", "domain": ".example.com"}],
    })

    assert resp.json() == {"persisted": 1, "injected": 0, "failed": 0,
                           "browser_reachable": False, "signature_stored": False}


def test_clear_cookies_fans_out_and_tolerates_one_down_target(monkeypatch, two_targets):
    ctx, store = two_targets
    store.upsert({"name": "SID", "value_enc": "enc", "domain": ".example.com",
                  "path": "/", "secure": False, "http_only": False,
                  "same_site": "Lax", "expires": None})
    monkeypatch.setattr(cdp, "cdp_ws_url", lambda url: ("ws://up" if url == PRIMARY else None))
    monkeypatch.setattr(cdp, "open_ws", lambda _url: _FakeSock())
    monkeypatch.setattr(cdp, "send_recv", lambda *a, **kw: {"result": {}})
    client = TestClient(routes.build_app(ctx, store))

    resp = client.post("/browser-cookies/clear", json={"cookies": [{"name": "SID"}]})

    body = resp.json()
    assert body["browser_reachable"] is True
    assert body["cleared"] == 1


def test_reads_stay_pinned_to_the_primary_target_only(monkeypatch, two_targets):
    """cookie-keys must never consult the secondary (automation) target —
    only the first configured one is the source of truth for what the user
    is logged into."""
    ctx, store = two_targets
    queried = []

    def _fake_ws_url(url):
        queried.append(url)
        return f"ws://{url}"

    monkeypatch.setattr(cdp, "cdp_ws_url", _fake_ws_url)
    monkeypatch.setattr(cdp, "open_ws", lambda _url: _FakeSock())
    monkeypatch.setattr(cdp, "send_recv", lambda *a, **kw: {"result": {"cookies": []}})
    client = TestClient(routes.build_app(ctx, store))

    client.get("/cookie-keys")

    assert queried == [PRIMARY]
