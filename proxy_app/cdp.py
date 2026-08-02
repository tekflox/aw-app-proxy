"""Raw-socket Chrome DevTools Protocol client — no ``websocket-client`` dep,
ported verbatim (minus class wrapping) from the monolith's
``tools/browser/proxy.py`` / ``src/api/routes/proxy_cookies.py``. Shared by
``routes.py`` (in-process cookie-keys/persist/clear endpoints) and
``proxy_server.py`` (the standalone CONNECT-proxy service subprocess).
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import urllib.request
from urllib.parse import urlparse


def cdp_list_url_default() -> str:
    # aw-app-browser is a separate Tier-2 (podman) container reachable by
    # its own name on the shared workspace network — 127.0.0.1 here would
    # only ever be this app's own loopback, not the browser's (reconciled
    # 2026-08-02; see aw-app-browser's aw-app.json dependency note).
    return "http://aw-app-browser:9223/json/list"


def cdp_ws_url(list_url: str, timeout: float = 3.0) -> str | None:
    """Fetch the CDP ``/json/list`` endpoint and return the first page's
    ``webSocketDebuggerUrl``, or ``None`` if the browser isn't reachable."""
    try:
        with urllib.request.urlopen(list_url, timeout=timeout) as resp:
            pages = json.loads(resp.read())
    except Exception:
        return None
    for page in pages:
        if page.get("type") == "page":
            return page.get("webSocketDebuggerUrl")
    return None


def open_ws(ws_url: str, timeout: float = 10.0) -> socket.socket:
    parsed = urlparse(ws_url)
    sock = socket.create_connection((parsed.hostname, parsed.port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    handshake = (
        f"GET {parsed.path} HTTP/1.1\r\n"
        f"Host: {parsed.hostname}:{parsed.port}\r\n"
        f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(handshake.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += sock.recv(4096)
    return sock


def send_recv(sock: socket.socket, msg_id: int, method: str, params: dict) -> dict | None:
    msg = json.dumps({"id": msg_id, "method": method, "params": params}).encode()
    frame = bytearray([0x81])
    mask = os.urandom(4)
    n = len(msg)
    if n < 126:
        frame.append(0x80 | n)
    elif n < 65536:
        frame.append(0x80 | 126)
        frame.extend(struct.pack(">H", n))
    else:
        frame.append(0x80 | 127)
        frame.extend(struct.pack(">Q", n))
    frame.extend(mask)
    frame.extend(bytes(b ^ mask[i % 4] for i, b in enumerate(msg)))
    sock.sendall(frame)

    try:
        header = sock.recv(2)
        payload_len = header[1] & 0x7F
        if payload_len == 126:
            payload_len = struct.unpack(">H", sock.recv(2))[0]
        elif payload_len == 127:
            payload_len = struct.unpack(">Q", sock.recv(8))[0]
        rdata = b""
        while len(rdata) < payload_len:
            rdata += sock.recv(payload_len - len(rdata))
        return json.loads(rdata.decode())
    except Exception:
        return None
