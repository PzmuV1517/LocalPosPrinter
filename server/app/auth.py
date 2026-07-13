"""
Authentication for the companion server.

Two independent mechanisms:

1. **HMAC request signatures** (machine clients — Scouts, print services, the printer app).
   Every request carries ``X-Device-Id``, ``X-Timestamp``, ``X-Nonce`` and ``X-Signature``.
   The signature is ``HMAC-SHA256(device_secret, signing_string)`` over the method, path and a
   SHA-256 of the raw body, so a proxy that logs the URL learns nothing reusable. Requests with
   a stale timestamp (outside ``skew``) or a replayed nonce are rejected.

2. **Session tokens** (the human operator, in the browser). ``login`` verifies the master
   password and mints a short HMAC-signed bearer token; ``verify_session`` checks it. The
   browser keeps the token in localStorage — the master password itself never persists there.
"""

from __future__ import annotations

import base64
import hmac
import json
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
        session_key: str,
        skew_secs: int = 300,
        session_ttl_secs: int = 12 * 3600,
    ):
        self.db = db
        self._session_key = session_key.encode()
        self.skew = skew_secs
        self.session_ttl = session_ttl_secs

    # ---- master password (scrypt-hashed in config; never stored in the clear) ----
    def is_master(self, password: Optional[str]) -> bool:
        if not password:
            return False
        stored = self.db.get_config("master_pw_hash")
        return bool(stored) and crypto.verify_password(password, stored)

    def set_master(self, password: str) -> None:
        self.db.set_config("master_pw_hash", crypto.hash_password(password))

    # ---- session tokens (browser) ----
    def login(self, password: Optional[str]) -> Optional[str]:
        if not self.is_master(password):
            return None
        return self._mint_session("admin")

    def _mint_session(self, sub: str) -> str:
        payload = {"sub": sub, "exp": time.time() + self.session_ttl}
        raw = json.dumps(payload, separators=(",", ":")).encode()
        body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        sig = hmac.new(self._session_key, body.encode(), "sha256").hexdigest()
        return f"{body}.{sig}"

    def verify_session(self, token: Optional[str]) -> bool:
        if not token or "." not in token:
            return False
        body, _, sig = token.partition(".")
        expected = hmac.new(self._session_key, body.encode(), "sha256").hexdigest()
        if not hmac.compare_digest(expected, sig):
            return False
        try:
            pad = "=" * (-len(body) % 4)
            payload = json.loads(base64.urlsafe_b64decode(body + pad))
        except (ValueError, TypeError):
            return False
        return float(payload.get("exp", 0)) > time.time()

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

        # Signature is valid *before* we spend the nonce, so a valid caller can't be locked
        # out by someone pre-burning nonces; only a replay of this exact signed request fails.
        if not self.db.use_nonce(nonce, ttl=self.skew * 2):
            return HmacResult(False, device_id, "nonce replay")

        self.db.touch_device(device_id)
        return HmacResult(True, device_id)
