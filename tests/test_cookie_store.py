"""Unit tests for cookie_store.py + crypto.py's ctx-based (in-process) path,
with ``ctx.db``/``ctx.secrets`` faked by an in-memory sqlite3 connection +
a dict — same pattern as the sibling ``tekflox/aw-app-whiteboard`` /
``tekflox/aw-app-presentations`` migrations. Doesn't exercise the standalone
``proxy_server.py`` direct-Postgres path (needs a real ``src.api.db`` engine)
— that one is covered by the repo's manual/live testing instead.

Run: .venv/aw/bin/python -m pytest tests/test_cookie_store.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from proxy_app.cookie_store import CookieStore  # noqa: E402
from proxy_app.crypto import decrypt, encrypt  # noqa: E402


class FakeDb:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)

    def create(self, name, columns_sql):
        self.conn.execute(f"CREATE TABLE IF NOT EXISTS {name} ({columns_sql})")
        self.conn.commit()
        return name

    def execute(self, name, sql, params=None):
        stmt = sql.replace("{table}", name)
        cur = self.conn.execute(stmt, params or {})
        self.conn.commit()
        if stmt.strip().lower().startswith("select"):
            return cur.fetchall()
        return cur


class FakeSecrets:
    def __init__(self):
        self._data = {}

    def read(self, key):
        return self._data.get(key)

    def write(self, key, value):
        self._data[key] = value


class FakeCtx:
    def __init__(self):
        self.app_id = "proxy"
        self.db = FakeDb()
        self.secrets = FakeSecrets()
        self.config = {}


@pytest.fixture
def ctx():
    return FakeCtx()


@pytest.fixture
def store(ctx):
    s = CookieStore(ctx)
    s.ensure_table()
    return s


def test_upsert_and_list_names(store):
    store.upsert({"name": "aw_jwt", "value_enc": "enc1", "domain": "aw.tekflox.com"})
    store.upsert({"name": "session", "value_enc": "enc2", "domain": "aw.tekflox.com"})
    assert store.list_names() == ["aw_jwt", "session"]


def test_upsert_is_idempotent_on_conflict(store):
    store.upsert({"name": "aw_jwt", "value_enc": "enc1", "domain": "d1"})
    store.upsert({"name": "aw_jwt", "value_enc": "enc2", "domain": "d2"})
    rows = store.all_rows()
    assert len(rows) == 1
    assert rows[0]["value_enc"] == "enc2"
    assert rows[0]["domain"] == "d2"


def test_delete_returns_false_when_missing(store):
    assert store.delete("nope") is False


def test_delete_removes_row(store):
    store.upsert({"name": "aw_jwt", "value_enc": "enc1"})
    assert store.delete("aw_jwt") is True
    assert store.list_names() == []


def test_encrypt_decrypt_roundtrip(ctx):
    token = encrypt(ctx, "top-secret-cookie-value")
    assert token != "top-secret-cookie-value"
    assert decrypt(ctx, token) == "top-secret-cookie-value"
    # key persisted in ctx.secrets so a second encrypt reuses the same key
    assert ctx.secrets.read("cookie_encryption_key") is not None


def test_decrypt_wrong_key_raises(ctx):
    token = encrypt(ctx, "value")
    other_ctx = FakeCtx()  # fresh ctx -> fresh (different) key on first use
    with pytest.raises(ValueError):
        decrypt(other_ctx, token)
