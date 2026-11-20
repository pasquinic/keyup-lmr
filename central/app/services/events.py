"""In-process pub/sub used to push live updates to dashboard WebSockets."""
from __future__ import annotations

import asyncio
import time


class EventBus:
    def __init__(self):
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, kind: str, **data) -> None:
        evt = {"event": kind, "ts": time.time(), **data}
        for q in list(self._subs):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                pass


bus = EventBus()
