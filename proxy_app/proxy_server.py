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

CDP targets (``--cdp-list-urls`` / ``AW_PROXY_CDP_LIST_URLS``, a JSON list,
default ``cdp.cdp_list_urls_default()``): writes (sync/clear/restore) fan out
to every target independently, one unreachable target must not skip the
others. The deprecated singular ``--cdp-list-url`` / ``AW_PROXY_CDP_LIST_URL``
is still accepted for one release and becomes a one-element list. Still
configurable (``browser_cdp_list_urls``/``browser_cdp_list_url`` in app
config, which ``plugin.py``/``routes.py`` prefer over the default) so targets
can move without new code here.

Usage:
    python -m proxy_app.proxy_server [--port 9124] [--cdp-list-urls '["url1","url2"]']
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import cdp
from . import mitm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("proxy_app.proxy_server")

DEFAULT_PORT = 9124
_COOKIE_ENDPOINTS = ("/sync-cookies", "/clear-cookies")

def _env_cdp_list_urls() -> list[str]:
    plural = os.environ.get("AW_PROXY_CDP_LIST_URLS")
    if plural:
        return json.loads(plural)
    singular = os.environ.get("AW_PROXY_CDP_LIST_URL")
    if singular:
        return [singular]
    return cdp.cdp_list_urls_default()


_CDP_LIST_URLS = _env_cdp_list_urls()
_ALLOWED_NETWORKS = [ipaddress.ip_network(n, strict=False)
                      for n in json.loads(os.environ.get("AW_PROXY_ALLOWED_NETWORKS", '["127.0.0.0/8"]'))]
_MITM_ENABLED = os.environ.get("AW_PROXY_MITM_DISABLED") != "1"
# Google's own bot-detection patent (US11184390B2) fingerprints the TLS
# ClientHello (JA3/JA4) on login requests — our MITM's outbound leg uses
# Python's OpenSSL, which doesn't match Chrome's BoringSSL fingerprint.
# These hosts get the raw splice tunnel instead, so Google sees Chrome's
# genuine TLS handshake end-to-end.
_MITM_BYPASS_HOSTS = {
    h.strip() for h in os.environ.get(
        "AW_PROXY_MITM_BYPASS_HOSTS",
        "accounts.google.com,accounts.youtube.com,myaccount.google.com",
    ).split(",") if h.strip()
}


def _fernet_key() -> bytes | None:
    """Read the same Fernet key ``crypto.py``/``ctx.secrets`` uses, directly
    from the workspace secret store file (no ``ctx`` in this subprocess)."""
    try:
        from .crypto import read_key_direct
        return read_key_direct()
    except Exception:
        log.warning("could not read cookie_encryption_key from secret store", exc_info=True)
        return None


def _read_message_head(rfile):
    start_line = rfile.readline()
    if not start_line:
        return None, None
    start_line = start_line.decode("iso-8859-1").rstrip("\r\n")
    if not start_line:
        return None, None
    headers = []
    while True:
        line = rfile.readline()
        if not line:
            break
        line = line.decode("iso-8859-1").rstrip("\r\n")
        if not line:
            break
        k, _, v = line.partition(":")
        headers.append((k.strip(), v.strip()))
    return start_line, headers


def _headers_get(headers, name):
    name = name.lower()
    for k, v in headers:
        if k.lower() == name:
            return v
    return None


def _read_body(rfile, headers):
    te = (_headers_get(headers, "Transfer-Encoding") or "").lower()
    if "chunked" in te:
        chunks = []
        while True:
            size_line = rfile.readline().decode("iso-8859-1").strip()
            if not size_line:
                break
            size = int(size_line.split(";")[0], 16)
            if size == 0:
                while True:
                    line = rfile.readline()
                    if not line or line in (b"\r\n", b"\n"):
                        break
                break
            chunks.append(rfile.read(size))
            rfile.read(2)
        return b"".join(chunks)
    length = _headers_get(headers, "Content-Length")
    if length:
        return rfile.read(int(length))
    return b""


def _reachable_targets(list_urls):
    """Resolve each configured CDP /json/list target to its page websocket
    URL, dropping any that aren't reachable right now — per-target failure
    isolation, one down target must not skip the others."""
    result = []
    for list_url in list_urls:
        ws_url = cdp.cdp_ws_url(list_url)
        if ws_url:
            result.append((list_url, ws_url))
    return result


