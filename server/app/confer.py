"""Confer: private chat that rides on the Watchtower server.

Server-trusted, not E2E: participants authenticate, bodies are encrypted at rest, the admin can
read and send everywhere, offline devices catch up on reconnect. Logins are DB sessions tagged
confer:<id>. History persists and prunes with the logs.

Printers connect /confer/ws with a participant token, the web admin with a session token. Both
register as a ConferConn and receive every message. A printer in Confer mode is not a print
target (mode is announced separately on the print socket).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .auth import Auth
from .db import Database

CONFER_SUB_PREFIX = "confer:"


class ConferSessions:
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
    send: Any                      # async (dict) -> awaitable
    user_id: Optional[int]         # confer user id, None for admin
    username: str
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
        return [{"username": c.username, "display": c.display, "admin": c.is_admin,
                 "since": c.connected_at} for c in self.conns]

    async def _fanout(self, frame: dict) -> None:
        for c in list(self.conns):
            try:
                await c.send(frame)
            except Exception:
                await self.unregister(c)

    async def post_message(self, chat_id: int, sender: str, sender_display: str,
                           kind: str, body: str) -> Optional[dict]:
        stored = self.db.confer_add_message(chat_id, sender, sender_display, kind, body)
        if not stored:
            return None
        await self._fanout({"type": "confer_msg", **stored})
        return stored

    async def deliver_catchup(self, conn: ConferConn) -> None:
        """Replay missed messages in subscribed chats so a printer that was off still prints them."""
        if conn.user_id is None:
            return
        for chat_id in self.db.confer_subscribed_chat_ids(conn.user_id):
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
