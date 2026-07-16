"""
Authentication for the companion server.

Two independent mechanisms:

1. **HMAC request signatures** (machine clients, Scouts, print services, the printer app).
   Every request carries ``X-Device-Id``, ``X-Timestamp``, ``X-Nonce`` and ``X-Signature``.
   The signature is ``HMAC-SHA256(device_secret, signing_string)`` over the method, path and a
   SHA-256 of the raw body, so a proxy that logs the URL learns nothing reusable. Requests with
   a stale timestamp (outside ``skew``) or a replayed nonce are rejected.

2. **Session tokens** (the human operator, in the browser). ``login`` verifies the master
   password and mints a short HMAC-signed bearer token; ``verify_session`` checks it. The
   browser keeps the token in localStorage, the master password itself never persists there.
"""

from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from . import crypto
from .db import Database


@dataclass
class HmacResult:
    ok: bool
    device_id: Optional[str] = None
    reason: str = ""


class Auth:
    def __init__(
        self,
        db: Database,
        skew_secs: int = 300,
        session_ttl_secs: int = 12 * 3600,
    ):
        self.db = db
        self.skew = skew_secs
        self.session_ttl = session_ttl_secs

    # ---- master credentials (username + scrypt-hashed password; never stored in the clear) ----
    def is_master(self, password: Optional[str]) -> bool:
        """Password-only check, authorises API printing (temp-password parent)."""
        if not password:
            return False
        stored = self.db.get_config("master_pw_hash")
        return bool(stored) and crypto.verify_password(password, stored)

    def check_login(self, username: Optional[str], password: Optional[str]) -> bool:
        """Full dashboard login: username AND password must both match."""
        if not username or not password:
            return False
        stored_user = self.db.get_config("master_username")
        stored_hash = self.db.get_config("master_pw_hash")
        if not stored_user or not stored_hash:
            return False
        user_ok = hmac.compare_digest(username, stored_user)
        pw_ok = crypto.verify_password(password, stored_hash)
        return user_ok and pw_ok

    def set_credentials(self, username: str, password: str) -> None:
        self.db.set_config("master_username", username)
        self.db.set_config("master_pw_hash", crypto.hash_password(password))

    def set_username(self, username: str) -> None:
        self.db.set_config("master_username", username)

    def set_password(self, password: str) -> None:
        self.db.set_config("master_pw_hash", crypto.hash_password(password))

    # ---- session tokens (browser) ----
    # Stored server-side in the DB (which every worker/restart shares), so login survives
    # restarts and works across workers/instances without depending on any signing key.
    def login(self, username: Optional[str], password: Optional[str]) -> Optional[str]:
        if not self.check_login(username, password):
            return None
        return self._mint_session("admin")

    def mint_session(self, sub: str = "admin") -> str:
        """Mint a session token directly (e.g. after a passkey login)."""
        return self._mint_session(sub)

    def _mint_session(self, sub: str) -> str:
        token = secrets.token_urlsafe(32)
        self.db.create_session(token, sub, time.time() + self.session_ttl)
        return token

    def verify_session(self, token: Optional[str]) -> bool:
        # Only the dashboard/admin session counts as an authenticated operator. Confer participant
        # logins live in the same sessions table tagged ``confer:<id>``, they must NOT pass here,
        # or a chat account could reach admin endpoints.
        return bool(token) and self.db.session_sub(token) == "admin"

    def logout(self, token: Optional[str]) -> None:
        if token:
            self.db.delete_session(token)

    @staticmethod
    def bearer(header: Optional[str]) -> Optional[str]:
        if not header:
            return None
        parts = header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return header.strip()

    # ---- HMAC request signatures (machine clients) ----
    def verify_request(self, method: str, path: str, headers, body: bytes) -> HmacResult:
        device_id = headers.get("x-device-id")
        timestamp = headers.get("x-timestamp")
        nonce = headers.get("x-nonce")
        signature = headers.get("x-signature")
        if not (device_id and timestamp and nonce and signature):
            return HmacResult(False, reason="missing HMAC headers")

        try:
            ts = float(timestamp)
        except (TypeError, ValueError):
            return HmacResult(False, device_id, "bad timestamp")
        if abs(time.time() - ts) > self.skew:
            return HmacResult(False, device_id, "timestamp outside allowed skew")

        secret = self.db.device_secret(device_id)
        if secret is None:
            return HmacResult(False, device_id, "unknown or revoked device")

        if not crypto.verify_signature(
            secret, signature, device_id, timestamp, nonce, method, path, body
        ):
            return HmacResult(False, device_id, "signature mismatch")

        # Signature is checked before the nonce is spent, so a valid caller can't be locked
        # out by someone pre-burning nonces; only a replay of this exact signed request fails.
        if not self.db.use_nonce(nonce, ttl=self.skew * 2):
            return HmacResult(False, device_id, "nonce replay")

        self.db.touch_device(device_id)
        return HmacResult(True, device_id)