def _write_message(sock, start_line, headers, body):
    lines = [start_line]
    for k, v in headers:
        lines.append(f"{k}: {v}")
    lines.append("")
    lines.append("")
    sock.sendall("\r\n".join(lines).encode("iso-8859-1"))
    if body:
        sock.sendall(body)


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

        if _MITM_ENABLED and port == 443 and host not in _MITM_BYPASS_HOSTS:
            try:
                self._mitm_connect(host, port)
                return
            except Exception as e:
                log.warning(f"MITM setup failed for {host}, falling back to raw tunnel: {e}")

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
        if self.path == "/restore-cookies":
            self._handle_restore_cookies()
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

        targets = _reachable_targets(_CDP_LIST_URLS)
        if not targets:
            self.wfile.write(json.dumps({"error": "No CDP page found"}).encode())
            return

        injected, failed = 0, 0
        for list_url, ws_url in targets:
            try:
                t_injected, t_failed = self._inject_via_cdp(ws_url, cookies)
            except Exception:
                log.warning(f"Cookie sync: target {list_url} failed", exc_info=True)
                continue
            injected += t_injected
            failed += t_failed

        if injected > 0:
            self._persist_cookies(cookies)

        log.info(f"Cookie sync: {injected} injected, {failed} failed across "
                 f"{len(targets)}/{len(_CDP_LIST_URLS)} reachable target(s)")
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

        targets = _reachable_targets(_CDP_LIST_URLS)
        if not targets:
            self.wfile.write(json.dumps({"error": "No CDP page found"}).encode())
            return

        cleared, failed = 0, 0
        cleared_all = False
        for list_url, ws_url in targets:
            try:
                t_cleared, t_failed = self._clear_via_cdp(ws_url, clear_all, cookies)
            except Exception:
                log.warning(f"Cookie clear: target {list_url} failed", exc_info=True)
                continue
            if t_cleared == -1:
                cleared_all = True
            else:
                cleared += t_cleared
            failed += t_failed
        cleared_out = -1 if cleared_all else cleared
        log.info(f"Cookie clear: {cleared_out} cleared, {failed} failed across "
                 f"{len(targets)}/{len(_CDP_LIST_URLS)} reachable target(s)")
        self.wfile.write(json.dumps({"cleared": cleared_out, "failed": failed}).encode())

    def _clear_via_cdp(self, ws_url, clear_all, cookies):
        """One target's worth of clear-cookies work — extracted so
        ``_handle_clear_cookies`` can fan it out across every configured
        target. Preserves the legacy ``-1`` "all cleared" sentinel."""
        sock = cdp.open_ws(ws_url)
        cleared, failed = 0, 0
        try:
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
        finally:
            sock.close()
        return cleared, failed

    def _handle_restore_cookies(self):
        """Trigger-only re-push of every persisted cookie into every
        configured CDP target. CIDR gate only (same as the CONNECT tunnel)
        — it returns no cookie data and ``Network.setCookie`` is idempotent,
        so a re-push from an already-up-to-date caller is harmless. Closes
        the lazy-launch gap: Kali's ``chromium-aw`` backgrounds a one-shot
        that calls this right after its Chromium comes up, instead of
        waiting for the next reconcile-loop tick."""
        if not self._check_allowed():
            self.connection.close()
            return
        summary = _restore_cookies()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(summary).encode())

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
        if url == "/mitm-ca-spki":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(mitm.ca_spki_b64().encode())
            return
        if url == "/mitm-ca.pem":
            self.send_response(200)
            self.send_header("Content-Type", "application/x-pem-file")
            self.end_headers()
            self.wfile.write(mitm.ca_cert_pem().encode())
            return
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
            except (socket.timeout, OSError, BrokenPipeError, ssl.SSLError):
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

    def _mitm_connect(self, host, port):
        server_ctx = mitm.server_ssl_context(host)
        self.send_response(200, "Connection Established")
        self.end_headers()
        try:
            tls_client = server_ctx.wrap_socket(self.connection, server_side=True)
        except ssl.SSLError as e:
            raise RuntimeError(f"client TLS handshake failed: {e}") from e

        raw_remote = socket.create_connection((host, port), timeout=30)
        remote_ctx = mitm.client_ssl_context()
        tls_remote = remote_ctx.wrap_socket(raw_remote, server_hostname=host)

        self._relay_http(tls_client, tls_remote, host)

    def _relay_http(self, client_sock, remote_sock, host):
        client_rfile = client_sock.makefile("rb")
        remote_rfile = remote_sock.makefile("rb")
        try:
            while True:
                req_line, req_headers = _read_message_head(client_rfile)
                if req_line is None:
                    break

                if (_headers_get(req_headers, "Upgrade") or "").strip():
                    _write_message(remote_sock, req_line, req_headers, b"")
                    self._tunnel(client_sock, remote_sock)
                    return

                req_body = _read_body(client_rfile, req_headers)
                simple = {k.lower(): v for k, v in req_headers}
                simple = mitm.rewrite_request_headers(simple)
                drop = {"transfer-encoding", "content-length", "connection", "proxy-connection"}
                new_headers = []
                seen = set()
                for k, v in req_headers:
                    lk = k.lower()
                    if lk in drop:
                        continue
                    new_headers.append((k, simple.get(lk, v)))
                    seen.add(lk)
                for lk, v in simple.items():
                    if lk not in seen and lk not in drop:
                        new_headers.append((lk, v))
                if req_body:
                    new_headers.append(("Content-Length", str(len(req_body))))
                new_headers.append(("Connection", "keep-alive"))
                _write_message(remote_sock, req_line, new_headers, req_body)

                resp_line, resp_headers = _read_message_head(remote_rfile)
                if resp_line is None:
                    break
                resp_body = _read_body(remote_rfile, resp_headers)
                resp_new_headers = [
                    (k, v) for k, v in resp_headers
                    if k.lower() not in ("transfer-encoding", "content-length", "connection")
                ]
                resp_new_headers.append(("Content-Length", str(len(resp_body))))
                resp_new_headers.append(("Connection", "keep-alive"))
                _write_message(client_sock, resp_line, resp_new_headers, resp_body)

                req_conn = (_headers_get(req_headers, "Connection") or "").lower()
                resp_conn = (_headers_get(resp_headers, "Connection") or "").lower()
                if "close" in req_conn or "close" in resp_conn:
                    break
        except (ConnectionError, ssl.SSLError, OSError):
            pass
        except Exception as e:
            log.info(f"MITM relay for {host} ended: {e}")
        finally:
            for sk in (client_sock, remote_sock):
                try:
                    sk.close()
                except Exception:
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
        # Constructed BEFORE super().__init__(): socketserver.TCPServer's own
        # __init__ calls self.server_close() from its exception handler if
        # server_bind()/server_activate() raises (e.g. port already in use),
        # and that was crashing with AttributeError (self._pool not set yet)
        # instead of letting the real OSError surface — see server_close().
        from concurrent.futures import ThreadPoolExecutor
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="proxy")
        super().__init__(*args, **kwargs)

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
        if self._pool is not None:
            self._pool.shutdown(wait=False)


