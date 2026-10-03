"""``resolve_cdp_list_urls`` config resolution — ``AppContext.config`` never
merges ``aw-app.json``'s ``config_schema`` defaults (the same trap documented
in ``plugin.py``'s ``DEFAULT_ALLOWED_NETWORKS`` docstring), so the code-level
default list in ``cdp.py`` is what an app installed with no explicit
``browser_cdp_list_urls``/``browser_cdp_list_url`` override actually gets.

Run: python -m pytest tests/test_cdp.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from proxy_app import cdp  # noqa: E402


def test_default_list_has_browser_then_kali():
    urls = cdp.cdp_list_urls_default()
    assert urls == [
        "http://aw-app-browser:9223/json/list",
        "http://aw-app-kali-linux:9223/json/list",
    ]


def test_singular_default_delegates_to_first_entry():
    assert cdp.cdp_list_url_default() == cdp.cdp_list_urls_default()[0]


def test_empty_config_falls_back_to_the_code_default_list():
    assert cdp.resolve_cdp_list_urls({}) == cdp.cdp_list_urls_default()


def test_plural_config_wins_over_everything():
    config = {
        "browser_cdp_list_urls": ["http://one/json/list", "http://two/json/list"],
        "browser_cdp_list_url": "http://singular/json/list",
    }
    assert cdp.resolve_cdp_list_urls(config) == ["http://one/json/list", "http://two/json/list"]


def test_deprecated_singular_override_wins_over_the_default_when_plural_unset():
    config = {"browser_cdp_list_url": "http://singular/json/list"}
    assert cdp.resolve_cdp_list_urls(config) == ["http://singular/json/list"]


def test_empty_string_singular_override_does_not_shadow_the_default():
    """aw-app.json declares browser_cdp_list_url's default as "" (empty) now
    that the plural key is canonical — an installed-with-no-override app
    must not resolve to a single empty-string target."""
    assert cdp.resolve_cdp_list_urls({"browser_cdp_list_url": ""}) == cdp.cdp_list_urls_default()
