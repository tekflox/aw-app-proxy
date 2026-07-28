"""allowed_networks resolution — ctx.config never carries manifest config_schema
defaults (see plugin.py's DEFAULT_ALLOWED_NETWORKS docstring), so the fallback
used when an app is installed with no explicit override is critical: it's what
actually gates whether aw-app-browser (a non-loopback client on the shared
podman network) can use the CONNECT tunnel at all."""
from __future__ import annotations

from proxy_app.plugin import DEFAULT_ALLOWED_NETWORKS, resolve_allowed_networks


def test_empty_config_falls_back_to_rfc1918_default():
    assert resolve_allowed_networks({}) == DEFAULT_ALLOWED_NETWORKS
    assert "10.0.0.0/8" in resolve_allowed_networks({})


def test_explicit_config_overrides_default():
    assert resolve_allowed_networks({"allowed_networks": ["192.0.2.0/24"]}) == ["192.0.2.0/24"]
