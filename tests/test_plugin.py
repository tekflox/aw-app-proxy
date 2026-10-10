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
    both — not just the first. Pinned to a single target here (see the
    sibling multi-target test below) so the tick count isn't entangled with
    the number of configured CDP targets."""
    monkeypatch.setattr(plugin, "RECONNECT_POLL_SECONDS", 0)
    monkeypatch.setattr(cdp, "cdp_ws_url", lambda _url: "ws://fake")
    monkeypatch.setattr(routes_mod, "_cdp_list_urls", lambda _ctx: ["http://fake-target/json/list"])

    calls = []
    monkeypatch.setattr(
        routes_mod, "restore_persisted_cookies",
        lambda ctx, store, target=None: (calls.append(1), routes_mod.ReconcileResult(injected=1))[1])

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


def test_reconnect_loop_iterates_targets_independently(monkeypatch):
    """One target being unreachable (e.g. Kali not installed, or stopped)
    must not block reconciling the others — the multi-target fan-out this
    loop gained for the Kali cookie-sync ADR."""
    monkeypatch.setattr(plugin, "RECONNECT_POLL_SECONDS", 0)
    up, down = "http://up/json/list", "http://down/json/list"
    monkeypatch.setattr(routes_mod, "_cdp_list_urls", lambda _ctx: [up, down])
    monkeypatch.setattr(cdp, "cdp_ws_url", lambda url: "ws://fake" if url == up else None)

    calls = []
    monkeypatch.setattr(
        routes_mod, "restore_persisted_cookies",
        lambda ctx, store, target=None: (calls.append(target), routes_mod.ReconcileResult(injected=1))[1])

    app_plugin = plugin.ProxyAppPlugin()
    app_plugin.ctx = FakeCtx()
    app_plugin.store = CookieStore(app_plugin.ctx)

    async def run_a_few_ticks():
        task = asyncio.create_task(app_plugin._cookie_reconnect_loop())
        try:
            while len(calls) < 3:
                await asyncio.sleep(0)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(asyncio.wait_for(run_a_few_ticks(), timeout=5))

    assert calls
    assert set(calls) == {up}  # the unreachable target never got a restore call


def _run_ticks(app_plugin, calls, n, timeout=5):
    """Runs the reconnect loop until tick ``n`` has fully completed —
    including its post-``restore_persisted_cookies`` logging, which happens
    synchronously in the loop's own task AFTER ``to_thread`` hands back
    control. ``restore_persisted_cookies`` itself runs in a worker thread,
    so ``calls.append()`` inside it can observably reach length ``n`` before
    the loop task gets scheduled to run tick ``n``'s logging — waiting one
    further call in guarantees tick ``n`` is done, not just started."""
    async def _run():
        task = asyncio.create_task(app_plugin._cookie_reconnect_loop())
        try:
            while len(calls) <= n:
                await asyncio.sleep(0)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(asyncio.wait_for(_run(), timeout=timeout))


def test_reconnect_loop_logs_warning_when_cookies_get_quarantined(monkeypatch, caplog):
    monkeypatch.setattr(plugin, "RECONNECT_POLL_SECONDS", 0)
    monkeypatch.setattr(cdp, "cdp_ws_url", lambda _url: "ws://fake")
    monkeypatch.setattr(routes_mod, "_cdp_list_urls", lambda _ctx: ["http://fake-target/json/list"])

    calls = []

    def fake_restore(ctx, store, target=None):
        calls.append(1)
        return routes_mod.ReconcileResult(
            injected=1, decrypt_dead=1,
            quarantined=[{"name": "myacinfo", "domain": ".apple.com",
                          "reason": "decrypt: key mismatch — row predates key rotation"}])

    monkeypatch.setattr(routes_mod, "restore_persisted_cookies", fake_restore)

    app_plugin = plugin.ProxyAppPlugin()
    app_plugin.ctx = FakeCtx()
    app_plugin.store = CookieStore(app_plugin.ctx)

    with caplog.at_level("WARNING", logger="aw_apps.proxy"):
        _run_ticks(app_plugin, calls, 1)

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("myacinfo@.apple.com" in w and "quarantining" in w for w in warnings)


def test_reconnect_loop_logs_info_only_when_summary_changes(monkeypatch, caplog):
    """Regression for the original symptom on this card: steady state
    (identical injected/rejected/transport_failed tick after tick) must go
    quiet — only a count CHANGE (or an unreachable->reachable edge) earns
    an INFO line; an unchanged tick is DEBUG."""
    monkeypatch.setattr(plugin, "RECONNECT_POLL_SECONDS", 0)
    monkeypatch.setattr(cdp, "cdp_ws_url", lambda _url: "ws://fake")
    monkeypatch.setattr(routes_mod, "_cdp_list_urls", lambda _ctx: ["http://fake-target/json/list"])

    results = [
        routes_mod.ReconcileResult(injected=1),
        routes_mod.ReconcileResult(injected=1),  # unchanged vs previous tick -> DEBUG
        routes_mod.ReconcileResult(injected=2),  # changed again -> INFO
        routes_mod.ReconcileResult(injected=2),  # unchanged vs tick 3 -> DEBUG (completes tick 4's wait)
    ]
    calls = []

    def fake_restore(ctx, store, target=None):
        calls.append(1)
        return results[len(calls) - 1]

    monkeypatch.setattr(routes_mod, "restore_persisted_cookies", fake_restore)

    app_plugin = plugin.ProxyAppPlugin()
    app_plugin.ctx = FakeCtx()
    app_plugin.store = CookieStore(app_plugin.ctx)

    with caplog.at_level("DEBUG", logger="aw_apps.proxy"):
        _run_ticks(app_plugin, calls, 3)

    info_lines = [r.getMessage() for r in caplog.records
                  if r.levelname == "INFO" and "reconciled" in r.getMessage()]
    debug_lines = [r.getMessage() for r in caplog.records
                   if r.levelname == "DEBUG" and "reconciled" in r.getMessage()]
    # Exactly 2 INFOs (tick 1 first-ever, tick 3 count-changed) is a stable
    # assertion. Tick 4's own DEBUG line (unchanged vs tick 3) is only
    # guaranteed to have STARTED by the time _run_ticks returns, not
    # necessarily finished logging — so its presence isn't asserted on,
    # only that tick 2's DEBUG line (guaranteed complete) is there.
    assert len(info_lines) == 2
    assert len(debug_lines) >= 1


def test_reconnect_loop_logs_one_warning_when_transport_failures_turn_chronic(monkeypatch, caplog):
    monkeypatch.setattr(plugin, "RECONNECT_POLL_SECONDS", 0)
    monkeypatch.setattr(cdp, "cdp_ws_url", lambda _url: "ws://fake")
    monkeypatch.setattr(routes_mod, "_cdp_list_urls", lambda _ctx: ["http://fake-target/json/list"])

    calls = []
    monkeypatch.setattr(
        routes_mod, "restore_persisted_cookies",
        lambda ctx, store, target=None: (calls.append(1), routes_mod.ReconcileResult(transport_failed=1))[1])

    app_plugin = plugin.ProxyAppPlugin()
    app_plugin.ctx = FakeCtx()
    app_plugin.store = CookieStore(app_plugin.ctx)

    with caplog.at_level("WARNING", logger="aw_apps.proxy"):
        _run_ticks(app_plugin, calls, 5)

    chronic_warnings = [r for r in caplog.records
                        if r.levelname == "WARNING" and "chronic" in r.getMessage()]
    assert len(chronic_warnings) == 1
