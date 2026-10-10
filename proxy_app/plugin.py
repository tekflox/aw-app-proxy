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
from .signature_store import SignatureStore

log = logging.getLogger("aw_apps.proxy")

SERVICE_ID = "proxy-server"

# How often to poll CDP reachability while the browser might be offline.
# Deliberately self-contained (no aw-app-browser callback dependency, see
# README/2026-08-03 design note) — cheap /json/list GET, so 15s is fine.
RECONNECT_POLL_SECONDS = 15

# How many consecutive ticks of transport_failed > 0 (same target) before a
# single "this has turned chronic" WARNING fires — 4 ticks * 15s ≈ 1 minute,
# long enough that a one-off websocket hiccup never trips it.
_TRANSPORT_FAILURE_CHRONIC_TICKS = 4

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
        self.signature_store = SignatureStore(ctx)
        self.signature_store.ensure_table()

        subapp = routes_mod.build_app(ctx, self.store, self.signature_store)
        ctx.routes.register(subapp)

        port = int(ctx.config.get("proxy_port") or 9124)
        allowed_networks = resolve_allowed_networks(ctx.config)
        cdp_list_urls = cdp.resolve_cdp_list_urls(ctx.config)
        start_cmd = (
            f"{sys.executable} -m proxy_app.proxy_server --port {port} "
            f"--allowed-networks {shlex.quote(json.dumps(allowed_networks))} "
            f"--cdp-list-urls {shlex.quote(json.dumps(cdp_list_urls))}"
        )
        ctx.services.register(SERVICE_ID, start_cmd, autostart=ctx.config.get("auto_start", True))

        self._reconnect_task = asyncio.create_task(self._cookie_reconnect_loop())

        log.info("aw-app-proxy activated (service port=%s)", port)

    async def _cookie_reconnect_loop(self) -> None:
        """Periodically re-inject every persisted cookie into the live
        browser, on every poll tick while CDP is reachable — not only on an
        unreachable → reachable transition. A browser that stays
        continuously reachable can still have individual cookies cleared,
        evicted or expired in-place (by an in-page action, Chrome's own
        eviction, or another CDP client); an edge-only check never observes
        that drift, so it never gets repaired until some unrelated
        down→up transition happens to occur — which may be never.
        ``Network.setCookie`` is idempotent, so reapplying already-correct
        cookies on every tick is harmless.

        Chosen over an aw-app-browser startup-hook calling back into this
        app: aw-app-browser is a prebuilt Tier-2 container image (own repo,
        own release cadence) — teaching it about this app's HTTP shape
        would be a new cross-app coupling for a problem this app can
        already solve entirely with the CDP helpers it already has.

        Iterates every configured target independently: one target being
        down (e.g. Kali not installed, or its container stopped) must not
        skip reconciling the others.

        Logging is split three ways so a steady state (everything already
        reconciled, same counts tick after tick) goes quiet instead of
        repeating an identical INFO line forever:

        * A newly-quarantined cookie (dead key, or a rejection that proved
          itself non-transient) is always loud — one aggregated WARNING per
          tick, naming every cookie quarantined this tick, since this is the
          one case that means "a real session just became unrecoverable".
        * The per-tick reconcile summary logs at INFO only when
          ``(injected, rejected, transport_failed)`` changed since this
          target's last tick, or on an unreachable→reachable edge;
          otherwise it's DEBUG — identical counts tick after tick carry no
          new information.
        * A transport failure that persists for
          ``_TRANSPORT_FAILURE_CHRONIC_TICKS`` consecutive ticks (~1 min)
          gets exactly one WARNING marking it as turned chronic rather than
          transient, then stays quiet until it recovers (streak resets)."""
        reachable_by_target: dict[str, bool] = {}
        last_summary_by_target: dict[str, tuple[int, int, int]] = {}
        transport_fail_streak_by_target: dict[str, int] = {}
        while True:
            try:
                await asyncio.sleep(RECONNECT_POLL_SECONDS)
                for target in routes_mod._cdp_list_urls(self.ctx):
                    was_reachable = reachable_by_target.get(target, False)
                    try:
                        reachable = cdp.cdp_ws_url(target) is not None
                        if reachable:
                            result = await asyncio.to_thread(
                                routes_mod.restore_persisted_cookies, self.ctx, self.store, target)

                            if result.quarantined:
                                names = ", ".join(
                                    f"{q['name']}@{q['domain']} ({q['reason']})"
                                    for q in result.quarantined)
                                log.warning(
                                    "Cookie reconnect [%s]: quarantining %s cookie(s): %s",
                                    target, len(result.quarantined), names)

                            edge = not was_reachable
                            summary = (result.injected, result.rejected, result.transport_failed)
                            changed = summary != last_summary_by_target.get(target)
                            if result.injected or result.rejected or result.decrypt_dead or result.transport_failed:
                                log_fn = log.info if (changed or edge) else log.debug
                                log_fn(
                                    "Cookie reconnect [%s]: %sreconciled %s persisted cookie(s) "
                                    "(%s rejected, %s decrypt-dead, %s transport-failed)",
                                    target, "browser back online, " if edge else "",
                                    result.injected, result.rejected, result.decrypt_dead,
                                    result.transport_failed)
                            last_summary_by_target[target] = summary

                            if result.transport_failed:
                                streak = transport_fail_streak_by_target.get(target, 0) + 1
                                transport_fail_streak_by_target[target] = streak
                                if streak == _TRANSPORT_FAILURE_CHRONIC_TICKS:
                                    log.warning(
                                        "Cookie reconnect [%s]: transport failures persisting for "
                                        "%s consecutive ticks (~1 min) — looks chronic, not transient",
                                        target, streak)
                            else:
                                transport_fail_streak_by_target[target] = 0
                        reachable_by_target[target] = reachable
                    except Exception:
                        log.warning(
                            "Cookie reconnect: target %s failed this tick", target, exc_info=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("Cookie reconnect loop iteration failed", exc_info=True)

    async def deactivate(self) -> None:
        task = getattr(self, "_reconnect_task", None)
        if task is not None:
            task.cancel()
        log.info("aw-app-proxy deactivated")
