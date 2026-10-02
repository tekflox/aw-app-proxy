"""Persisted-browser-signature table — one row per source browser install
(the real browser the aw-sync extension ran in, identified by a
client-generated ``source_id``), latest-wins. Sibling of ``cookie_store.py``,
same ``db:own-tables`` contribution point and ``app__proxy__`` table-name
prefix (ADR Decision 8).

Grain is deliberately per-browser, not per-cookie: a signature describes the
browser, not any one cookie, and per-cookie storage would just duplicate the
same blob across every synced cookie from one sync call.

:func:`normalize` is the structural guarantee behind "absent field persists
as null, never a synthesized default" (PO criterion 2) — it is the ONLY
place a ``signature`` dict is read, and it does a bare ``.get(field, None)``
over the frozen :data:`CANONICAL_FIELDS` list, so there is no code path left
that could apply a fallback value.

Only ``routes.py``'s FastAPI ``/sync-cookies`` handler writes this table —
unlike ``cookie_store.py`` there is no standalone ``*_direct`` twin here,
because the only other process in this app, ``proxy_server.py``, cannot
reach either extension (see the Architect design on this card): a
no-``ctx`` twin would be dead code until a real no-``ctx`` consumer exists.
"""
from __future__ import annotations

import time

TABLE = "app__proxy__browser_signatures"

COLUMNS_SQL = (
    "source_id TEXT PRIMARY KEY, "
    "variant TEXT NOT NULL, "
    "signature_enc TEXT NOT NULL, "
    "captured_at DOUBLE PRECISION, "
    "updated_at DOUBLE PRECISION"
)

# Frozen on purpose — the field set a consumer can rely on existing (as
# `None` if the browser didn't report it), never grown ad hoc from whatever
# happens to be in an incoming payload.
CANONICAL_FIELDS = (
    "user_agent",
    "platform",
    "languages",
    "timezone",
    "screen",
    "hardware_concurrency",
    "device_memory",
    "ua_ch",
)


def normalize(signature: dict) -> dict:
    """Project an incoming signature payload onto :data:`CANONICAL_FIELDS`.

    A missing key becomes an explicit ``None`` — never a default value. This
    is what keeps a dropped `getHighEntropyValues` call (Safari has none;
    Chrome's can reject) honest instead of silently fabricated."""
    return {field: signature.get(field, None) for field in CANONICAL_FIELDS}


class SignatureStore:
    """In-process access via ``ctx.db`` (routes.py side)."""

    def __init__(self, ctx) -> None:
        self.ctx = ctx

    def ensure_table(self) -> None:
        self.ctx.db.create(TABLE, COLUMNS_SQL)

    def upsert(self, source_id: str, variant: str, signature_enc: str) -> None:
        # captured_at is the FIRST time this source_id was ever seen — the
        # ON CONFLICT branch deliberately doesn't touch it, only updated_at
        # advances on a re-sync.
        now = time.time()
        self.ctx.db.execute(
            TABLE,
            "INSERT INTO {table} "
            "(source_id, variant, signature_enc, captured_at, updated_at) "
            "VALUES (:source_id, :variant, :signature_enc, :captured_at, :updated_at) "
            "ON CONFLICT (source_id) DO UPDATE SET "
            "variant=EXCLUDED.variant, signature_enc=EXCLUDED.signature_enc, "
            "updated_at=EXCLUDED.updated_at",
            {
                "source_id": source_id,
                "variant": variant,
                "signature_enc": signature_enc,
                "captured_at": now,
                "updated_at": now,
            },
        )

    def all_rows(self) -> list[dict]:
        rows = self.ctx.db.execute(
            TABLE,
            "SELECT source_id, variant, signature_enc, captured_at, updated_at "
            "FROM {table} ORDER BY source_id",
        )
        cols = ("source_id", "variant", "signature_enc", "captured_at", "updated_at")
        return [dict(zip(cols, r)) for r in rows]
