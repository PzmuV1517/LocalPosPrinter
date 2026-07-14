"""
WebAuthn passkeys — fingerprint / Touch ID / Windows Hello login.

Register a passkey (while signed in with the password) on each device you use — Mac (Touch ID),
laptop (Windows Hello), phone (Chrome fingerprint). Any registered passkey logs you in as the
master user. User verification is required, so a biometric/PIN is always used.

Challenges are kept in memory between begin/complete, keyed by a one-time ``state`` token.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Optional, Tuple

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .db import Database


class Passkeys:
    def __init__(self, db: Database):
        self.db = db
        self._pending: dict[str, tuple] = {}  # state -> (challenge, rp_id, origin, expiry)

    def _remember(self, challenge: bytes, rp_id: str, origin: str) -> str:
        now = time.time()
        for k in [k for k, v in self._pending.items() if v[3] < now]:
            self._pending.pop(k, None)
        state = secrets.token_urlsafe(16)
        self._pending[state] = (challenge, rp_id, origin, now + 300)
        return state

    def _recall(self, state: str):
        v = self._pending.pop(state, None)
        if not v or v[3] < time.time():
            return None
        return v

    # ---- registration (must already be signed in) ----
    def register_begin(self, rp_id: str, origin: str, username: str) -> Tuple[str, str]:
        exclude = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(c)) for c in self.db.all_credential_ids()]
        opts = generate_registration_options(
            rp_id=rp_id,
            rp_name="Watchtower",
            user_name=username,
            user_id=username.encode(),
            user_display_name=username,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=exclude,
        )
        return self._remember(opts.challenge, rp_id, origin), options_to_json(opts)

    def register_complete(self, state: str, credential_json: str, label: str) -> bool:
        v = self._recall(state)
        if not v:
            return False
        challenge, rp_id, origin, _ = v
        ver = verify_registration_response(
            credential=credential_json,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            require_user_verification=True,
        )
        self.db.add_credential(
            credential_id=bytes_to_base64url(ver.credential_id),
            public_key=bytes_to_base64url(ver.credential_public_key),
            sign_count=ver.sign_count,
            label=(label or "passkey")[:60],
            transports="",
        )
        return True

    # ---- authentication (pre-login) ----
    def login_begin(self, rp_id: str, origin: str) -> Optional[Tuple[str, str]]:
        creds = self.db.all_credential_ids()
        if not creds:
            return None
        allow = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(c)) for c in creds]
        opts = generate_authentication_options(
            rp_id=rp_id, allow_credentials=allow, user_verification=UserVerificationRequirement.REQUIRED,
        )
        return self._remember(opts.challenge, rp_id, origin), options_to_json(opts)

    def login_complete(self, state: str, credential_json: str) -> bool:
        v = self._recall(state)
        if not v:
            return False
        challenge, rp_id, origin, _ = v
        try:
            cred_id = json.loads(credential_json).get("id")
        except (ValueError, AttributeError):
            return False
        stored = self.db.get_credential(cred_id) if cred_id else None
        if not stored:
            return False
        ver = verify_authentication_response(
            credential=credential_json,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=base64url_to_bytes(stored["public_key"]),
            credential_current_sign_count=stored["sign_count"],
            require_user_verification=True,
        )
        self.db.update_credential_sign_count(cred_id, ver.new_sign_count)
        return True
