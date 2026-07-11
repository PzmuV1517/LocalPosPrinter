"""
Connected-device tracking + pending-job queue for the /messages WebSocket relay.

Single-device for now, but the structures carry a ``device_id`` so adding real routing
later isn't a rewrite. If no device is connected when a job is submitted, it's queued
in memory and delivered on the next connect rather than dropped.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

from fastapi import WebSocket


@dataclass
class Client:
    ws: WebSocket
    device_id: str = "default"


@dataclass
class Relay:
    clients: List[Client] = field(default_factory=list)
    pending: Dict[str, Deque[dict]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def register(self, ws: WebSocket, device_id: str = "default") -> Client:
        client = Client(ws=ws, device_id=device_id)
        async with self._lock:
            self.clients.append(client)
        # Flush anything queued for this device while it was away.
        await self._flush(device_id)
        return client

    async def unregister(self, client: Client) -> None:
        async with self._lock:
            if client in self.clients:
                self.clients.remove(client)

    def is_connected(self, device_id: str = "default") -> bool:
        return any(c.device_id == device_id for c in self.clients)

    async def submit(self, job: dict, device_id: str = "default") -> bool:
        """Send a job to the target device, or queue it if none is connected.

        Returns True if delivered immediately, False if queued.
        """
        target = next((c for c in self.clients if c.device_id == device_id), None)
        if target is None:
            self.pending.setdefault(device_id, deque()).append(job)
            return False
        try:
            await target.ws.send_text(json.dumps(job))
            return True
        except Exception:
            # Delivery failed mid-flight; drop the socket and queue for next connect.
            await self.unregister(target)
            self.pending.setdefault(device_id, deque()).append(job)
            return False

    async def _flush(self, device_id: str) -> None:
        queue = self.pending.get(device_id)
        if not queue:
            return
        target = next((c for c in self.clients if c.device_id == device_id), None)
        if target is None:
            return
        while queue:
            job = queue[0]
            try:
                await target.ws.send_text(json.dumps(job))
                queue.popleft()
            except Exception:
                break


relay = Relay()
