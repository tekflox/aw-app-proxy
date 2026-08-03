"""FastAPI sub-app mounted at ``/api/apps/proxy`` (``ctx.routes.register``).

Ports the monolith's ``src/api/routes/proxy_cookies.py`` (5 endpoints) onto
``ctx.db`` + ``ctx.secrets``, and adds two endpoints so the browser
extensions and the settings window have something to point at:

GET    /cookie-keys                → live browser cookies + persisted flag
GET    /persistent-cookies         → persisted cookie names
POST   /persistent-cookies/{name}  → fetch from browser, encrypt, upsert
DELETE /persistent-cookies/{name}  → remove from persistence
GET    /persisted-cookie-values    → decrypted values (debug/inspection —
                                      proxy_server.py reads the DB directly
                                      on startup, see cookie_store.py)
POST   /browser-cookies/clear      → clear cookies in the live browser
GET    /extensions/chrome.zip      → download the Chrome sync extension
GET    /extensions/ios-readme      → iOS/Safari extension setup instructions
"""
from __future__ import annotations

import io
import os
import re
import zipfile

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from . import cdp
from .cookie_store import CookieStore
from .crypto import decrypt, encrypt

_PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXT_CHROME_DIR = os.path.join(_PACKAGE_ROOT, "extensions", "aw-sync-chrome")
_EXT_IOS_README = os.path.join(_PACKAGE_ROOT, "extensions", "aw-sync-ios", "README.md")


def _workspace_api_host() -> str:
    """This workspace's own public API domain (F4 three-plane split — the
    SPA is at ``<slug>.workspace.aw.tekflox.com``, its API at
    ``api.<slug>.workspace.aw.tekflox.com``). Decoupled here instead of
    hand-typed into the extension by every user (Frederico's ask,
    2026-08-02) — AW_WORKSPACE is set on every aw-workspace process."""
    slug = os.environ.get("AW_WORKSPACE", "").strip()
    return f"api.{slug}.workspace.aw.tekflox.com" if slug else "aw.tekflox.com"


def _patch_default_host(popup_js_path: str) -> str:
    """Bake this workspace's real API host into the extension's
    DEFAULT_HOST constant so it works out of the box with zero manual
    configuration — same source file served to every workspace, only the
    baked-in default differs per download."""
    src = open(popup_js_path, encoding="utf-8").read()
    return re.sub(
        r'const DEFAULT_HOST = "[^"]*";',
        f'const DEFAULT_HOST = "{_workspace_api_host()}";',
        src,
        count=1,
    )


def _cdp_list_url(ctx) -> str:
    return ctx.config.get("browser_cdp_list_url") or cdp.cdp_list_url_default()


def restore_persisted_cookies(ctx, store: CookieStore) -> tuple[int, int]:
    """Inject every persisted cookie into the live browser via CDP,
    best-effort. Shared by the periodic reconnect-loop in ``plugin.py``
    (browser comes back online → catch it up on whatever was persisted
    while it was down) and available for a manual "restore now" call.
    Returns ``(injected, failed)``; ``(0, 0)`` if there's nothing to do or
    CDP isn't reachable right now."""
    rows = store.all_rows()
    if not rows:
        return 0, 0
    ws_url = cdp.cdp_ws_url(_cdp_list_url(ctx))
    if not ws_url:
        return 0, 0

    sock = cdp.open_ws(ws_url)
    injected, failed = 0, 0
    try:
        for msg_id, row in enumerate(rows, start=1):
            try:
                value = decrypt(ctx, row["value_enc"])
            except ValueError:
                failed += 1
                continue
            secure = bool(row["secure"])
            scheme = "https" if secure else "http"
            domain = (row["domain"] or "").lstrip(".")
            params = {
                "name": row["name"], "value": value,
                "path": row["path"] or "/", "secure": secure,
                "httpOnly": bool(row["http_only"]), "sameSite": row["same_site"] or "Lax",
                "url": f"{scheme}://{domain}/", "domain": row["domain"],
            }
            if row["expires"]:
                params["expires"] = row["expires"]
            result = cdp.send_recv(sock, msg_id, "Network.setCookie", params)
            if result and result.get("result", {}).get("success"):
                injected += 1
            else:
                failed += 1
    finally:
        sock.close()
    return injected, failed


