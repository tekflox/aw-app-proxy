"""Unit tests for signature_store.py's ctx-based (in-process) path, same
FakeCtx/FakeDb pattern as ``test_cookie_store.py``, plus route-level tests
for the ``/sync-cookies`` integration (optional signature, never-synthesize,
never-block-cookies).

Run: .venv/aw/bin/python -m pytest tests/test_signature_store.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from proxy_app import cdp, routes  # noqa: E402
from proxy_app.cookie_store import CookieStore  # noqa: E402
from proxy_app.crypto import decrypt  # noqa: E402
from proxy_app.signature_store import CANONICAL_FIELDS, SignatureStore, normalize  # noqa: E402

from tests.test_cookie_store import FakeCtx  # noqa: E402


@pytest.fixture
def ctx():
    return FakeCtx()


@pytest.fixture
def store(ctx):
    s = SignatureStore(ctx)
    s.ensure_table()
    return s


# ── normalize() — the null guarantee ────────────────────────────────────────

def test_normalize_fills_every_canonical_field():
    result = normalize({"user_agent": "UA/1"})
    assert set(result) == set(CANONICAL_FIELDS)
    assert result["user_agent"] == "UA/1"
    for field in CANONICAL_FIELDS:
        if field != "user_agent":
            assert result[field] is None


def test_normalize_never_synthesizes_from_an_ios_shaped_payload():
    """iOS Safari has no navigator.userAgentData and the extension sends
    screen: null for it (popup-sheet size can't be trusted as the device
    screen) — both must come back null, not a fallback/guessed value."""
    ios_payload = {
        "source_id": "abc-123",
        "variant": "ios",
        "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)",
        "platform": "iPhone",
        "languages": ["en-US"],
        "timezone": "Europe/Lisbon",
        "screen": None,
        "hardware_concurrency": 6,
        "device_memory": None,
        # no ua_ch key at all — Safari has no userAgentData
    }
    result = normalize(ios_payload)
    assert result["screen"] is None
    assert result["device_memory"] is None
    assert result["ua_ch"] is None
    assert result["user_agent"] == ios_payload["user_agent"]


def test_normalize_ignores_extra_keys_like_source_id_and_variant():
    result = normalize({"source_id": "x", "variant": "chrome", "user_agent": "UA"})
    assert "source_id" not in result
    assert "variant" not in result


# ── SignatureStore — latest-wins grain ──────────────────────────────────────

def test_upsert_and_all_rows(store):
    store.upsert("src-1", "chrome", "enc1")
    rows = store.all_rows()
    assert len(rows) == 1
    assert rows[0]["source_id"] == "src-1"
    assert rows[0]["variant"] == "chrome"
    assert rows[0]["signature_enc"] == "enc1"


def test_two_source_ids_produce_two_rows(store):
    store.upsert("src-1", "chrome", "enc1")
    store.upsert("src-2", "ios", "enc2")
    rows = {r["source_id"]: r for r in store.all_rows()}
    assert set(rows) == {"src-1", "src-2"}


def test_same_source_id_is_latest_wins_not_a_second_row(store):
    store.upsert("src-1", "chrome", "enc-old")
    store.upsert("src-1", "chrome", "enc-new")
    rows = store.all_rows()
    assert len(rows) == 1
    assert rows[0]["signature_enc"] == "enc-new"


def test_captured_at_is_preserved_across_a_resync(store):
    store.upsert("src-1", "chrome", "enc-old")
    first = store.all_rows()[0]
    store.upsert("src-1", "chrome", "enc-new")
    second = store.all_rows()[0]
    assert second["captured_at"] == first["captured_at"]
    assert second["updated_at"] >= first["updated_at"]


# ── /sync-cookies route integration ─────────────────────────────────────────

@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(cdp, "cdp_ws_url", lambda _url: None)

    def _boom(*_a, **_kw):
        raise AssertionError("opened a CDP socket while the browser was offline")

    monkeypatch.setattr(cdp, "open_ws", _boom)


@pytest.fixture
def client_stores(offline):
    ctx = FakeCtx()
    cookie_store = CookieStore(ctx)
    signature_store = SignatureStore(ctx)
    app = routes.build_app(ctx, cookie_store, signature_store)
    return TestClient(app), ctx, cookie_store, signature_store


def test_sync_with_signature_stores_both(client_stores):
    client, ctx, _cookies, signatures = client_stores
    resp = client.post("/sync-cookies", json={
        "cookies": [{"name": "aw_jwt", "value": "v1", "domain": "d"}],
        "signature": {
            "source_id": "src-1", "variant": "chrome",
            "user_agent": "UA/1", "platform": "Win32",
        },
    })
    assert resp.status_code == 200
    assert resp.json()["signature_stored"] is True
    rows = signatures.all_rows()
    assert len(rows) == 1
    blob = json.loads(decrypt(ctx, rows[0]["signature_enc"]))
    assert blob["user_agent"] == "UA/1"
    assert blob["screen"] is None  # never synthesized


def test_sync_without_signature_still_works(client_stores):
    client, _ctx, _cookies, signatures = client_stores
    resp = client.post("/sync-cookies", json={
        "cookies": [{"name": "aw_jwt", "value": "v1", "domain": "d"}],
    })
    assert resp.status_code == 200
    assert resp.json()["persisted"] == 1
    assert resp.json()["signature_stored"] is False
    assert signatures.all_rows() == []


def test_cookie_sync_survives_a_broken_signature_store(client_stores, monkeypatch):
    """PO criterion 4: a failure capturing/storing the signature must never
    block or fail the cookie sync — the existing path cannot regress for a
    new, secondary feature."""
    client, _ctx, cookies, signatures = client_stores
    monkeypatch.setattr(
        signatures, "upsert",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    resp = client.post("/sync-cookies", json={
        "cookies": [{"name": "aw_jwt", "value": "v1", "domain": "d"}],
        "signature": {"source_id": "src-1", "variant": "chrome", "user_agent": "UA/1"},
    })

    assert resp.status_code == 200
    assert resp.json()["persisted"] == 1
    assert resp.json()["signature_stored"] is False
    assert cookies.list_names() == ["aw_jwt"]


def test_signature_missing_source_id_or_variant_is_not_stored(client_stores):
    """A signature payload with no identity (source_id/variant) has nothing
    to key a row on — skip storing it rather than guessing an identity."""
    client, _ctx, _cookies, signatures = client_stores
    resp = client.post("/sync-cookies", json={
        "cookies": [{"name": "aw_jwt", "value": "v1", "domain": "d"}],
        "signature": {"user_agent": "UA/1"},
    })
    assert resp.status_code == 200
    assert resp.json()["signature_stored"] is False
    assert signatures.all_rows() == []


def test_browser_signatures_endpoint_decrypts(client_stores):
    client, _ctx, _cookies, _signatures = client_stores
    client.post("/sync-cookies", json={
        "cookies": [{"name": "aw_jwt", "value": "v1", "domain": "d"}],
        "signature": {"source_id": "src-1", "variant": "chrome", "user_agent": "UA/1"},
    })
    resp = client.get("/browser-signatures")
    assert resp.status_code == 200
    sigs = resp.json()["signatures"]
    assert len(sigs) == 1
    assert sigs[0]["source_id"] == "src-1"
    assert sigs[0]["signature"]["user_agent"] == "UA/1"
