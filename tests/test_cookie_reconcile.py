"""Reconcile-pass failure taxonomy — routes.py::restore_persisted_cookies.

Three outcomes used to be lumped into one "failed" counter: a cookie whose
ciphertext predates a Fernet key rotation (permanently undecryptable), a
cookie Chromium structurally rejects (e.g. SameSite=None without Secure),
and a plain transient CDP/websocket hiccup. This exercises the split:
dead rows are quarantined immediately (visible forever, never deleted); a
rejection only quarantines once it proves itself non-transient (3
consecutive rejects, primary-target-only bookkeeping); a transport hiccup
never writes anything to the DB.

Run: .venv/aw/bin/python -m pytest tests/test_cookie_reconcile.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from proxy_app import cdp, routes  # noqa: E402
from proxy_app.cookie_store import CookieStore  # noqa: E402
from proxy_app.crypto import encrypt  # noqa: E402

from tests.test_cookie_store import FakeCtx  # noqa: E402


class _FakeSock:
    def close(self):
        pass


@pytest.fixture
def ctx():
    c = FakeCtx()
    # A single target makes it trivially the primary one, so every pass in
    # these tests writes bookkeeping — the primary-only gate itself is
    # covered by test_plugin.py's multi-target test.
    c.config = {"browser_cdp_list_urls": ["http://fake-target/json/list"]}
    return c


@pytest.fixture
def store(ctx):
    s = CookieStore(ctx)
    s.ensure_table()
    return s


@pytest.fixture(autouse=True)
def _cdp_reachable(monkeypatch):
    monkeypatch.setattr(cdp, "cdp_ws_url", lambda _url: "ws://fake")
    monkeypatch.setattr(cdp, "open_ws", lambda _url: _FakeSock())


def test_decrypt_dead_row_quarantined_once_then_excluded_from_next_pass(monkeypatch, ctx, store):
    encrypt(ctx, "warm-up")  # establish the key before persisting ciphertext it can't decrypt
    store.upsert({"name": "myacinfo", "value_enc": "garbage-not-fernet", "domain": ".apple.com"})
    monkeypatch.setattr(cdp, "send_recv", lambda *a, **kw: {"result": {"success": True}})

    result = routes.restore_persisted_cookies(ctx, store)
    assert result.decrypt_dead == 1
    assert result.quarantined == [
        {"name": "myacinfo", "domain": ".apple.com",
         "reason": "decrypt: key mismatch — row predates key rotation"}]

    row = store.all_rows()[0]
    assert row["quarantined_at"] is not None
    assert row["quarantine_reason"] == "decrypt: key mismatch — row predates key rotation"

    # Second pass: now excluded from the active set — not re-attempted,
    # not re-counted, not re-quarantined.
    result2 = routes.restore_persisted_cookies(ctx, store)
    assert result2.decrypt_dead == 0
    assert result2.quarantined == []


def test_rejected_cookie_quarantined_only_after_three_consecutive_passes(monkeypatch, ctx, store):
    store.upsert({"name": "tk_lr", "value_enc": encrypt(ctx, "v"), "domain": ".tekflox.com",
                  "same_site": "None", "secure": False})
    monkeypatch.setattr(cdp, "send_recv", lambda *a, **kw: {"result": {"success": False}})

    r1 = routes.restore_persisted_cookies(ctx, store)
    assert r1.rejected == 1 and r1.quarantined == []
    assert store.all_rows()[0]["fail_streak"] == 1
    assert store.all_rows()[0]["quarantined_at"] is None

    r2 = routes.restore_persisted_cookies(ctx, store)
    assert r2.quarantined == []
    assert store.all_rows()[0]["fail_streak"] == 2

    r3 = routes.restore_persisted_cookies(ctx, store)
    assert len(r3.quarantined) == 1
    assert "SameSite=None requires Secure" in r3.quarantined[0]["reason"]
    row = store.all_rows()[0]
    assert row["fail_streak"] == 3
    assert row["quarantined_at"] is not None

    # Quarantined now -> excluded from the next pass entirely.
    r4 = routes.restore_persisted_cookies(ctx, store)
    assert r4.rejected == 0


def test_transport_failure_never_increments_streak_or_quarantines(monkeypatch, ctx, store):
    store.upsert({"name": "aw_jwt", "value_enc": encrypt(ctx, "v"), "domain": ".example.com"})
    monkeypatch.setattr(cdp, "send_recv", lambda *a, **kw: None)

    for _ in range(5):
        result = routes.restore_persisted_cookies(ctx, store)
        assert result.transport_failed == 1
        assert result.rejected == 0
        assert result.quarantined == []

    row = store.all_rows()[0]
    assert row["fail_streak"] == 0
    assert row["quarantined_at"] is None


def test_expired_row_hard_deleted_during_reconcile(monkeypatch, ctx, store):
    store.upsert({"name": "_fbp", "value_enc": encrypt(ctx, "v"), "domain": ".app.aw.tekflox.com",
                  "expires": time.time() - 90000})  # past expiry + the 86400s grace
    store.upsert({"name": "session", "value_enc": encrypt(ctx, "v"), "domain": ".example.com"})
    monkeypatch.setattr(cdp, "send_recv", lambda *a, **kw: {"result": {"success": True}})

    routes.restore_persisted_cookies(ctx, store)

    assert {r["name"] for r in store.all_rows()} == {"session"}


def test_session_cookie_with_no_expiry_is_never_deleted(monkeypatch, ctx, store):
    store.upsert({"name": "session", "value_enc": encrypt(ctx, "v"), "domain": ".example.com"})
    monkeypatch.setattr(cdp, "send_recv", lambda *a, **kw: {"result": {"success": True}})

    routes.restore_persisted_cookies(ctx, store)

    assert {r["name"] for r in store.all_rows()} == {"session"}
