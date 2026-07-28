"""Cookie-value at-rest encryption. Ported from the monolith's
``src/api/secrets_crypto.py`` but keyed off ``ctx.secrets`` (the F4
``secrets:own`` facade) instead of a bespoke key file — the framework
already gives every app an encrypted, namespaced KV store; we just use it
to hold ONE Fernet key that in turn encrypts the (many) cookie rows kept in
this app's Postgres table (``cookie_store.py``), the same two-tier shape
the monolith used (its own ``secrets.key`` file -> Fernet -> DB rows).
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

_SECRET_KEY_NAME = "cookie_encryption_key"


def _get_or_create_key(ctx) -> bytes:
    key = ctx.secrets.read(_SECRET_KEY_NAME)
    if not key:
        key = Fernet.generate_key().decode()
        ctx.secrets.write(_SECRET_KEY_NAME, key)
    return key.encode()


def encrypt(ctx, plaintext: str) -> str:
    if not plaintext:
        return ""
    return Fernet(_get_or_create_key(ctx)).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ctx, token: str) -> str:
    if not token:
        return ""
    try:
        return Fernet(_get_or_create_key(ctx)).decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("cookie_encryption_key cannot decrypt this value — "
                          "key was rotated or ciphertext was corrupted") from e


def decrypt_direct(key: bytes, token: str) -> str | None:
    """Standalone decrypt for ``proxy_server.py`` (no ``ctx``) — ``key`` comes
    from ``src.apps.secret_store.SecretStore().get('proxy', 'cookie_encryption_key')``
    read directly (same namespaced file the facade above writes to)."""
    if not token:
        return ""
    try:
        return Fernet(key).decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        return None


def encrypt_direct(key: bytes, plaintext: str) -> str:
    """Standalone encrypt counterpart to :func:`decrypt_direct` — used by
    ``proxy_server.py`` to persist extension-synced cookies without a ``ctx``."""
    if not plaintext:
        return ""
    return Fernet(key).encrypt(plaintext.encode("utf-8")).decode("ascii")


def read_key_direct() -> bytes | None:
    """Read the same Fernet key ``_get_or_create_key`` reads/writes, without a
    ``ctx`` — ``src.apps.secret_store.SecretStore`` is the module the F4
    ``secrets:own`` facade wraps; reading the same namespaced file directly is
    the standalone-process equivalent (mirrors ``cookie_store.py``'s direct
    Postgres access). Returns ``None`` if the app was never activated (no key
    generated yet — nothing to decrypt)."""
    from src.apps.secret_store import SecretStore

    key = SecretStore().get("proxy", _SECRET_KEY_NAME)
    return key.encode() if key else None
