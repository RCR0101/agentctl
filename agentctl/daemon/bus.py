import asyncio
import logging
from collections.abc import Generator
from contextlib import contextmanager

from agentctl.models import SessionEvent

log = logging.getLogger(__name__)


class Bus:
    """In-process fan-out of SessionEvents. Slow subscribers drop events instead of blocking."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue[SessionEvent]] = set()

    def publish(self, ev: SessionEvent) -> None:
        for q in self._subs:
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                log.warning("subscriber queue full, dropping event for %s", ev.session_id)

    @contextmanager
    def subscribe(self) -> Generator[asyncio.Queue[SessionEvent]]:
        q: asyncio.Queue[SessionEvent] = asyncio.Queue(maxsize=1000)
        self._subs.add(q)
        try:
            yield q
        finally:
            self._subs.discard(q)
