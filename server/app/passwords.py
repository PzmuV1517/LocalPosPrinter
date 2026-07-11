"""
Password + print-history store for the companion server.

- One **master password** (env ACCESS_PASSWORD, legacy fallback ACCESS_CODE) — unlimited use,
  and the credential the admin portal is gated behind.
- Any number of **temporary passwords**, each with a `user` label (who you gave it to) and a
  `max_uses` cap. Each print consumes one use; when exhausted the password is invalid.
- A rolling **print history** (timestamp, format, who, status) for the admin portal.

Temp passwords never reach the POS device — the server validates them and pushes the job to
the device using the master password. State is persisted to small JSON files so it survives a
restart (the admin portal wants history to stick around).
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

_HERE = os.path.dirname(__file__)
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(_HERE), "data"))
_PW_FILE = os.path.join(DATA_DIR, "passwords.json")
_HIST_FILE = os.path.join(DATA_DIR, "history.json")
_HISTORY_CAP = 1000


@dataclass
class TempPassword:
    password: str
    user: str = ""
    max_uses: int = 1
    used: int = 0
    created_at: float = field(default_factory=time.time)
    revoked: bool = False

    @property
    def remaining(self) -> int:
        return max(0, self.max_uses - self.used)

    @property
    def active(self) -> bool:
        return not self.revoked and self.remaining > 0

    def to_public(self) -> dict:
        d = asdict(self)
        d["remaining"] = self.remaining
        d["active"] = self.active
        return d


class Store:
    def __init__(self, master: str):
        self.master = master
        self.temp: Dict[str, TempPassword] = {}
        self.history: List[dict] = []
        self._lock = threading.RLock()
        self._load()

    # ---- validation ----
    def check(self, password: Optional[str]) -> dict:
        """Non-consuming check. Returns valid / unlimited / remaining / user."""
        if password and password == self.master:
            return {"valid": True, "unlimited": True, "remaining": None, "user": "master"}
        tp = self.temp.get(password or "")
        if tp and tp.active:
            return {"valid": True, "unlimited": False, "remaining": tp.remaining, "user": tp.user}
        return {"valid": False, "unlimited": False, "remaining": 0, "user": None}

    def consume(self, password: Optional[str]) -> dict:
        """Validate and, for temp passwords, decrement one use. Same shape as check()."""
        with self._lock:
            if password and password == self.master:
                return {"valid": True, "unlimited": True, "remaining": None, "user": "master"}
            tp = self.temp.get(password or "")
            if tp and tp.active:
                tp.used += 1
                self._save_pw()
                return {"valid": True, "unlimited": False, "remaining": tp.remaining, "user": tp.user}
            return {"valid": False, "unlimited": False, "remaining": 0, "user": None}

    def is_master(self, password: Optional[str]) -> bool:
        return bool(password) and password == self.master

    # ---- temp password management ----
    def create(self, user: str, max_uses: int, password: Optional[str] = None) -> TempPassword:
        with self._lock:
            pw = (password or "").strip() or secrets.token_urlsafe(6)
            tp = TempPassword(password=pw, user=(user or "").strip(), max_uses=max(1, int(max_uses)))
            self.temp[pw] = tp
            self._save_pw()
            return tp

    def revoke(self, password: str) -> bool:
        with self._lock:
            tp = self.temp.get(password)
            if tp is None:
                return False
            tp.revoked = True
            self._save_pw()
            return True

    def list_passwords(self) -> List[dict]:
        # Newest first.
        return [tp.to_public() for tp in sorted(self.temp.values(), key=lambda t: t.created_at, reverse=True)]

    # ---- history ----
    def add_history(self, entry: dict) -> None:
        with self._lock:
            entry = {"timestamp": time.time(), **entry}
            self.history.append(entry)
            if len(self.history) > _HISTORY_CAP:
                self.history = self.history[-_HISTORY_CAP:]
            self._save_hist()

    def list_history(self) -> List[dict]:
        return list(reversed(self.history))  # newest first

    # ---- persistence ----
    def _load(self) -> None:
        try:
            with open(_PW_FILE, "r", encoding="utf-8") as f:
                for d in json.load(f):
                    tp = TempPassword(**d)
                    self.temp[tp.password] = tp
        except (FileNotFoundError, ValueError, TypeError):
            pass
        try:
            with open(_HIST_FILE, "r", encoding="utf-8") as f:
                self.history = json.load(f)
        except (FileNotFoundError, ValueError):
            pass

    def _save_pw(self) -> None:
        self._write(_PW_FILE, [asdict(tp) for tp in self.temp.values()])

    def _save_hist(self) -> None:
        self._write(_HIST_FILE, self.history)

    @staticmethod
    def _write(path: str, data) -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except OSError:
            pass  # best-effort persistence; never fail a print over it
