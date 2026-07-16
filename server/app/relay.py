"""Connected-printer tracking and pending-job queue for the /messages relay.

A job submitted with no printer connected is queued in memory and delivered on next connect.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from fastapi import WebSocket


@dataclass
class Client:
    ws: WebSocket
    device_id: str = "default"
    # A printer in Confer mode holds the same socket but must NOT receive print jobs (they'd
    # interleave with chat traffic). While confer is True it's not a print target, so jobs queue
    # and flush when it returns to Print mode.
    confer: bool = False


@dataclass
class Relay:
    clients: List[Client] = field(default_factory=list)
    # Each pending entry is (job, on_delivered) so the delivery hook survives queueing.
    pending: Dict[str, Deque[Tuple[dict, Optional[Callable[[], Any]]]]] = field(default_factory=dict)
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
        """A print target is available: a client for this device that is NOT in Confer mode."""
        return any(c.device_id == device_id and not c.confer for c in self.clients)

    def any_confer(self) -> bool:
        """True if any connected printer has announced it is in Confer mode (for the status badge)."""
        return any(c.confer for c in self.clients)

    async def set_confer_mode(self, client: Client, on: bool) -> None:
        """Toggle a connected printer between Confer and Print mode. Returning to Print flushes
        anything that queued while it was chatting."""
        client.confer = on
        if not on:
            await self._flush(client.device_id)

    async def close_all(self, code: int = 1012) -> None:
        """Close every socket (1012 = service restart) so printers reconnect at once."""
        for client in list(self.clients):
            try:
                await client.ws.close(code=code)
            except Exception:
                pass
        self.clients.clear()

    async def submit(self, job: dict, device_id: str = "default", on_delivered=None) -> bool:
        """Send to the printer, or queue if none is connected. Returns True if delivered now.

        on_delivered fires once, only when the job actually reaches a printer (here or from
        _flush), never on queue, so a temp-password use is spent only on a real print.
        """
        target = next((c for c in self.clients if c.device_id == device_id and not c.confer), None)
        if target is None:
            self.pending.setdefault(device_id, deque()).append((job, on_delivered))
            return False
        try:
            await target.ws.send_text(json.dumps(job))
            self._fire(on_delivered)
            return True
        except Exception:
            # Delivery failed mid-flight; drop the socket and queue for next connect.
            await self.unregister(target)
            self.pending.setdefault(device_id, deque()).append((job, on_delivered))
            return False

    async def _flush(self, device_id: str) -> None:
        queue = self.pending.get(device_id)
        if not queue:
            return
        target = next((c for c in self.clients if c.device_id == device_id and not c.confer), None)
        if target is None:
            return
        while queue:
            job, on_delivered = queue[0]
            try:
                await target.ws.send_text(json.dumps(job))
                queue.popleft()
                self._fire(on_delivered)
            except Exception:
                break

    @staticmethod
    def _fire(on_delivered) -> None:
        if on_delivered is None:
            return
        try:
            on_delivered()
        except Exception:
            pass


relay = Relay()
