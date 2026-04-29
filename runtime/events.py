"""Tiny in-process pub/sub for daemon → TUI plumbing.

The daemon publishes structured events (ws state, runs, fs sync, errors)
and any number of subscribers (currently: the TUI app) drain them via an
asyncio.Queue. The bus is process-local, optional, and a no-op when
nobody subscribes — daemon code calls `EVENTS.publish(...)` unconditionally.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Event:
    """Generic envelope. `kind` is a dotted namespace, e.g. "ws.connected"."""

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


class EventBus:
    """Multi-subscriber fan-out queue. Drops on full to keep the daemon hot."""

    def __init__(self, *, queue_size: int = 1024) -> None:
        self._queue_size = queue_size
        self._subs: list[asyncio.Queue[Event]] = []
        self._lock = asyncio.Lock()

    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        try:
            self._subs.remove(q)
        except ValueError:
            pass

    def publish(self, kind: str, **payload: Any) -> None:
        if not self._subs:
            return
        ev = Event(kind=kind, payload=dict(payload))
        # Synchronous put_nowait — events are small dicts, drops are fine.
        for q in list(self._subs):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                # Drop the oldest to make space; TUI shows recent state, not history.
                try:
                    q.get_nowait()
                    q.put_nowait(ev)
                except Exception:
                    pass


# Module-level singleton used by daemon publishers.
EVENTS = EventBus()


__all__ = ["Event", "EventBus", "EVENTS"]
