"""In-process async fan-out bus.

Single-process only. If the backend is ever scaled to multiple workers /
containers, replace the internals with Redis pub/sub (or similar) -- the
`subscribe` / `publish` surface is intentionally small so that swap is
local. Documented in trading/infrastructure/backend/STAGE19_REALTIME.md.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("trading.api.realtime")

# Per-subscriber queue depth. A consumer that falls this far behind is
# dropped (its websocket is closed with 1013); the client reconnects and
# re-syncs over REST. Keeps one slow tab from stalling everyone.
QUEUE_MAXSIZE = 1000


class Subscription:
    __slots__ = ("queue", "_bus", "dropped")

    def __init__(self, bus: "EventBus") -> None:
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._bus = bus
        self.dropped = False

    async def get(self) -> dict[str, Any]:
        return await self.queue.get()

    def close(self) -> None:
        self._bus._remove(self)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class EventBus:
    def __init__(self) -> None:
        self._subs: set[Subscription] = set()
        self._lock = asyncio.Lock()

    def subscribe(self) -> Subscription:
        sub = Subscription(self)
        self._subs.add(sub)
        return sub

    def _remove(self, sub: Subscription) -> None:
        self._subs.discard(sub)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def publish(self, event: dict[str, Any]) -> None:
        """Non-blocking fan-out. Safe to call from a sync request handler
        (it never awaits). A full subscriber queue marks that subscriber
        `dropped` -- the ws writer task notices and closes the socket."""
        if not self._subs:
            return
        for sub in list(self._subs):
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                if not sub.dropped:
                    sub.dropped = True
                    logger.warning("realtime subscriber overflow -- dropping (seq=%s)", event.get("seq"))


bus = EventBus()
