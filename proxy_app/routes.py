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
import zipfile

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from . import cdp
from .cookie_store import CookieStore
from .crypto import decrypt, encrypt

_PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXT_CHROME_DIR = os.path.join(_PACKAGE_ROOT, "extensions", "aw-sync-chrome")
_EXT_IOS_README = os.path.join(_PACKAGE_ROOT, "extensions", "aw-sync-ios", "README.md")


def _cdp_list_url(ctx) -> str:
    return ctx.config.get("browser_cdp_list_url") or cdp.cdp_list_url_default()


def build_app(ctx) -> FastAPI:
    app = FastAPI()
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
        """Receive cookies exported by the aw-sync browser extension and
        inject them into the workspace's own Chrome via CDP Network.setCookie
        (ported from proxy_server.py's bespoke /sync-cookies handler — that
        one is unreachable from outside the workspace, since it's a raw
        socket server on an internal-only port; this route rides the same
        /api/apps/proxy mount the rest of the app already exposes publicly,
        auth handled by the framework's IdentityGuard instead of a
        hand-rolled JWT check)."""
        cookies = body.get("cookies") or []
        if not cookies:
            return JSONResponse({"error": "No cookies"}, status_code=400)

        ws_url = cdp.cdp_ws_url(_cdp_list_url(ctx))
        if not ws_url:
            return JSONResponse({"error": "CDP not reachable — is the browser running?"}, status_code=502)

        sock = cdp.open_ws(ws_url)
        injected, failed = 0, 0
        injected_cookies = []
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
                injected_cookies.append(cookie)
            else:
                failed += 1
        sock.close()

        for cookie in injected_cookies:
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

        return {"injected": injected, "failed": failed}

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
                if os.path.isfile(fpath):
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
