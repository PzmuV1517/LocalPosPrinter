"""
Email notifications (SMTP, stdlib), reach the operator when they're not at the printer.

Config lives in the DB (edited in Settings → Notifications); the SMTP password is stored
encrypted (SecretBox). Sending is synchronous smtplib, so callers run it via asyncio.to_thread.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional

from .crypto import SecretBox
from .db import Database


class Notifier:
    def __init__(self, db: Database, box: SecretBox):
        self.db = db
        self.box = box

    # ---- config helpers ----
    def get_settings(self) -> dict:
        g = self.db.get_config
        return {
            "enabled": g("smtp_enabled", "0") == "1",
            "host": g("smtp_host", "") or "",
            "port": self.db.get_int("smtp_port", 587),
            "security": g("smtp_security", "starttls") or "starttls",  # none | starttls | ssl
            "username": g("smtp_user", "") or "",
            "from_addr": g("smtp_from", "") or "",
            "to_addr": g("smtp_to", "") or "",
            "min_sev": g("notify_min_sev", "crit") or "crit",
            "has_password": bool(g("smtp_pass_enc")),
        }

    def save_settings(self, body: dict) -> None:
        s = self.db.set_config
        if "enabled" in body:
            s("smtp_enabled", "1" if body.get("enabled") else "0")
        for key, cfg in (("host", "smtp_host"), ("security", "smtp_security"),
                         ("username", "smtp_user"), ("from_addr", "smtp_from"),
                         ("to_addr", "smtp_to"), ("min_sev", "notify_min_sev")):
            if body.get(key) is not None:
                s(cfg, str(body[key]))
        if body.get("port") is not None:
            s("smtp_port", int(body["port"]))
        # Only overwrite the password when a new non-empty one is provided.
        if body.get("password"):
            s("smtp_pass_enc", self.box.encrypt(str(body["password"])))

    def _password(self) -> str:
        enc = self.db.get_config("smtp_pass_enc")
        return (self.box.decrypt(enc) or "") if enc else ""

    # ---- send ----
    def send(self, subject: str, body: str, to_addr: Optional[str] = None) -> tuple[bool, str]:
        st = self.get_settings()
        to = to_addr or st["to_addr"]
        if not (st["host"] and st["from_addr"] and to):
            return False, "SMTP not configured (host/from/to)"
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = st["from_addr"]
        msg["To"] = to
        msg.set_content(body)
        try:
            if st["security"] == "ssl":
                server = smtplib.SMTP_SSL(st["host"], st["port"], timeout=15, context=ssl.create_default_context())
            else:
                server = smtplib.SMTP(st["host"], st["port"], timeout=15)
            with server:
                server.ehlo()
                if st["security"] == "starttls":
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                pw = self._password()
                if st["username"] and pw:
                    server.login(st["username"], pw)
                server.send_message(msg)
            return True, "sent"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
