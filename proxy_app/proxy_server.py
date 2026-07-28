"""The CONNECT-tunnel + cookie-sync HTTP server — registered as a managed
service via ``ctx.services.register`` (see ``plugin.py``), so it runs as a
subprocess of the aw-workspace process (inherits ``PYTHONPATH=/opt/agentic-workspace``
from the Dockerfile, which is how it can ``import src.api.identity`` /
``import src.apps.secret_store`` below despite ``cwd`` being this app's own
package dir, not the aw-workspace repo root).

Ported from the monolith's ``tools/browser/proxy.py``. Two behavioural
differences from the original:

* Auth check (``_check_aw_auth``) verifies the ``aw_id_jwt`` EdDSA JWT
  **offline** via ``src.api.identity.decode_identity_jwt`` instead of an
  HTTP round-trip to ``/api/auth/status`` — the decoupled-apps identity
  model (F2) makes this possible without a network call.
* Startup cookie restore + persist-on-sync reads/writes
  ``app__proxy__persisted_cookies`` directly via ``cookie_store.py``
  (same Postgres schema this workspace's own process uses) instead of
  calling back into an HTTP API with a bearer API key.

CDP target (``--cdp-list-url`` / ``AW_PROXY_CDP_LIST_URL``, default
``http://127.0.0.1:9223/json/list``): still the old hard-coded container-Chrome
debug port from ``tools/browser/``. **Not yet reconciled** with
``aw-app-browser``'s new Tier-2 (podman container; CDP reachable at
``/api/apps/browser/*``) — see this repo's README. Configurable so the
orchestrator can point it at the right place once that's decided, without
needing new code here.

Usage:
    python -m proxy_app.proxy_server [--port 9124] [--cdp-list-url URL]
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import cdp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("proxy_app.proxy_server")

DEFAULT_PORT = 9124
_COOKIE_ENDPOINTS = ("/sync-cookies", "/clear-cookies")

_CDP_LIST_URL = os.environ.get("AW_PROXY_CDP_LIST_URL", cdp.cdp_list_url_default())
_ALLOWED_NETWORKS = [ipaddress.ip_network(n, strict=False)
                      for n in json.loads(os.environ.get("AW_PROXY_ALLOWED_NETWORKS", '["127.0.0.0/8"]'))]


def _fernet_key() -> bytes | None:
    """Read the same Fernet key ``crypto.py``/``ctx.secrets`` uses, directly
    from the workspace secret store file (no ``ctx`` in this subprocess)."""
    try:
        from .crypto import read_key_direct
        return read_key_direct()
    except Exception:
        log.warning("could not read cookie_encryption_key from secret store", exc_info=True)
        return None


class ProxyHandler(BaseHTTPRequestHandler):
    timeout = None

    def do_GET(self):
        self._forward_http()

    def do_CONNECT(self):
        if not self._check_allowed():
            self.connection.close()
            return
        host, port = self._parse_host_port(self.path, default_port=443)
        if host == "host.docker.internal":
            host = "127.0.0.1"
        log.info(f"CONNECT {host}:{port}")
        try:
            remote = socket.create_connection((host, port), timeout=30)
        except Exception as e:
            self.send_error(502, f"Cannot connect to {host}:{port}: {e}")
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        self._tunnel(self.connection, remote)

    def do_POST(self):
        if self.path == "/sync-cookies":
            self._handle_sync_cookies()
            return
        if self.path == "/clear-cookies":
            self._handle_clear_cookies()
            return
        self._forward_http()

    def do_PUT(self):
        self._forward_http()

    def do_DELETE(self):
        self._forward_http()

    def do_HEAD(self):
        self._forward_http()

    def do_PATCH(self):
        self._forward_http()

    def do_OPTIONS(self):
        if self.path in _COOKIE_ENDPOINTS:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, x-aw-jwt")
            self.send_header("Access-Control-Max-Age", "86400")
            self.end_headers()
            return
        self._forward_http()

    def _check_allowed(self):
        try:
            addr = ipaddress.ip_address(self.client_address[0])
            if any(addr in net for net in _ALLOWED_NETWORKS):
                return True
        except ValueError:
            pass
        log.warning(f"Blocked {self.client_address[0]}")
        return False

    def _check_aw_auth(self):
        """Verify the caller's aw_id_jwt (header X-AW-JWT or Cookie), offline."""
        from src.api.identity import COOKIE_NAME, decode_identity_jwt

        token = (self.headers.get("X-AW-JWT") or "").strip()
        if not token:
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE_NAME:
                    token = v.strip()
                    break
        if not token:
            return False
        return decode_identity_jwt(token) is not None

    def _send_unauthorized(self):
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, x-aw-jwt")
        self.end_headers()
        self.wfile.write(json.dumps({
            "error": "unauthorized",
            "message": "Not logged in. Open the workspace in a tab and log in first.",
        }).encode())

    def _handle_sync_cookies(self):
        if not self._check_aw_auth():
            self._send_unauthorized()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, x-aw-jwt")
        self.end_headers()

        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self.wfile.write(json.dumps({"error": "No body"}).encode())
            return
        try:
            data = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode())
            return

        cookies = data.get("cookies", [])
        if not cookies:
            self.wfile.write(json.dumps({"error": "No cookies"}).encode())
            return
        log.info(f"Cookie sync: received {len(cookies)} cookies")

        ws_url = cdp.cdp_ws_url(_CDP_LIST_URL)
        if not ws_url:
            self.wfile.write(json.dumps({"error": "No CDP page found"}).encode())
            return
        injected, failed = self._inject_via_cdp(ws_url, cookies)
        log.info(f"Cookie sync: {injected} injected, {failed} failed")
        self.wfile.write(json.dumps({"injected": injected, "failed": failed}).encode())

    def _handle_clear_cookies(self):
        if not self._check_aw_auth():
            self._send_unauthorized()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, x-aw-jwt")
        self.end_headers()

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode())
            return

        clear_all = bool(data.get("all"))
        cookies = data.get("cookies", []) or []
        if not clear_all and not cookies:
            self.wfile.write(json.dumps({"error": "Nothing to clear"}).encode())
            return

        ws_url = cdp.cdp_ws_url(_CDP_LIST_URL)
        if not ws_url:
            self.wfile.write(json.dumps({"error": "No CDP page found"}).encode())
            return

        sock = cdp.open_ws(ws_url)
        cleared, failed = 0, 0
        if clear_all:
            result = cdp.send_recv(sock, 1, "Network.clearBrowserCookies", {})
            cleared = -1 if result and "result" in result and "error" not in result else 0
            failed = 0 if cleared == -1 else 1
        else:
            for i, c in enumerate(cookies, start=1):
                name = c.get("name", "")
                if not name:
                    failed += 1
                    continue
                params = {"name": name}
                if c.get("domain"):
                    params["domain"] = c["domain"]
                    params["path"] = c.get("path", "/")
                elif c.get("url"):
                    params["url"] = c["url"]
                else:
                    failed += 1
                    continue
                result = cdp.send_recv(sock, i, "Network.deleteCookies", params)
                if result and "error" not in result:
                    cleared += 1
                else:
                    failed += 1
        sock.close()
        log.info(f"Cookie clear: {cleared} cleared, {failed} failed")
        self.wfile.write(json.dumps({"cleared": cleared, "failed": failed}).encode())

    def _inject_via_cdp(self, ws_url, cookies):
        injected, failed = 0, 0
        try:
            sock = cdp.open_ws(ws_url)
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
            sock.close()
        except Exception as e:
            log.error(f"Cookie injection failed: {e}")

        if injected > 0:
            self._persist_cookies(cookies)
        return injected, failed

    def _persist_cookies(self, cookies):
        """Encrypt + upsert extension-sent cookies straight into the
        ``app__proxy__persisted_cookies`` table (see module docstring)."""
        key = _fernet_key()
        if key is None:
            log.warning("no cookie_encryption_key yet (app never activated?) — skipping persist")
            return
        from .cookie_store import upsert_direct
        from .crypto import encrypt_direct

        persisted = 0
        for c in cookies:
            name = c.get("name", "")
            if not name:
                continue
            try:
                upsert_direct({
                    "name": name,
                    "value_enc": encrypt_direct(key, c.get("value", "")),
                    "domain": c.get("domain", ""),
                    "path": c.get("path", "/"),
                    "secure": c.get("secure"),
                    "http_only": c.get("httpOnly"),
                    "same_site": c.get("sameSite"),
                    "expires": c.get("expirationDate"),
                })
                persisted += 1
            except Exception:
                log.warning(f"Failed to persist cookie {name!r} to DB", exc_info=True)
        log.info(f"Cookie sync: persisted {persisted}/{len(cookies)} cookies to DB")

    def _forward_http(self):
        if not self._check_allowed():
            self.connection.close()
            return
        import urllib.error
        import urllib.request

        url = self.path.replace("host.docker.internal", "127.0.0.1")
        log.info(f"{self.command} {url}")
        skip = {"host", "proxy-connection", "connection", "keep-alive",
                "proxy-authenticate", "proxy-authorization", "te",
                "trailer", "transfer-encoding", "upgrade"}
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length > 0 else None
            req = urllib.request.Request(url, data=body, method=self.command)
            for k, v in self.headers.items():
                if k.lower() not in skip:
                    req.add_header(k, v)

            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    raise urllib.error.HTTPError(newurl, code, msg, headers, fp)

            opener = urllib.request.build_opener(_NoRedirect)
            try:
                resp = opener.open(req, timeout=30)
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in skip:
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp.read())
                resp.close()
            except urllib.error.HTTPError as e:
                self.send_response(e.code)
                for k, v in e.headers.items():
                    if k.lower() not in skip:
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(e.read())
        except Exception as e:
            self.send_error(502, str(e))

    def _tunnel(self, client_sock, remote_sock):
        client_sock.settimeout(self.timeout)
        remote_sock.settimeout(self.timeout)

        def forward(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except (socket.timeout, OSError, BrokenPipeError):
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        t1 = threading.Thread(target=forward, args=(client_sock, remote_sock), daemon=True)
        t2 = threading.Thread(target=forward, args=(remote_sock, client_sock), daemon=True)
        t1.start()
        t2.start()
        t1.join(timeout=self.timeout)
        t2.join(timeout=self.timeout)
        try:
            remote_sock.close()
        except OSError:
            pass

    def _parse_host_port(self, path, default_port=80):
        if ":" in path:
            host, port = path.rsplit(":", 1)
            try:
                port = int(port)
            except ValueError:
                port = default_port
        else:
            host, port = path, default_port
        return host, port

    def log_message(self, format, *args):
        pass


class ThreadPoolHTTPServer(HTTPServer):
    """Bounded thread pool instead of unbounded per-connection threads."""
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, *args, max_workers: int = 32, **kwargs):
        super().__init__(*args, **kwargs)
        from concurrent.futures import ThreadPoolExecutor
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="proxy")

    def process_request(self, request, client_address):
        self._pool.submit(self._handle, request, client_address)

    def _handle(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)

    def server_close(self):
        super().server_close()
        self._pool.shutdown(wait=False)


