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

import json
import logging
import shlex
import sys

from . import routes as routes_mod

log = logging.getLogger("aw_apps.proxy")

SERVICE_ID = "proxy-server"

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

        subapp = routes_mod.build_app(ctx)
        ctx.routes.register(subapp)

        port = int(ctx.config.get("proxy_port") or 9124)
        allowed_networks = resolve_allowed_networks(ctx.config)
        start_cmd = (
            f"{sys.executable} -m proxy_app.proxy_server --port {port} "
            f"--allowed-networks {shlex.quote(json.dumps(allowed_networks))}"
        )
        ctx.services.register(SERVICE_ID, start_cmd, autostart=True)

        log.info("aw-app-proxy activated (service port=%s)", port)

    async def deactivate(self) -> None:
        log.info("aw-app-proxy deactivated")
