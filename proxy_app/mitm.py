"""TLS-intercepting CONNECT handler for ``proxy_server.py``.

Generates a self-signed root CA on first run (persisted to
``AW_PROXY_MITM_DIR``, default ``/opt/aw-workspace/.aw-workspace/data/proxy``),
then mints a fresh leaf certificate per hostname on demand, signed by that CA
and cached in memory for the life of the process.

Chrome is told to trust this CA two ways: the standard one is an NSS
trust-store install (entrypoint-lite.sh fetches ``GET /mitm-ca.pem`` at boot
and runs it through ``certutil``, now that libnss3-tools ships that tool in
the aw-app-browser image). SPKI pinning (``GET /mitm-ca-spki`` →
``--ignore-certificate-errors-spki-list``) is kept as a parallel fallback for
the brief window before the certutil install finishes, or in case the nssdb
write ever fails — scoped to exactly this one key rather than a blanket
``--ignore-certificate-errors``.

ALPN is pinned to http/1.1 on both legs deliberately: negotiating h2 would
require a much larger frame-relay implementation for no benefit here.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import os
import ssl
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

_DIR = os.environ.get("AW_PROXY_MITM_DIR", "/opt/aw-workspace/.aw-workspace/data/proxy")
_CA_KEY_PATH = os.path.join(_DIR, "mitm_ca.key")
_CA_CRT_PATH = os.path.join(_DIR, "mitm_ca.crt")

_lock = threading.Lock()
_ca_key = None
_ca_cert = None
_leaf_cache: dict[str, tuple[str, str]] = {}  # host -> (cert_pem_path, key_pem_path)
_leaf_dir = None


def _load_or_create_ca():
    global _ca_key, _ca_cert
    os.makedirs(_DIR, exist_ok=True)
    if os.path.exists(_CA_KEY_PATH) and os.path.exists(_CA_CRT_PATH):
        with open(_CA_KEY_PATH, "rb") as f:
            _ca_key = serialization.load_pem_private_key(f.read(), password=None)
        with open(_CA_CRT_PATH, "rb") as f:
            _ca_cert = x509.load_pem_x509_certificate(f.read())
        return
    _ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "aw-app-proxy MITM CA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    _ca_cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(_ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(_ca_key, hashes.SHA256())
    )
    with open(_CA_KEY_PATH, "wb") as f:
        f.write(_ca_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
    with open(_CA_CRT_PATH, "wb") as f:
        f.write(_ca_cert.public_bytes(serialization.Encoding.PEM))


def ca_spki_b64() -> str:
    """Base64 SHA-256 of the CA's SubjectPublicKeyInfo — what Chrome's
    ``--ignore-certificate-errors-spki-list`` flag pins against."""
    with _lock:
        if _ca_cert is None:
            _load_or_create_ca()
        spki = _ca_cert.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return base64.b64encode(hashlib.sha256(spki).digest()).decode()


def ca_cert_pem() -> str:
    """PEM text of the CA certificate itself, for installing into a real
    trust store (NSS via certutil) instead of relying on SPKI pinning."""
    with _lock:
        if _ca_cert is None:
            _load_or_create_ca()
        return _ca_cert.public_bytes(serialization.Encoding.PEM).decode()


def _mint_leaf(host: str):
    """Generate (once, cached) a leaf cert for ``host`` signed by our CA."""
    global _leaf_dir
    if _leaf_dir is None:
        _leaf_dir = os.path.join(_DIR, "leaves")
        os.makedirs(_leaf_dir, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(_ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(_ca_key, hashes.SHA256())
    )
    crt_path = os.path.join(_leaf_dir, f"{host}.crt")
    key_path = os.path.join(_leaf_dir, f"{host}.key")
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
    # Chrome's --ignore-certificate-errors-spki-list matches the pinned SPKI
    # against ANY certificate in the presented chain, not just the leaf — the
    # leaf itself has its own (freshly generated, per-host) key, so the CA
    # cert must ride along in the same file for the pin to ever match.
    with open(crt_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
        f.write(_ca_cert.public_bytes(serialization.Encoding.PEM))
    return crt_path, key_path


def leaf_for(host: str):
    with _lock:
        if _ca_cert is None:
            _load_or_create_ca()
        cached = _leaf_cache.get(host)
        if cached is not None:
            return cached
        paths = _mint_leaf(host)
        _leaf_cache[host] = paths
        return paths


def server_ssl_context(host: str) -> ssl.SSLContext:
    """Server-side TLS context presenting a leaf cert for ``host``, http/1.1 only."""
    crt_path, key_path = leaf_for(host)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=crt_path, keyfile=key_path)
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


def client_ssl_context() -> ssl.SSLContext:
    """Client-side TLS context for the outbound leg to the real origin."""
    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


# ── Header rewrite ───────────────────────────────────────────────────────────
# Chrome's Sec-CH-UA on this container's Debian/Chromium package omits the
# "Google Chrome" brand entirely (only "Chromium" ships there — the brand
# is a build-time flag baked into Google's own binary, absent from the
# open-source package). That is a real, server-visible signal distinguishing
# this browser from any genuine Chrome install, checkable on the very first
# request. Add the brand back on the wire, matching the version already
# reported by the other two brands so the three stay internally consistent.
def rewrite_request_headers(headers: dict) -> dict:
    out = dict(headers)
    ua_ch = out.get("sec-ch-ua")
    if ua_ch and "Google Chrome" not in ua_ch:
        # ua_ch looks like: "Not A(Brand";v="99", "Chromium";v="154"
        chromium_part = None
        for part in ua_ch.split(", "):
            if "Chromium" in part:
                chromium_part = part
                break
        version = "154"
        if chromium_part and 'v="' in chromium_part:
            version = chromium_part.split('v="', 1)[1].split('"', 1)[0]
        out["sec-ch-ua"] = f'{ua_ch}, "Google Chrome";v="{version}"'
    fvl = out.get("sec-ch-ua-full-version-list")
    if fvl and "Google Chrome" not in fvl:
        chromium_part = None
        for part in fvl.split(", "):
            if "Chromium" in part:
                chromium_part = part
                break
        full_version = "154.0.8037.57"
        if chromium_part and 'v="' in chromium_part:
            full_version = chromium_part.split('v="', 1)[1].split('"', 1)[0]
        out["sec-ch-ua-full-version-list"] = f'{fvl}, "Google Chrome";v="{full_version}"'
    return out
