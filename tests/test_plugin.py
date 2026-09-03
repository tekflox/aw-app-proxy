"""allowed_networks resolution — ctx.config never carries manifest config_schema
defaults (see plugin.py's DEFAULT_ALLOWED_NETWORKS docstring), so the fallback
used when an app is installed with no explicit override is critical: it's what
actually gates whether aw-app-browser (a non-loopback client on the shared
podman network) can use the CONNECT tunnel at all."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from proxy_app import cdp, plugin  # noqa: E402
from proxy_app import routes as routes_mod  # noqa: E402
from proxy_app.cookie_store import CookieStore  # noqa: E402
from proxy_app.plugin import DEFAULT_ALLOWED_NETWORKS, resolve_allowed_networks  # noqa: E402

from tests.test_cookie_store import FakeCtx  # noqa: E402


def test_empty_config_falls_back_to_rfc1918_default():
    assert resolve_allowed_networks({}) == DEFAULT_ALLOWED_NETWORKS
    assert "10.0.0.0/8" in resolve_allowed_networks({})


def test_explicit_config_overrides_default():
    assert resolve_allowed_networks({"allowed_networks": ["192.0.2.0/24"]}) == ["192.0.2.0/24"]


def test_reconnect_loop_reconciles_on_every_tick_not_only_on_reachability_edge(monkeypatch):
    """Regression: the loop used to re-inject persisted cookies only on an
    unreachable -> reachable transition, so a browser that stayed
    continuously CDP-reachable while individual cookies silently drifted
    (cleared/evicted in-browser, or by another CDP client) was never
    reconciled. With the browser reachable across two consecutive ticks and
    no transition in between, restore_persisted_cookies must still fire on
    both — not just the first."""
    monkeypatch.setattr(plugin, "RECONNECT_POLL_SECONDS", 0)
    monkeypatch.setattr(cdp, "cdp_ws_url", lambda _url: "ws://fake")

    calls = []
    monkeypatch.setattr(
        routes_mod, "restore_persisted_cookies",
        lambda ctx, store: (calls.append(1), (1, 0))[1])

    app_plugin = plugin.ProxyAppPlugin()
    app_plugin.ctx = FakeCtx()
    app_plugin.store = CookieStore(app_plugin.ctx)

    async def run_until_two_ticks():
        task = asyncio.create_task(app_plugin._cookie_reconnect_loop())
        try:
            while len(calls) < 2:
                await asyncio.sleep(0)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(asyncio.wait_for(run_until_two_ticks(), timeout=5))

    assert len(calls) >= 2
