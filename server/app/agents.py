"""
Scout agent presence + command channel.

Scouts running in ``agent`` mode long-poll ``/agent/poll``. The server holds the request open
and returns immediately when there's a command queued for that device (an event wakes the waiter),
otherwise returns nothing after a short timeout and the agent re-polls. That poll is also the
device's heartbeat, so:

- a device shows **online** while its agent keeps polling (no need to send a log first), and
- after a server restart the agent's in-flight poll drops, it re-polls at once, and it's marked
  alive again within seconds.

Commands (e.g. ``{"cmd": "update"}``) are delivered near-instantly because enqueuing sets the
device's event. State is in-memory: transient commands are fine to lose on restart.
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Optional


class AgentHub:
    def __init__(self) -> None:
        self._pending: Dict[str, List[dict]] = {}
        self._events: Dict[str, asyncio.Event] = {}
        self._last_poll: Dict[str, float] = {}

    def _event(self, device_id: str) -> asyncio.Event:
        ev = self._events.get(device_id)
        if ev is None:
            ev = asyncio.Event()
            self._events[device_id] = ev
        return ev

    def queue(self, device_id: str, cmd: dict) -> None:
        self._pending.setdefault(device_id, []).append(cmd)
        self._event(device_id).set()

    def queue_many(self, device_ids: List[str], cmd: dict) -> int:
        for d in device_ids:
            self.queue(d, dict(cmd))
        return len(device_ids)

    async def wait(self, device_id: str, timeout: float = 25.0) -> Optional[dict]:
        """Record the heartbeat and return the next queued command, or None after [timeout]."""
        self._last_poll[device_id] = time.time()
        q = self._pending.get(device_id)
        if q:
            return q.pop(0)
        ev = self._event(device_id)
        ev.clear()
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except asyncio.TimeoutError:
            return None
        q = self._pending.get(device_id)
        return q.pop(0) if q else None

    def online(self, device_id: str, within: float = 45.0) -> bool:
        return time.time() - self._last_poll.get(device_id, 0.0) < within


agents = AgentHub()
