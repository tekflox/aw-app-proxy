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

import logging
import sys

from . import routes as routes_mod

log = logging.getLogger("aw_apps.proxy")

SERVICE_ID = "proxy-server"


class ProxyAppPlugin:
    async def activate(self, ctx) -> None:
        self.ctx = ctx

        subapp = routes_mod.build_app(ctx)
        ctx.routes.register(subapp)

        port = int(ctx.config.get("proxy_port") or 9124)
        start_cmd = f"{sys.executable} -m proxy_app.proxy_server --port {port}"
        ctx.services.register(SERVICE_ID, start_cmd, autostart=True)

        log.info("aw-app-proxy activated (service port=%s)", port)

    async def deactivate(self) -> None:
        log.info("aw-app-proxy deactivated")
