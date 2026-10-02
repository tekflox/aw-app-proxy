"""Persisted-cookie table — one row per browser cookie marked to survive a
proxy restart. Ported from the monolith's ``PersistedCookie`` SQLModel
(``src/api/db_models.py``) + ``src/api/routes/proxy_cookies.py`` onto the
decoupled-apps ``db:own-tables`` contribution point.

Table name is prefixed ``app__proxy__`` per ADR Decision 8 (enforced by
``src.apps.db_tables._validate``). Two access paths share this module:

* :class:`CookieStore` used from ``routes.py`` (in-process, has ``ctx`` —
  goes through ``ctx.db.execute`` so writes are attributed/journaled).
* the standalone ``proxy_server.py`` service subprocess, which has no
  ``ctx`` (it's a separate process spawned by ``ServiceSupervisor``) and
  reads this same table directly via ``get_engine()`` — see
  :func:`read_persisted_values_direct`.
"""
from __future__ import annotations

import time

TABLE = "app__proxy__persisted_cookies"

COLUMNS_SQL = (
    "name TEXT NOT NULL, "
    "value_enc TEXT NOT NULL, "
    "domain TEXT NOT NULL DEFAULT '', "
    "path TEXT NOT NULL DEFAULT '/', "
    "secure INTEGER DEFAULT 0, "
    "http_only INTEGER DEFAULT 0, "
    "same_site TEXT DEFAULT 'Lax', "
    "expires DOUBLE PRECISION, "
    "updated_at DOUBLE PRECISION, "
    "PRIMARY KEY (name, domain, path)"
)


class CookieStore:
    """In-process access via ``ctx.db`` (routes.py side)."""

    def __init__(self, ctx) -> None:
        self.ctx = ctx

    def ensure_table(self) -> None:
        self.ctx.db.create(TABLE, COLUMNS_SQL)

    def list_names(self) -> list[str]:
        rows = self.ctx.db.execute(TABLE, "SELECT DISTINCT name FROM {table} ORDER BY name")
        return [r[0] for r in rows]

    def persisted_keys(self) -> set[tuple[str, str]]:
        """(name, domain) pairs already persisted — used by routes.py's
        ``/cookie-keys`` to flag live-browser cookies that still need a
        push, now that identity is no longer name-only."""
        rows = self.ctx.db.execute(TABLE, "SELECT DISTINCT name, domain FROM {table}")
        return {(r[0], r[1]) for r in rows}

    def upsert(self, cookie: dict) -> None:
        self.ctx.db.execute(
            TABLE,
            "INSERT INTO {table} "
            "(name, value_enc, domain, path, secure, http_only, same_site, expires, updated_at) "
            "VALUES (:name, :value_enc, :domain, :path, :secure, :http_only, :same_site, :expires, :updated_at) "
            "ON CONFLICT (name, domain, path) DO UPDATE SET "
            "value_enc=EXCLUDED.value_enc, "
            "secure=EXCLUDED.secure, http_only=EXCLUDED.http_only, same_site=EXCLUDED.same_site, "
            "expires=EXCLUDED.expires, updated_at=EXCLUDED.updated_at",
            {
                "name": cookie["name"],
                "value_enc": cookie["value_enc"],
                "domain": cookie.get("domain", ""),
                "path": cookie.get("path", "/"),
                "secure": int(bool(cookie.get("secure"))),
                "http_only": int(bool(cookie.get("http_only"))),
                "same_site": cookie.get("same_site") or "Lax",
                "expires": cookie.get("expires"),
                "updated_at": time.time(),
            },
        )

    def delete(self, name: str, domain: str | None = None, path: str | None = None) -> bool:
        """Name-only deletes every row with that name (preserves the UI's
        "Forget" semantic and API back-compat); passing ``domain`` (and
        optionally ``path``) narrows the delete to that one cookie."""
        where = "name = :name"
        params: dict = {"name": name}
        if domain is not None:
            where += " AND domain = :domain"
            params["domain"] = domain
        if path is not None:
            where += " AND path = :path"
            params["path"] = path
        rows = self.ctx.db.execute(TABLE, f"SELECT name FROM {{table}} WHERE {where}", params)
        if not rows:
            return False
        self.ctx.db.execute(TABLE, f"DELETE FROM {{table}} WHERE {where}", params)
        return True

    def delete_all(self) -> int:
        rows = self.ctx.db.execute(TABLE, "SELECT name FROM {table}")
        if not rows:
            return 0
        self.ctx.db.execute(TABLE, "DELETE FROM {table}")
        return len(rows)

    def all_rows(self) -> list[dict]:
        rows = self.ctx.db.execute(
            TABLE,
            "SELECT name, value_enc, domain, path, secure, http_only, same_site, expires "
            "FROM {table} ORDER BY name",
        )
        cols = ("name", "value_enc", "domain", "path", "secure", "http_only", "same_site", "expires")
        return [dict(zip(cols, r)) for r in rows]


def read_persisted_values_direct() -> list[dict]:
    """Standalone (no-``ctx``) read used by ``proxy_server.py``.

    Talks straight to Postgres via the same engine/schema the in-process
    facade uses (``src.api.db``) — legitimate because both run inside the
    same workspace process tree with the same ``AW_WORKSPACE_SCHEMA``; this
    only bypasses the ``ctx.db`` facade's journaling, not the isolation
    (still fully schema-scoped, still only ever touches this one
    ``app__proxy__``-prefixed table).
    """
    from sqlalchemy import text

    from src.api.db import get_engine, get_workspace_schema

    schema = get_workspace_schema()
    qualified = f'"{schema}"."{TABLE}"'
    with get_engine().begin() as conn:
        try:
            result = conn.execute(text(
                f"SELECT name, value_enc, domain, path, secure, http_only, same_site, expires "
                f"FROM {qualified}"
            ))
        except Exception:
            return []  # table not created yet (app never activated)
        cols = ("name", "value_enc", "domain", "path", "secure", "http_only", "same_site", "expires")
        return [dict(zip(cols, r)) for r in result.fetchall()]


def upsert_direct(cookie: dict) -> None:
    """Standalone (no-``ctx``) upsert used by ``proxy_server.py`` to persist
    extension-synced cookies — mirrors :meth:`CookieStore.upsert` exactly,
    same table/schema, direct engine access for the same reason as
    :func:`read_persisted_values_direct`."""
    from sqlalchemy import text

    from src.api.db import get_engine, get_workspace_schema

    schema = get_workspace_schema()
    qualified = f'"{schema}"."{TABLE}"'
    with get_engine().begin() as conn:
        conn.execute(text(
            f"INSERT INTO {qualified} "
            "(name, value_enc, domain, path, secure, http_only, same_site, expires, updated_at) "
            "VALUES (:name, :value_enc, :domain, :path, :secure, :http_only, :same_site, :expires, :updated_at) "
            "ON CONFLICT (name, domain, path) DO UPDATE SET "
            "value_enc=EXCLUDED.value_enc, "
            "secure=EXCLUDED.secure, http_only=EXCLUDED.http_only, same_site=EXCLUDED.same_site, "
            "expires=EXCLUDED.expires, updated_at=EXCLUDED.updated_at"
        ), {
            "name": cookie["name"],
            "value_enc": cookie["value_enc"],
            "domain": cookie.get("domain", ""),
            "path": cookie.get("path", "/"),
            "secure": int(bool(cookie.get("secure"))),
            "http_only": int(bool(cookie.get("http_only"))),
            "same_site": cookie.get("same_site") or "Lax",
            "expires": cookie.get("expires"),
            "updated_at": time.time(),
        })
