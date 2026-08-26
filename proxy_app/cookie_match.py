"""Cookie/URL matching — the ``store`` fallback's half of ``GET /cookies-for``.

The live-browser path does NOT use this: ``Network.getCookies`` with a
``urls`` filter makes Chrome apply its own matching rules, which is the
most correct answer available and one we should never try to out-guess.
This module exists only for the case where the browser is unreachable and
the persisted ``app__proxy__persisted_cookies`` rows are all we have —
plain rows with no engine attached, so somebody has to do the matching.

Deliberately narrower than RFC 6265: domain, path and ``secure`` only. No
``SameSite`` evaluation, because there is no "site of the initiating
context" here to compare against — the caller (aw-app-mini-browser's Bare
Server) is a server-side relay, not a browsing context. Treating every
fetch as same-site would over-share; treating it as cross-site would drop
the ``Lax`` cookies that carry most real sessions. So the dimension is
left out rather than guessed at, and the caller's own host allowlist is
what bounds the blast radius. Same reasoning applies to ``__Host-``/
``__Secure-`` prefixes: they constrain what a *server* may set, and these
rows were already accepted at set time.
"""
from __future__ import annotations

from urllib.parse import urlparse


def domain_matches(cookie_domain: str, host: str) -> bool:
    """RFC 6265 §5.1.3 — host-only match, or domain-suffix match on a
    leading-dot ("domain") cookie.

    A leading dot is the classic wire form and is what both CDP and the
    aw-sync extensions hand us; RFC 6265 dropped it as *required* but kept
    it as *permitted*, so both spellings have to work. ``.google.com``
    matches ``google.com`` itself as well as ``mail.google.com`` — the dot
    means "and subdomains", not "subdomains only".
    """
    cookie_domain = (cookie_domain or "").strip().lower()
    host = (host or "").strip().lower().rstrip(".")
    if not cookie_domain or not host:
        return False
    if cookie_domain.startswith("."):
        bare = cookie_domain[1:]
        return host == bare or host.endswith("." + bare)
    return host == cookie_domain


def path_matches(cookie_path: str, request_path: str) -> bool:
    """RFC 6265 §5.1.4. ``/`` matches everything; ``/app`` matches ``/app``,
    ``/app/x`` and ``/app?q`` — but NOT ``/applesauce``, which is exactly
    the case a naive ``startswith`` gets wrong."""
    cookie_path = cookie_path or "/"
    request_path = request_path or "/"
    if not request_path.startswith("/"):
        request_path = "/" + request_path
    if cookie_path == request_path:
        return True
    if not request_path.startswith(cookie_path):
        return False
    return cookie_path.endswith("/") or request_path[len(cookie_path):].startswith("/")


def matches(cookie: dict, url: str) -> bool:
    """True if ``cookie`` (a CDP-shaped dict) would be sent to ``url``."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    if cookie.get("secure") and parsed.scheme != "https":
        return False
    return (domain_matches(cookie.get("domain", ""), parsed.hostname or "")
            and path_matches(cookie.get("path", "/"), parsed.path or "/"))


def select(cookies: list[dict], url: str) -> list[dict]:
    return [c for c in cookies if matches(c, url)]
