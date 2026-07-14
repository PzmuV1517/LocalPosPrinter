"""
SQLite storage for the companion server / Watchtower.

Everything that used to live in loose JSON files now lives in one indexed database:

- ``devices``        — HMAC clients (Scouts, print services, the printer app). Their signing
                       secret is stored **encrypted** (see crypto.SecretBox), never in the clear.
- ``logs``           — the observability stream every Scout reports into. Indexed by time,
                       severity and device so the dashboard can filter fast.
- ``nonces``         — spent request nonces, for HMAC replay protection. Pruned on write.
- ``temp_passwords`` — limited-use print passwords. Found via a keyed lookup hash and verified
                       with a slow scrypt hash; the plaintext is never stored.
- ``history``        — print history for the admin/Watchtower views.

A one-time best-effort migration pulls in any pre-existing ``passwords.json`` / ``history.json``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from . import crypto

# syslog / journald severities, most severe first. Lower number = worse.
SEVERITIES = ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"]
SEV_NUM = {name: i for i, name in enumerate(SEVERITIES)}


def sev_num(severity: str) -> int:
    return SEV_NUM.get((severity or "").lower(), SEV_NUM["info"])


class Database:
    def __init__(self, data_dir: str, box: crypto.SecretBox, lookup_key: str):
        self.data_dir = data_dir
        self.box = box
        # Deterministic keyed hash used only to *find* a temp password row without storing
        # the plaintext. Verification still goes through slow scrypt on top.
        self._lookup_key = lookup_key.encode()
        os.makedirs(data_dir, exist_ok=True)
        self._path = os.path.join(data_dir, "printhub.db")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        self._migrate_json()

    # ---- schema ----
    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    id            TEXT PRIMARY KEY,
                    name          TEXT DEFAULT '',
                    secret_enc    TEXT NOT NULL,
                    created_at    REAL NOT NULL,
                    last_seen_at  REAL,
                    meta_json     TEXT DEFAULT '{}',
                    heartbeat_secs INTEGER DEFAULT 0,
                    revoked       INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS logs (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id  TEXT,
                    severity   TEXT NOT NULL,
                    sev_num    INTEGER NOT NULL,
                    service    TEXT DEFAULT '',
                    message    TEXT DEFAULT '',
                    meta_json  TEXT DEFAULT '{}',
                    source_ip  TEXT DEFAULT '',
                    printed    INTEGER DEFAULT 0,
                    ts         REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_logs_ts      ON logs(ts);
                CREATE INDEX IF NOT EXISTS idx_logs_sev     ON logs(sev_num);
                CREATE INDEX IF NOT EXISTS idx_logs_device  ON logs(device_id);

                CREATE TABLE IF NOT EXISTS nonces (
                    nonce      TEXT PRIMARY KEY,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_nonces_exp ON nonces(expires_at);

                CREATE TABLE IF NOT EXISTS temp_passwords (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    lookup      TEXT UNIQUE NOT NULL,
                    pw_hash     TEXT NOT NULL,
                    user        TEXT DEFAULT '',
                    max_uses    INTEGER DEFAULT 1,
                    used        INTEGER DEFAULT 0,
                    created_at  REAL NOT NULL,
                    revoked     INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS history (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts         REAL NOT NULL,
                    format     TEXT DEFAULT '',
                    label      TEXT DEFAULT '',
                    user       TEXT DEFAULT '',
                    status     TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS config (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    token      TEXT PRIMARY KEY,
                    sub        TEXT DEFAULT 'admin',
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_exp ON sessions(expires_at);

                CREATE TABLE IF NOT EXISTS webauthn_credentials (
                    credential_id TEXT PRIMARY KEY,   -- base64url
                    public_key    TEXT NOT NULL,      -- base64url
                    sign_count    INTEGER DEFAULT 0,
                    label         TEXT DEFAULT '',
                    transports    TEXT DEFAULT '',
                    created_at    REAL NOT NULL,
                    last_used_at  REAL
                );

                -- ---- Confer (private printer chat) ----
                -- Per-user accounts issued by the server owner; passwords scrypt-hashed.
                CREATE TABLE IF NOT EXISTS confer_users (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    username     TEXT UNIQUE NOT NULL,
                    pw_hash      TEXT NOT NULL,
                    display_name TEXT DEFAULT '',
                    created_at   REAL NOT NULL,
                    revoked      INTEGER DEFAULT 0
                );
                -- Folders form a tree via parent_id; chats live at the root or inside a folder.
                CREATE TABLE IF NOT EXISTS confer_folders (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    name       TEXT NOT NULL,
                    parent_id  INTEGER REFERENCES confer_folders(id) ON DELETE CASCADE,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS confer_chats (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    name       TEXT NOT NULL,
                    folder_id  INTEGER REFERENCES confer_folders(id) ON DELETE CASCADE,
                    created_at REAL NOT NULL
                );
                -- Message bodies are encrypted at rest (SecretBox); 'body_enc' holds ciphertext of
                -- either the text or, for images, the base64 PNG. Retention-pruned like logs.
                CREATE TABLE IF NOT EXISTS confer_messages (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id        INTEGER NOT NULL REFERENCES confer_chats(id) ON DELETE CASCADE,
                    sender         TEXT NOT NULL,          -- confer username, or 'admin'
                    sender_display TEXT DEFAULT '',
                    kind           TEXT DEFAULT 'text',    -- 'text' | 'image'
                    body_enc       TEXT NOT NULL,
                    ts             REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_confer_msg_chat ON confer_messages(chat_id, id);
                CREATE INDEX IF NOT EXISTS idx_confer_msg_ts   ON confer_messages(ts);
                -- Screen-off subscriptions: which chats/folders a user auto-prints when idle.
                CREATE TABLE IF NOT EXISTS confer_subscriptions (
                    user_id     INTEGER NOT NULL REFERENCES confer_users(id) ON DELETE CASCADE,
                    target_type TEXT NOT NULL,             -- 'chat' | 'folder'
                    target_id   INTEGER NOT NULL,
                    PRIMARY KEY(user_id, target_type, target_id)
                );
                -- Per-user, per-chat high-water mark for offline catch-up delivery.
                CREATE TABLE IF NOT EXISTS confer_reads (
                    user_id     INTEGER NOT NULL REFERENCES confer_users(id) ON DELETE CASCADE,
                    chat_id     INTEGER NOT NULL REFERENCES confer_chats(id) ON DELETE CASCADE,
                    last_msg_id INTEGER DEFAULT 0,
                    PRIMARY KEY(user_id, chat_id)
                );
                """
            )
            # Add columns that may be missing on databases created by older versions.
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(devices)")}
            if "heartbeat_secs" not in cols:
                self._conn.execute("ALTER TABLE devices ADD COLUMN heartbeat_secs INTEGER DEFAULT 0")
            self._conn.commit()

    # ---- config (web-driven setup; persists across pulls/updates) ----
    def get_config(self, key: str, default=None):
        with self._lock:
            row = self._conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def get_int(self, key: str, default: int) -> int:
        try:
            return int(self.get_config(key, default))
        except (TypeError, ValueError):
            return default

    def set_config(self, key: str, value) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO config(key, value) VALUES(?, ?)", (key, str(value))
            )
            self._conn.commit()

    def is_configured(self) -> bool:
        # Requires BOTH a username and a password hash, so a lone bootstrap password (or an old
        # password-only config) still triggers the browser setup wizard.
        return bool(self.get_config("master_username")) and bool(self.get_config("master_pw_hash"))

    # ---- sessions (server-side, so login survives restarts + works across workers) ----
    def create_session(self, token: str, sub: str, expires_at: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions(token, sub, created_at, expires_at) VALUES(?,?,?,?)",
                (token, sub, time.time(), expires_at))
            self._conn.commit()

    def session_valid(self, token: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT expires_at FROM sessions WHERE token=?", (token,)).fetchone()
        return bool(row and row["expires_at"] > time.time())

    def session_sub(self, token: str) -> Optional[str]:
        """Return a valid session's subject (e.g. 'admin' or 'confer:<id>'), or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT sub, expires_at FROM sessions WHERE token=?", (token,)).fetchone()
        if row and row["expires_at"] > time.time():
            return row["sub"]
        return None

    def delete_session(self, token: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE token=?", (token,))
            self._conn.commit()

    def prune_sessions(self) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
            self._conn.commit()
            return cur.rowcount

    # ---- WebAuthn passkeys ----
    def add_credential(self, credential_id: str, public_key: str, sign_count: int,
                       label: str, transports: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO webauthn_credentials"
                "(credential_id,public_key,sign_count,label,transports,created_at,last_used_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (credential_id, public_key, sign_count, label, transports, time.time(), None),
            )
            self._conn.commit()

    def get_credential(self, credential_id: str) -> Optional[dict]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM webauthn_credentials WHERE credential_id=?", (credential_id,)).fetchone()
        return dict(r) if r else None

    def list_credentials(self) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT credential_id,label,created_at,last_used_at FROM webauthn_credentials ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def all_credential_ids(self) -> List[str]:
        with self._lock:
            rows = self._conn.execute("SELECT credential_id FROM webauthn_credentials").fetchall()
        return [r["credential_id"] for r in rows]

    def update_credential_sign_count(self, credential_id: str, sign_count: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE webauthn_credentials SET sign_count=?, last_used_at=? WHERE credential_id=?",
                (sign_count, time.time(), credential_id))
            self._conn.commit()

    def delete_credential(self, credential_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM webauthn_credentials WHERE credential_id=?", (credential_id,))
            self._conn.commit()
            return bool(cur.rowcount)

    def _migrate_json(self) -> None:
        """One-time import of the old JSON files, if the DB is empty and they exist."""
        pw_file = os.path.join(self.data_dir, "passwords.json")
        hist_file = os.path.join(self.data_dir, "history.json")
        with self._lock:
            have_pw = self._conn.execute("SELECT COUNT(*) FROM temp_passwords").fetchone()[0]
            have_hist = self._conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
        if not have_pw:
            try:
                with open(pw_file, "r", encoding="utf-8") as f:
                    for d in json.load(f):
                        self.create_temp_password(
                            password=d.get("password", ""),
                            user=d.get("user", ""),
                            max_uses=int(d.get("max_uses", 1)),
                            used=int(d.get("used", 0)),
                            revoked=bool(d.get("revoked", False)),
                            created_at=d.get("created_at"),
                        )
            except (FileNotFoundError, ValueError, TypeError):
                pass
        if not have_hist:
            try:
                with open(hist_file, "r", encoding="utf-8") as f:
                    for e in json.load(f):
                        self.add_history(
                            {k: e.get(k) for k in ("format", "label", "user", "status")},
                            ts=e.get("timestamp"),
                        )
            except (FileNotFoundError, ValueError, TypeError):
                pass

    # ---- devices ----
    def create_device(self, device_id: str, name: str, meta: Optional[dict] = None) -> str:
        """Create/replace a device and return its fresh plaintext secret (shown once)."""
        secret = crypto.new_device_secret()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO devices(id,name,secret_enc,created_at,last_seen_at,meta_json,revoked)"
                " VALUES(?,?,?,?,?,?,0)",
                (
                    device_id,
                    name or "",
                    self.box.encrypt(secret),
                    time.time(),
                    None,
                    json.dumps(meta or {}),
                ),
            )
            self._conn.commit()
        return secret

    def rotate_device_secret(self, device_id: str) -> Optional[str]:
        secret = crypto.new_device_secret()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE devices SET secret_enc=?, revoked=0 WHERE id=?",
                (self.box.encrypt(secret), device_id),
            )
            self._conn.commit()
            return secret if cur.rowcount else None

    def device_secret(self, device_id: str) -> Optional[str]:
        """Decrypt and return a non-revoked device's secret, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT secret_enc, revoked FROM devices WHERE id=?", (device_id,)
            ).fetchone()
        if not row or row["revoked"]:
            return None
        return self.box.decrypt(row["secret_enc"])

    def touch_device(self, device_id: str, meta: Optional[dict] = None) -> None:
        with self._lock:
            if meta:
                self._conn.execute(
                    "UPDATE devices SET last_seen_at=?, meta_json=? WHERE id=?",
                    (time.time(), json.dumps(meta), device_id),
                )
            else:
                self._conn.execute(
                    "UPDATE devices SET last_seen_at=? WHERE id=?", (time.time(), device_id)
                )
            self._conn.commit()

    def set_heartbeat(self, device_id: str, seconds: int) -> bool:
        """Expected reporting interval for the dead-man's-switch. 0 disables it."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE devices SET heartbeat_secs=? WHERE id=?", (max(0, int(seconds)), device_id)
            )
            self._conn.commit()
            return bool(cur.rowcount)

    def revoke_device(self, device_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("UPDATE devices SET revoked=1 WHERE id=?", (device_id,))
            self._conn.commit()
            return bool(cur.rowcount)

    def delete_device(self, device_id: str, require_revoked: bool = True) -> bool:
        """Permanently remove a device. By default only revoked devices can be deleted, so an
        active device can't vanish by accident. Its past logs are left in place for history."""
        with self._lock:
            if require_revoked:
                cur = self._conn.execute("DELETE FROM devices WHERE id=? AND revoked=1", (device_id,))
            else:
                cur = self._conn.execute("DELETE FROM devices WHERE id=?", (device_id,))
            self._conn.commit()
            return bool(cur.rowcount)

    def list_devices(self) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,name,created_at,last_seen_at,meta_json,heartbeat_secs,revoked "
                "FROM devices ORDER BY created_at DESC"
            ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "created_at": r["created_at"],
                    "last_seen_at": r["last_seen_at"],
                    "meta": json.loads(r["meta_json"] or "{}"),
                    "heartbeat_secs": r["heartbeat_secs"] or 0,
                    "revoked": bool(r["revoked"]),
                }
            )
        return out

    # ---- logs ----
    def add_log(
        self,
        device_id: str,
        severity: str,
        message: str,
        service: str = "",
        meta: Optional[dict] = None,
        source_ip: str = "",
        printed: bool = False,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO logs(device_id,severity,sev_num,service,message,meta_json,source_ip,printed,ts)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    device_id,
                    severity,
                    sev_num(severity),
                    service or "",
                    message or "",
                    json.dumps(meta or {}),
                    source_ip or "",
                    1 if printed else 0,
                    time.time(),
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_log(self, log_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM logs WHERE id=?", (log_id,)).fetchone()
        return self._log_row(row) if row else None

    def mark_printed(self, log_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE logs SET printed=1 WHERE id=?", (log_id,))
            self._conn.commit()

    def list_logs(
        self,
        limit: int = 200,
        before_id: Optional[int] = None,
        max_sev: Optional[int] = None,
        device_id: Optional[str] = None,
        service: Optional[str] = None,
        search: Optional[str] = None,
    ) -> List[dict]:
        clauses = []
        params: List[Any] = []
        if before_id:
            clauses.append("id < ?")
            params.append(before_id)
        if max_sev is not None:
            clauses.append("sev_num <= ?")
            params.append(max_sev)
        if device_id:
            clauses.append("device_id = ?")
            params.append(device_id)
        if service:
            clauses.append("service = ?")
            params.append(service)
        if search:
            clauses.append("message LIKE ?")
            params.append(f"%{search}%")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(int(limit), 1000)))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM logs {where} ORDER BY id DESC LIMIT ?", params
            ).fetchall()
        return [self._log_row(r) for r in rows]

    def severity_counts(self, since_secs: float = 86400) -> Dict[str, int]:
        """Per-device highest-severity + counts within a window, for the dashboard cards."""
        cutoff = time.time() - since_secs
        with self._lock:
            rows = self._conn.execute(
                "SELECT device_id, severity, COUNT(*) c FROM logs WHERE ts>=? GROUP BY device_id, severity",
                (cutoff,),
            ).fetchall()
        out: Dict[str, Dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["device_id"] or "?", {})[r["severity"]] = r["c"]
        return out

    @staticmethod
    def _log_row(r: sqlite3.Row) -> dict:
        return {
            "id": r["id"],
            "device_id": r["device_id"],
            "severity": r["severity"],
            "sev_num": r["sev_num"],
            "service": r["service"],
            "message": r["message"],
            "meta": json.loads(r["meta_json"] or "{}"),
            "source_ip": r["source_ip"],
            "printed": bool(r["printed"]),
            "ts": r["ts"],
        }

    def prune_logs(self, retention_days: int, err_retention_days: int = 0) -> int:
        """Delete old logs. Non-error logs (sev_num > 3) go after [retention_days]; errors
        (sev_num <= 3) are kept for [err_retention_days] if that's larger (per-severity retention)."""
        if retention_days <= 0 and err_retention_days <= 0:
            return 0
        now = time.time()
        removed = 0
        with self._lock:
            if retention_days > 0:
                cur = self._conn.execute(
                    "DELETE FROM logs WHERE sev_num > 3 AND ts < ?", (now - retention_days * 86400,))
                removed += cur.rowcount
                # If errors aren't kept longer, prune them on the same schedule.
                if err_retention_days <= 0:
                    cur = self._conn.execute(
                        "DELETE FROM logs WHERE sev_num <= 3 AND ts < ?", (now - retention_days * 86400,))
                    removed += cur.rowcount
            if err_retention_days > 0:
                cur = self._conn.execute(
                    "DELETE FROM logs WHERE sev_num <= 3 AND ts < ?", (now - err_retention_days * 86400,))
                removed += cur.rowcount
            self._conn.commit()
        return removed

    def severity_timeseries(self, hours: int = 24, buckets: int = 48) -> dict:
        """Counts of errors (sev<=3) and everything else per time bucket, for the chart."""
        now = time.time()
        span = hours * 3600
        start = now - span
        width = span / buckets
        errs = [0] * buckets
        other = [0] * buckets
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, sev_num FROM logs WHERE ts >= ?", (start,)).fetchall()
        for r in rows:
            i = int((r["ts"] - start) / width)
            if i < 0 or i >= buckets:
                continue
            (errs if r["sev_num"] <= 3 else other)[i] += 1
        return {"start": start, "width": width, "buckets": buckets, "err": errs, "other": other}

    # ---- nonces (HMAC replay protection) ----
    def use_nonce(self, nonce: str, ttl: float) -> bool:
        """Record a nonce. Returns True if fresh, False if already seen (replay)."""
        now = time.time()
        with self._lock:
            self._conn.execute("DELETE FROM nonces WHERE expires_at < ?", (now,))
            try:
                self._conn.execute(
                    "INSERT INTO nonces(nonce, expires_at) VALUES(?,?)", (nonce, now + ttl)
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    # ---- temp passwords ----
    def _lookup(self, password: str) -> str:
        import hmac as _hmac
        import hashlib as _hashlib

        return _hmac.new(self._lookup_key, password.encode(), _hashlib.sha256).hexdigest()

    def create_temp_password(
        self,
        password: str,
        user: str = "",
        max_uses: int = 1,
        used: int = 0,
        revoked: bool = False,
        created_at: Optional[float] = None,
    ) -> dict:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO temp_passwords(lookup,pw_hash,user,max_uses,used,created_at,revoked)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    self._lookup(password),
                    crypto.hash_password(password),
                    user or "",
                    max(1, int(max_uses)),
                    int(used),
                    created_at or time.time(),
                    1 if revoked else 0,
                ),
            )
            self._conn.commit()
        return {"user": user, "max_uses": max_uses, "used": used}

    def find_temp_password(self, password: str) -> Optional[sqlite3.Row]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM temp_passwords WHERE lookup=?", (self._lookup(password),)
            ).fetchone()
        if row and crypto.verify_password(password, row["pw_hash"]):
            return row
        return None

    def consume_temp_password(self, password: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, max_uses, used, revoked FROM temp_passwords WHERE lookup=?",
                (self._lookup(password),),
            ).fetchone()
            if not row or row["revoked"] or row["used"] >= row["max_uses"]:
                return False
            self._conn.execute(
                "UPDATE temp_passwords SET used = used + 1 WHERE id=?", (row["id"],)
            )
            self._conn.commit()
            return True

    def revoke_temp_password(self, password: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE temp_passwords SET revoked=1 WHERE lookup=?", (self._lookup(password),)
            )
            self._conn.commit()
            return bool(cur.rowcount)

    def list_temp_passwords(self) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT user,max_uses,used,created_at,revoked FROM temp_passwords ORDER BY created_at DESC"
            ).fetchall()
        out = []
        for r in rows:
            remaining = max(0, r["max_uses"] - r["used"])
            out.append(
                {
                    # The plaintext is not recoverable; the list shows metadata only.
                    "user": r["user"],
                    "max_uses": r["max_uses"],
                    "used": r["used"],
                    "remaining": remaining,
                    "created_at": r["created_at"],
                    "revoked": bool(r["revoked"]),
                    "active": (not r["revoked"]) and remaining > 0,
                }
            )
        return out

    # ---- history ----
    def add_history(self, entry: dict, ts: Optional[float] = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO history(ts,format,label,user,status) VALUES(?,?,?,?,?)",
                (
                    ts or time.time(),
                    entry.get("format", ""),
                    entry.get("label", ""),
                    entry.get("user", ""),
                    entry.get("status", ""),
                ),
            )
            self._conn.execute(
                "DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 1000)"
            )
            self._conn.commit()

    def list_history(self, limit: int = 500) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts,format,label,user,status FROM history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "timestamp": r["ts"],
                "format": r["format"],
                "label": r["label"],
                "user": r["user"],
                "status": r["status"],
            }
            for r in rows
        ]

    # ---- Confer: user accounts ----
    def confer_create_user(self, username: str, password: str, display_name: str = "") -> Optional[int]:
        """Create a Confer account. Returns the new id, or None if the username is taken."""
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO confer_users(username,pw_hash,display_name,created_at,revoked)"
                    " VALUES(?,?,?,?,0)",
                    (username, crypto.hash_password(password), display_name or username, time.time()),
                )
                self._conn.commit()
                return cur.lastrowid
            except sqlite3.IntegrityError:
                return None

    def confer_verify_user(self, username: str, password: str) -> Optional[dict]:
        """Return the user row (as dict) if the password matches and the account is active."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM confer_users WHERE username=? AND revoked=0", (username,)
            ).fetchone()
        if row and crypto.verify_password(password, row["pw_hash"]):
            return {"id": row["id"], "username": row["username"], "display_name": row["display_name"]}
        return None

    def confer_get_user(self, user_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id,username,display_name,revoked FROM confer_users WHERE id=?", (user_id,)
            ).fetchone()
        return dict(row) if row else None

    def confer_set_password(self, user_id: int, password: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE confer_users SET pw_hash=? WHERE id=?",
                (crypto.hash_password(password), user_id))
            self._conn.commit()
            return bool(cur.rowcount)

    def confer_revoke_user(self, user_id: int, revoked: bool = True) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE confer_users SET revoked=? WHERE id=?", (1 if revoked else 0, user_id))
            self._conn.commit()
            return bool(cur.rowcount)

    def confer_list_users(self) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,username,display_name,created_at,revoked FROM confer_users ORDER BY username"
            ).fetchall()
        return [
            {"id": r["id"], "username": r["username"], "display_name": r["display_name"],
             "created_at": r["created_at"], "revoked": bool(r["revoked"])}
            for r in rows
        ]

    # ---- Confer: folders & chats (the tree) ----
    def confer_create_folder(self, name: str, parent_id: Optional[int] = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO confer_folders(name,parent_id,created_at) VALUES(?,?,?)",
                (name, parent_id, time.time()))
            self._conn.commit()
            return cur.lastrowid

    def confer_delete_folder(self, folder_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM confer_folders WHERE id=?", (folder_id,))
            self._conn.commit()
            return bool(cur.rowcount)

    def confer_create_chat(self, name: str, folder_id: Optional[int] = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO confer_chats(name,folder_id,created_at) VALUES(?,?,?)",
                (name, folder_id, time.time()))
            self._conn.commit()
            return cur.lastrowid

    def confer_delete_chat(self, chat_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM confer_chats WHERE id=?", (chat_id,))
            self._conn.commit()
            return bool(cur.rowcount)

    def confer_chat_exists(self, chat_id: int) -> bool:
        with self._lock:
            return self._conn.execute(
                "SELECT 1 FROM confer_chats WHERE id=?", (chat_id,)).fetchone() is not None

    def confer_tree(self) -> dict:
        """Return the whole folder/chat tree (shared by all authenticated users)."""
        with self._lock:
            folders = self._conn.execute(
                "SELECT id,name,parent_id,created_at FROM confer_folders ORDER BY name").fetchall()
            chats = self._conn.execute(
                "SELECT id,name,folder_id,created_at FROM confer_chats ORDER BY name").fetchall()
        return {
            "folders": [{"id": r["id"], "name": r["name"], "parent_id": r["parent_id"]} for r in folders],
            "chats": [{"id": r["id"], "name": r["name"], "folder_id": r["folder_id"]} for r in chats],
        }

    def confer_chats_in_folder(self, folder_id: int) -> List[int]:
        """All chat ids under a folder, recursing into subfolders."""
        with self._lock:
            folders = self._conn.execute(
                "SELECT id,parent_id FROM confer_folders").fetchall()
            chats = self._conn.execute("SELECT id,folder_id FROM confer_chats").fetchall()
        children: Dict[Optional[int], List[int]] = {}
        for f in folders:
            children.setdefault(f["parent_id"], []).append(f["id"])
        wanted = set()
        stack = [folder_id]
        while stack:
            fid = stack.pop()
            wanted.add(fid)
            stack.extend(children.get(fid, []))
        return [c["id"] for c in chats if c["folder_id"] in wanted]

    # ---- Confer: messages (encrypted at rest) ----
    def confer_add_message(self, chat_id: int, sender: str, sender_display: str,
                           kind: str, body: str) -> Optional[dict]:
        """Store an encrypted message. Returns the stored row (plaintext body) or None if the
        chat is gone."""
        if not self.confer_chat_exists(chat_id):
            return None
        ts = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO confer_messages(chat_id,sender,sender_display,kind,body_enc,ts)"
                " VALUES(?,?,?,?,?,?)",
                (chat_id, sender, sender_display or sender, kind or "text", self.box.encrypt(body), ts))
            self._conn.commit()
            mid = cur.lastrowid
        return {"id": mid, "chat_id": chat_id, "sender": sender, "sender_display": sender_display or sender,
                "kind": kind or "text", "body": body, "ts": ts}

    def confer_list_messages(self, chat_id: int, limit: int = 200,
                             after_id: Optional[int] = None) -> List[dict]:
        clauses = ["chat_id = ?"]
        params: List[Any] = [chat_id]
        if after_id:
            clauses.append("id > ?")
            params.append(after_id)
        where = "WHERE " + " AND ".join(clauses)
        params.append(max(1, min(int(limit), 1000)))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM confer_messages {where} ORDER BY id DESC LIMIT ?", params).fetchall()
        rows = list(reversed(rows))
        return [self._confer_msg_row(r) for r in rows]

    def confer_messages_since(self, chat_id: int, after_id: int, limit: int = 500) -> List[dict]:
        """Ascending messages in a chat with id > after_id — for offline catch-up."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM confer_messages WHERE chat_id=? AND id>? ORDER BY id ASC LIMIT ?",
                (chat_id, after_id, max(1, min(int(limit), 1000)))).fetchall()
        return [self._confer_msg_row(r) for r in rows]

    def _confer_msg_row(self, r: sqlite3.Row) -> dict:
        return {
            "id": r["id"], "chat_id": r["chat_id"], "sender": r["sender"],
            "sender_display": r["sender_display"], "kind": r["kind"],
            "body": self.box.decrypt(r["body_enc"]) or "", "ts": r["ts"],
        }

    def confer_prune_messages(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM confer_messages WHERE ts < ?", (time.time() - retention_days * 86400,))
            self._conn.commit()
            return cur.rowcount

    # ---- Confer: subscriptions & read state ----
    def confer_set_subscription(self, user_id: int, target_type: str, target_id: int, on: bool) -> None:
        with self._lock:
            if on:
                self._conn.execute(
                    "INSERT OR IGNORE INTO confer_subscriptions(user_id,target_type,target_id)"
                    " VALUES(?,?,?)", (user_id, target_type, target_id))
            else:
                self._conn.execute(
                    "DELETE FROM confer_subscriptions WHERE user_id=? AND target_type=? AND target_id=?",
                    (user_id, target_type, target_id))
            self._conn.commit()

    def confer_list_subscriptions(self, user_id: int) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT target_type,target_id FROM confer_subscriptions WHERE user_id=?",
                (user_id,)).fetchall()
        return [{"type": r["target_type"], "id": r["target_id"]} for r in rows]

    def confer_subscribed_chat_ids(self, user_id: int) -> set:
        """Flatten a user's chat + folder subscriptions into a set of chat ids."""
        chat_ids = set()
        for sub in self.confer_list_subscriptions(user_id):
            if sub["type"] == "chat":
                chat_ids.add(sub["id"])
            elif sub["type"] == "folder":
                chat_ids.update(self.confer_chats_in_folder(sub["id"]))
        return chat_ids

    def confer_get_read(self, user_id: int, chat_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_msg_id FROM confer_reads WHERE user_id=? AND chat_id=?",
                (user_id, chat_id)).fetchone()
        return row["last_msg_id"] if row else 0

    def confer_set_read(self, user_id: int, chat_id: int, last_msg_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO confer_reads(user_id,chat_id,last_msg_id) VALUES(?,?,?)"
                " ON CONFLICT(user_id,chat_id) DO UPDATE SET last_msg_id=MAX(last_msg_id,excluded.last_msg_id)",
                (user_id, chat_id, last_msg_id))
            self._conn.commit()
