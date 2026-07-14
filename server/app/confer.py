"""
Confer — the private, printer-native chat that rides on the Watchtower server.

Design (see the three product decisions):

* **Server-trusted.** Every participant authenticates; traffic is TLS/WSS; message bodies are
  encrypted at rest by the DB layer (``SecretBox``). The server owner (admin) can read and send
  in every chat, and offline devices catch up on reconnect — so this is deliberately *not* E2E.
* **Per-user accounts.** The owner issues ``confer_users`` (scrypt-hashed). A login mints an
  ordinary DB session with ``sub = "confer:<id>"``, so it survives restarts like the dashboard's.
* **Persisted history with retention**, pruned alongside logs.

Transport: participants are heterogeneous. A **printer** carries Confer frames over the same
HMAC ``/messages`` WebSocket it already holds open (after a ``confer_hello``); a **web admin**
connects the dedicated ``/confer/ws``. Both register here as a :class:`ConferConn`, and the hub
fans every message out to all of them. Print-vs-Confer mode is mutually exclusive per printer,
which also keeps two writers off one socket (a printer in Confer mode is not a print target).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .auth import Auth
from .db import Database

CONFER_SUB_PREFIX = "confer:"


class ConferSessions:
    """Confer logins, minted as DB sessions tagged ``confer:<user_id>``."""

    def __init__(self, db: Database, auth: Auth):
        self.db = db
        self.auth = auth

    def login(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        user = self.db.confer_verify_user(username, password)
        if not user:
            return None
        token = self.auth.mint_session(f"{CONFER_SUB_PREFIX}{user['id']}")
        return {"token": token, "user": user}

    def resolve(self, token: Optional[str]) -> Optional[int]:
        """Return the confer user id for a valid confer session token, else None."""
        if not token:
            return None
        sub = self.db.session_sub(token)
        if not sub or not sub.startswith(CONFER_SUB_PREFIX):
            return None
        try:
            uid = int(sub[len(CONFER_SUB_PREFIX):])
        except ValueError:
            return None
        u = self.db.confer_get_user(uid)
        if not u or u["revoked"]:
            return None
        return uid


@dataclass
class ConferConn:
    """One live participant: a printer (user_id set) or the web admin (is_admin)."""
    send: Any                      # async callable: (dict) -> awaitable
    user_id: Optional[int]         # confer user id; None for admin
    username: str                  # confer username, or 'admin'
    display: str
    is_admin: bool = False
    connected_at: float = field(default_factory=time.time)


@dataclass
class ConferHub:
    db: Database
    conns: List[ConferConn] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def register(self, conn: ConferConn) -> None:
        async with self._lock:
            self.conns.append(conn)

    async def unregister(self, conn: ConferConn) -> None:
        async with self._lock:
            if conn in self.conns:
                self.conns.remove(conn)

    def presence(self) -> List[Dict[str, Any]]:
        """Who is currently present in Confer (for the dashboard's 'in Confer mode' badge)."""
        return [
            {"username": c.username, "display": c.display, "admin": c.is_admin,
             "since": c.connected_at}
            for c in self.conns
        ]

    def printer_in_confer_mode(self) -> bool:
        """True if at least one non-admin (a printer) is currently a Confer participant."""
        return any(not c.is_admin for c in self.conns)

    async def _fanout(self, frame: dict, exclude: Optional[ConferConn] = None) -> None:
        for c in list(self.conns):
            if c is exclude:
                continue
            try:
                await c.send(frame)
            except Exception:
                # Drop a dead socket; its own reader loop will also clean up.
                await self.unregister(c)

    # ---- high-level operations ----
    async def post_message(self, chat_id: int, sender: str, sender_display: str,
                           kind: str, body: str, origin: Optional[ConferConn] = None) -> Optional[dict]:
        """Persist a message and fan it out live to every participant. Returns the stored row."""
        stored = self.db.confer_add_message(chat_id, sender, sender_display, kind, body)
        if not stored:
            return None
        frame = {"type": "confer_msg", **stored}
        await self._fanout(frame)
        return stored

    async def deliver_catchup(self, conn: ConferConn) -> None:
        """On (re)connect, replay to a user the messages they missed in subscribed chats,
        so a printer that was off still prints what arrived while it was away."""
        if conn.user_id is None:
            return
        chat_ids = self.db.confer_subscribed_chat_ids(conn.user_id)
        for chat_id in chat_ids:
            last = self.db.confer_get_read(conn.user_id, chat_id)
            missed = self.db.confer_messages_since(chat_id, last)
            if not missed:
                continue
            try:
                await conn.send({"type": "confer_catchup", "chat_id": chat_id, "messages": missed})
            except Exception:
                await self.unregister(conn)
                return
            self.db.confer_set_read(conn.user_id, chat_id, missed[-1]["id"])
