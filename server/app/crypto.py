"""Reversible encryption for user secrets at rest (e.g. the OpenRouter API key).

Unlike ``security.py`` (one-way argon2 password *hashing*), an API key must be
recovered in plaintext to hand to the provider, so this is symmetric encryption
(Fernet / AES-128-CBC + HMAC).

The Fernet key is derived from ``settings.APP_SECRET`` via HKDF-SHA256. When
``APP_SECRET`` is unset (local dev), we fall back to a key derived from
``JWT_SECRET`` so dev works with no extra config — documented as dev-only in
``config.py`` because rotating that secret would orphan any stored ciphertext.

``decrypt`` never raises into a request path: a tampered/rotated/garbage token
yields ``None``, so callers treat it exactly like "no key stored".
"""
from __future__ import annotations

import base64
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import settings

# A fixed, non-secret context string binds the derived key to this use. It is
# NOT a salt in the secrecy sense — it just domain-separates the KDF output.
_HKDF_INFO = b"ber.user-secret.fernet.v1"


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    """Build the Fernet instance once from the configured master secret."""
    master = (settings.APP_SECRET or settings.JWT_SECRET or "").encode("utf-8")
    # 32 bytes → urlsafe-base64 is exactly the Fernet key format.
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_HKDF_INFO,
    ).derive(master)
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt(plaintext: str) -> str:
    """Encrypt a secret, returning a URL-safe token string for storage."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str | None) -> str | None:
    """Decrypt a stored token back to plaintext, or ``None`` if it can't be
    recovered (missing, tampered, or encrypted under a different secret). Never
    raises — callers treat ``None`` as "no usable secret"."""
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return None
