"""
Cryptographic helpers for the companion server.

Two distinct needs, handled two different ways on purpose:

- **Device HMAC secrets** must be *recoverable* by the server (to recompute a request's
  signature), so they are **encrypted** at rest with Fernet (AES-128-CBC + HMAC). The key
  comes from the ``SERVER_SECRET_KEY`` env var, or an auto-generated ``server.key`` under
  ``DATA_DIR`` (created 0600). Rotating that key invalidates stored device secrets.

- **Passwords** (the temporary limited-use ones) never need to be recovered, we only ever
  verify them, so they are one-way **hashed** with scrypt + a per-password salt.

Both comparisons use ``hmac.compare_digest`` to avoid leaking length/prefix via timing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken

# ---------------------------------------------------------------------------
# Secret encryption (recoverable), Fernet
# ---------------------------------------------------------------------------


def _load_or_create_key(data_dir: str) -> bytes:
    """Return a urlsafe-base64 Fernet key: env override, else a persisted random key."""
    env = os.environ.get("SERVER_SECRET_KEY")
    if env:
        # Accept either a ready Fernet key or any string we can normalise into one.
        try:
            Fernet(env.encode())
            return env.encode()
        except (ValueError, TypeError):
            digest = hashlib.sha256(env.encode()).digest()
            return base64.urlsafe_b64encode(digest)

    key_path = os.path.join(data_dir, "server.key")
    try:
        with open(key_path, "rb") as f:
            return f.read().strip()
    except FileNotFoundError:
        pass

    key = Fernet.generate_key()
    os.makedirs(data_dir, exist_ok=True)
    # O_EXCL so concurrent workers converge on the FIRST writer's key instead of each keeping
    # its own in-memory key (which would make session tokens fail across workers). 0600.
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        with open(key_path, "rb") as f:
            return f.read().strip()
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


class SecretBox:
    """Encrypt/decrypt device secrets so they never sit on disk in the clear."""

    def __init__(self, data_dir: str):
        self._key = _load_or_create_key(data_dir)
        self._fernet = Fernet(self._key)

    def derive(self, label: str) -> str:
        """A deterministic subkey (hex) from the server key, for the temp-password lookup
        hash and the session-token signing key, so everything hangs off one root secret."""
        return hmac.new(self._key, label.encode(), hashlib.sha256).hexdigest()

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str | None:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except (InvalidToken, ValueError, TypeError):
            return None


# ---------------------------------------------------------------------------
# Password hashing (one-way), scrypt
# ---------------------------------------------------------------------------

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


def hash_password(password: str) -> str:
    """Return ``scrypt$<salt_hex>$<hash_hex>`` for a password."""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify against a value produced by :func:`hash_password`."""
    try:
        scheme, salt_hex, hash_hex = stored.split("$", 2)
    except (ValueError, AttributeError):
        return False
    if scheme != "scrypt":
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    dk = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=len(expected)
    )
    return hmac.compare_digest(dk, expected)


# ---------------------------------------------------------------------------
# HMAC request signing
# ---------------------------------------------------------------------------


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def signing_string(
    device_id: str, timestamp: str, nonce: str, method: str, path: str, body: bytes
) -> str:
    """The canonical string both sides HMAC. Keep this in lock-step with every client."""
    return "\n".join([device_id, timestamp, nonce, method.upper(), path, sha256_hex(body)])


def sign(secret: str, device_id: str, timestamp: str, nonce: str, method: str, path: str, body: bytes) -> str:
    msg = signing_string(device_id, timestamp, nonce, method, path, body).encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def verify_signature(
    secret: str,
    provided_sig: str,
    device_id: str,
    timestamp: str,
    nonce: str,
    method: str,
    path: str,
    body: bytes,
) -> bool:
    expected = sign(secret, device_id, timestamp, nonce, method, path, body)
    return hmac.compare_digest(expected, provided_sig or "")


def new_device_secret() -> str:
    """A fresh, high-entropy device secret (shown to the operator exactly once)."""
    return "sph_" + secrets.token_urlsafe(32)