def _restore_cookies_into(ws_url: str, rows, key: bytes) -> int:
    """Inject every persisted row into one CDP target. Returns the count
    injected."""
    from .crypto import decrypt_direct

    sock = cdp.open_ws(ws_url)
    try:
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
        return injected
    finally:
        sock.close()


def _restore_cookies() -> dict:
    """Re-inject persisted cookies from the DB into every reachable CDP
    target, independently — one unreachable/failing target must not skip
    the others. Never deletes anything — only re-applies the persisted set.
    Called at startup and by the trigger-only ``POST /restore-cookies``
    (closes the lazy-launch gap: ``@playwright/mcp`` only starts Chromium on
    its first tool call, so without a trigger the first navigation could run
    up to the reconcile loop's poll interval before cookies catch up)."""
    from .cookie_store import read_persisted_values_direct

    key = _fernet_key()
    if key is None:
        return {"targets": len(_CDP_LIST_URLS), "reachable": 0, "injected": 0}
    rows = read_persisted_values_direct()
    if not rows:
        return {"targets": len(_CDP_LIST_URLS), "reachable": 0, "injected": 0}

    reachable, total_injected = 0, 0
    for list_url in _CDP_LIST_URLS:
        ws_url = cdp.cdp_ws_url(list_url, timeout=3.0)
        if not ws_url:
            continue
        reachable += 1
        try:
            injected = _restore_cookies_into(ws_url, rows, key)
            total_injected += injected
            if injected:
                log.info(f"Restore [{list_url}]: injected {injected} persisted cookie(s)")
        except Exception:
            log.warning(f"Cookie restore failed for target {list_url}", exc_info=True)

    return {"targets": len(_CDP_LIST_URLS), "reachable": reachable, "injected": total_injected}


def main():
    parser = argparse.ArgumentParser(description="aw-app-proxy cookie proxy")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--cdp-list-urls", default=None,
                         help="JSON list of CDP /json/list URLs to push cookies into")
    parser.add_argument("--cdp-list-url", default=None,
                         help="Deprecated singular override — one CDP /json/list URL. "
                              "Prefer --cdp-list-urls; accepted for one release.")
    parser.add_argument("--allowed-networks", default=None,
                         help="JSON list of CIDR ranges allowed to use the CONNECT tunnel "
                              "(overrides AW_PROXY_ALLOWED_NETWORKS)")
    args = parser.parse_args()

    global _CDP_LIST_URLS, _ALLOWED_NETWORKS
    if args.cdp_list_urls:
        _CDP_LIST_URLS = json.loads(args.cdp_list_urls)
    elif args.cdp_list_url:
        _CDP_LIST_URLS = [args.cdp_list_url]
    if args.allowed_networks:
        _ALLOWED_NETWORKS = [ipaddress.ip_network(n, strict=False)
                              for n in json.loads(args.allowed_networks)]

    summary = _restore_cookies()
    if summary["injected"]:
        log.info(f"Startup: restored {summary['injected']} cookie(s) across "
                 f"{summary['reachable']}/{summary['targets']} reachable target(s)")

    server = ThreadPoolHTTPServer((args.bind, args.port), ProxyHandler, max_workers=128)
    log.info(f"Proxy listening on {args.bind}:{args.port}, CDP targets {_CDP_LIST_URLS}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