def build_app(ctx, store: CookieStore | None = None) -> FastAPI:
    app = FastAPI()
    if store is None:
        store = CookieStore(ctx)
    store.ensure_table()

    @app.get("/status")
    async def status():
        from .plugin import SERVICE_ID
        svc_status = ctx.services.status(SERVICE_ID)
        ws_url = cdp.cdp_ws_url(_cdp_list_url(ctx))
        return {**svc_status, "cdp_reachable": ws_url is not None,
                "proxy_port": ctx.config.get("proxy_port") or 9124}

    @app.get("/cookie-keys")
    async def get_cookie_keys():
        ws_url = cdp.cdp_ws_url(_cdp_list_url(ctx))
        if not ws_url:
            return {"keys": [], "error": "CDP not reachable — is the browser running?"}
        sock = cdp.open_ws(ws_url)
        result = cdp.send_recv(sock, 1, "Network.getAllCookies", {})
        sock.close()
        if result is None:
            return {"keys": [], "error": "CDP command failed"}

        persisted_names = set(store.list_names())
        seen: dict[str, dict] = {}
        for c in result.get("result", {}).get("cookies", []):
            name = c.get("name")
            if not name or name in seen:
                continue
            seen[name] = {"name": name, "domain": c.get("domain", ""), "persisted": name in persisted_names}
        return {"keys": sorted(seen.values(), key=lambda x: x["name"])}

    @app.get("/persistent-cookies")
    async def get_persistent():
        return {"persistent_cookies": store.list_names()}

    @app.post("/persistent-cookies/{name}")
    async def persist_cookie(name: str):
        ws_url = cdp.cdp_ws_url(_cdp_list_url(ctx))
        if not ws_url:
            return JSONResponse({"error": "CDP not reachable — is the browser running?"}, status_code=502)
        sock = cdp.open_ws(ws_url)
        result = cdp.send_recv(sock, 1, "Network.getAllCookies", {})
        sock.close()
        if result is None:
            return JSONResponse({"error": "CDP command failed"}, status_code=502)

        cookie = next((c for c in result.get("result", {}).get("cookies", [])
                        if c.get("name") == name), None)
        if cookie is None:
            return JSONResponse({"error": f"Cookie '{name}' not found in browser"}, status_code=404)

        store.upsert({
            "name": name,
            "value_enc": encrypt(ctx, cookie.get("value", "")),
            "domain": cookie.get("domain", ""),
            "path": cookie.get("path", "/"),
            "secure": cookie.get("secure", False),
            "http_only": cookie.get("httpOnly", False),
            "same_site": cookie.get("sameSite") or "Lax",
            "expires": cookie.get("expires") or None,
        })
        return {"ok": True, "name": name}

    @app.delete("/persistent-cookies/{name}")
    async def unpersist_cookie(name: str):
        store.delete(name)
        return {"ok": True, "name": name}

    @app.get("/persisted-cookie-values")
    async def get_persisted_values():
        rows = store.all_rows()
        cookies = []
        protected_names = []
        for row in rows:
            protected_names.append(row["name"])
            try:
                value = decrypt(ctx, row["value_enc"])
            except ValueError:
                continue
            entry = {
                "name": row["name"], "value": value, "domain": row["domain"],
                "path": row["path"], "secure": bool(row["secure"]),
                "httpOnly": bool(row["http_only"]), "sameSite": row["same_site"],
            }
            if row["expires"]:
                entry["expires"] = row["expires"]
            cookies.append(entry)
        return {"cookies": cookies, "protected_names": protected_names}

    @app.post("/sync-cookies")
    async def sync_cookies(body: dict = Body(default={})):
        """Receive cookies exported by the aw-sync browser extension.

        Persist-then-inject, decoupled (2026-08-03 design change, Frederico):
        the browser (aw-app-browser, a separate Tier-2 app) being down used
        to make the whole sync request 502 and lose the cookies entirely —
        step 2 (live inject) was a precondition for step 1 (persist). Now:

        1. Persist every incoming cookie to ``cookie_store`` unconditionally
           — this is the durable source of truth regardless of whether the
           browser is reachable right now.
        2. THEN, best-effort, inject live via CDP if the browser happens to
           be reachable right now, so an already-open session updates
           immediately.

        If the browser is offline, a later reconnect is handled by the
        periodic CDP-reachability poll in ``plugin.py``
        (``restore_persisted_cookies``), not by this endpoint."""
        cookies = body.get("cookies") or []
        if not cookies:
            return JSONResponse({"error": "No cookies"}, status_code=400)

        persisted = 0
        for cookie in cookies:
            name = cookie.get("name")
            if not name:
                continue
            store.upsert({
                "name": name,
                "value_enc": encrypt(ctx, cookie.get("value", "")),
                "domain": cookie.get("domain", ""),
                "path": cookie.get("path", "/"),
                "secure": bool(cookie.get("secure", False)),
                "http_only": bool(cookie.get("httpOnly", False)),
                "same_site": cookie.get("sameSite") or "Lax",
                "expires": cookie.get("expirationDate") or None,
            })
            persisted += 1

        ws_url = cdp.cdp_ws_url(_cdp_list_url(ctx))
        if not ws_url:
            return {"persisted": persisted, "injected": 0, "failed": 0,
                    "browser_reachable": False}

        sock = cdp.open_ws(ws_url)
        injected, failed = 0, 0
        try:
            for msg_id, cookie in enumerate(cookies, start=1):
                name = cookie.get("name", "")
                is_host_prefix = name.startswith("__Host-")
                is_secure_prefix = name.startswith("__Secure-")
                same_site = cookie.get("sameSite", "Lax")
                secure = bool(cookie.get("secure", False)) or is_host_prefix \
                    or is_secure_prefix or same_site == "None"
                scheme = "https" if secure else "http"
                domain = cookie.get("domain", "").lstrip(".")
                params = {
                    "name": name, "value": cookie.get("value", ""),
                    "path": "/" if is_host_prefix else cookie.get("path", "/"),
                    "secure": secure, "httpOnly": cookie.get("httpOnly", False),
                    "sameSite": same_site, "url": f"{scheme}://{domain}/",
                }
                if not is_host_prefix:
                    params["domain"] = cookie.get("domain", "")
                if cookie.get("expirationDate"):
                    params["expires"] = cookie["expirationDate"]
                result = cdp.send_recv(sock, msg_id, "Network.setCookie", params)
                if result and result.get("result", {}).get("success"):
                    injected += 1
                else:
                    failed += 1
        finally:
            sock.close()

        return {"persisted": persisted, "injected": injected, "failed": failed,
                "browser_reachable": True}

    @app.post("/clear-cookies")  # alias the aw-sync extensions POST to
    @app.post("/browser-cookies/clear")
    async def clear_browser_cookies(body: dict = Body(default={})):
        clear_all = bool(body.get("all"))
        cookies = body.get("cookies") or []
        if not clear_all and not cookies:
            return JSONResponse({"error": "nothing to clear"}, status_code=400)

        ws_url = cdp.cdp_ws_url(_cdp_list_url(ctx))
        if not ws_url:
            return JSONResponse({"error": "CDP not reachable — is the browser running?"}, status_code=502)
        sock = cdp.open_ws(ws_url)
        if clear_all:
            result = cdp.send_recv(sock, 1, "Network.clearBrowserCookies", {})
            sock.close()
            return {"ok": True, "cleared": "all"} if result is not None else JSONResponse(
                {"error": "CDP command failed"}, status_code=502)

        cleared = 0
        for i, c in enumerate(cookies, start=1):
            name = c.get("name", "")
            if not name:
                continue
            params: dict = {"name": name}
            if c.get("domain"):
                params["domain"] = c["domain"]
                params["path"] = c.get("path", "/")
            result = cdp.send_recv(sock, i, "Network.deleteCookies", params)
            if result is not None:
                cleared += 1
        sock.close()
        return {"ok": True, "cleared": cleared}

    @app.get("/extensions/chrome.zip")
    async def download_chrome_extension():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in os.listdir(_EXT_CHROME_DIR):
                fpath = os.path.join(_EXT_CHROME_DIR, fname)
                if not os.path.isfile(fpath):
                    continue
                if fname == "popup.js":
                    zf.writestr(fname, _patch_default_host(fpath))
                else:
                    zf.write(fpath, arcname=fname)
        buf.seek(0)
        return StreamingResponse(
            buf, media_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=aw-sync-chrome-extension.zip"},
        )

    @app.get("/extensions/ios-readme")
    async def ios_extension_readme():
        try:
            with open(_EXT_IOS_README, encoding="utf-8") as f:
                return PlainTextResponse(f.read())
        except FileNotFoundError:
            return PlainTextResponse("iOS extension README not found.", status_code=404)

    return app