def _restore_cookies() -> None:
    """On startup: re-inject persisted cookies from the DB into the browser.
    Never deletes anything from the browser — only re-applies the persisted set."""
    from .cookie_store import read_persisted_values_direct

    key = _fernet_key()
    if key is None:
        return
    rows = read_persisted_values_direct()
    if not rows:
        return

    from .crypto import decrypt_direct

    ws_url = cdp.cdp_ws_url(_CDP_LIST_URL, timeout=3.0)
    if not ws_url:
        return

    try:
        sock = cdp.open_ws(ws_url)
        msg_id = 1
        injected = 0
        for row in rows:
            value = decrypt_direct(key, row["value_enc"])
            if value is None:
                continue
            scheme = "https" if row["secure"] else "http"
            domain = (row["domain"] or "").lstrip(".")
            params = {
                "name": row["name"], "value": value, "domain": row["domain"],
                "path": row["path"], "secure": bool(row["secure"]),
                "httpOnly": bool(row["http_only"]), "sameSite": row["same_site"],
                "url": f"{scheme}://{domain}/",
            }
            if row["expires"]:
                params["expires"] = row["expires"]
            cdp.send_recv(sock, msg_id, "Network.setCookie", params)
            msg_id += 1
            injected += 1
        sock.close()
        if injected:
            log.info(f"Startup: injected {injected} persisted cookies from DB")
    except Exception:
        log.warning("Startup cookie restore failed", exc_info=True)


def main():
    parser = argparse.ArgumentParser(description="aw-app-proxy cookie proxy")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--cdp-list-url", default=None)
    args = parser.parse_args()

    global _CDP_LIST_URL
    if args.cdp_list_url:
        _CDP_LIST_URL = args.cdp_list_url

    _restore_cookies()

    server = ThreadPoolHTTPServer((args.bind, args.port), ProxyHandler, max_workers=128)
    log.info(f"Proxy listening on {args.bind}:{args.port}, CDP target {_CDP_LIST_URL}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
