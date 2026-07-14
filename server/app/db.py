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
