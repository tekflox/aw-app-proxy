"""Entrypoint referenced by aw-app.json's runtime.entrypoint
("proxy_app.plugin:ProxyAppPlugin").

Ports three previously monolith-only pieces onto the F4 ``ctx`` facades:

* ``ctx.routes`` (``routes:register``) — the cookie-persistence API
  (``src/api/routes/proxy_cookies.py``) at ``/api/apps/proxy/*``.
* ``ctx.db`` (``db:own-tables``) — persisted cookie rows, in this
  workspace's own Postgres schema instead of the monolith's shared table.
* ``ctx.services`` (``service:manage``) — the CONNECT-tunnel proxy server
  (``tools/browser/proxy.py``) as a managed subprocess instead of a
  hand-run ``./aw start proxy``.
* ``ctx.secrets`` (``secrets:own``) — the Fernet key that encrypts cookie
  values at rest in the Postgres table (``crypto.py``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import shlex
import sys

from . import cdp
from . import routes as routes_mod
from .cookie_store import CookieStore

log = logging.getLogger("aw_apps.proxy")

SERVICE_ID = "proxy-server"

# How often to poll CDP reachability while the browser might be offline.
# Deliberately self-contained (no aw-app-browser callback dependency, see
# README/2026-08-03 design note) — cheap /json/list GET, so 15s is fine.
RECONNECT_POLL_SECONDS = 15

# Mirrors aw-app.json's config_schema.allowed_networks default. AppContext.config
# is the raw persisted/passed config dict — src/apps/base.py's AppContext never
# merges manifest config_schema defaults into it — so an app installed with no
# explicit `allowed_networks` override gets an EMPTY ctx.config here, not the
# manifest's default. This constant is the real fallback in that (common) case;
# keep it in sync with aw-app.json by hand.
DEFAULT_ALLOWED_NETWORKS = ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]


def resolve_allowed_networks(config: dict) -> list:
    return config.get("allowed_networks") or DEFAULT_ALLOWED_NETWORKS


class ProxyAppPlugin:
    async def activate(self, ctx) -> None:
        self.ctx = ctx
        self.store = CookieStore(ctx)
        self.store.ensure_table()

        subapp = routes_mod.build_app(ctx, self.store)
        ctx.routes.register(subapp)

        port = int(ctx.config.get("proxy_port") or 9124)
        allowed_networks = resolve_allowed_networks(ctx.config)
        from .cdp import cdp_list_url_default
        cdp_list_url = ctx.config.get("browser_cdp_list_url") or cdp_list_url_default()
        start_cmd = (
            f"{sys.executable} -m proxy_app.proxy_server --port {port} "
            f"--allowed-networks {shlex.quote(json.dumps(allowed_networks))} "
            f"--cdp-list-url {shlex.quote(cdp_list_url)}"
        )
        ctx.services.register(SERVICE_ID, start_cmd, autostart=True)

        self._reconnect_task = asyncio.create_task(self._cookie_reconnect_loop())

        log.info("aw-app-proxy activated (service port=%s)", port)

    async def _cookie_reconnect_loop(self) -> None:
        """Periodically poll CDP reachability and, when the browser
        transitions from unreachable → reachable (e.g. aw-app-browser was
        restarted or is slow to come up), re-inject every persisted cookie
        so an offline sync_cookies call eventually reaches a live browser
        session with no cross-app callback required.

        Chosen over an aw-app-browser startup-hook calling back into this
        app: aw-app-browser is a prebuilt Tier-2 container image (own repo,
        own release cadence) — teaching it about this app's HTTP shape
        would be a new cross-app coupling for a problem this app can
        already solve entirely with the CDP helpers it already has."""
        was_reachable = False
        while True:
            try:
                await asyncio.sleep(RECONNECT_POLL_SECONDS)
                reachable = cdp.cdp_ws_url(routes_mod._cdp_list_url(self.ctx)) is not None
                if reachable and not was_reachable:
                    injected, failed = await asyncio.to_thread(
                        routes_mod.restore_persisted_cookies, self.ctx, self.store)
                    if injected or failed:
                        log.info(
                            "Cookie reconnect: browser back online, re-injected "
                            "%s persisted cookie(s) (%s failed)", injected, failed)
                was_reachable = reachable
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("Cookie reconnect loop iteration failed", exc_info=True)

    async def deactivate(self) -> None:
        task = getattr(self, "_reconnect_task", None)
        if task is not None:
            task.cancel()
        log.info("aw-app-proxy deactivated")
