"""Event reporting: every lane event reaches the server, eventually.

Eventually is the operative word. The lane never waits on the network to make a
decision, so reporting is a queue that drains when it can and holds when it
cannot.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class LaneEvent:
    kind: str
    lane_id: str
    at: float
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventTransport(Protocol):
    """How events get to the server. Stubbed for now; HTTP later."""

    def send(self, events: list[LaneEvent]) -> bool:
        """Return True only if the server accepted every event in the batch."""
        ...


class EventQueue:
    """Bounded, offline-tolerant, oldest-dropped-first.

    Bounded because a lane that has been offline for a week must not run the
    controller out of memory. Dropping is visible -- `dropped` is a counter the
    lane reports on -- rather than silent, because a gap in the event record
    that nobody knows about is worse than one that is measured.
    """

    def __init__(
        self, transport: EventTransport | None = None, *, max_events: int = 10_000
    ) -> None:
        self._queue: deque[LaneEvent] = deque(maxlen=max_events)
        self._transport = transport
        self.dropped = 0

    def record(self, kind: str, lane_id: str, **detail: Any) -> LaneEvent:
        event = LaneEvent(kind=kind, lane_id=lane_id, at=time.time(), detail=detail)
        if self._queue.maxlen is not None and len(self._queue) == self._queue.maxlen:
            self.dropped += 1
        self._queue.append(event)
        return event

    def flush(self) -> int:
        """Try to deliver everything queued. Returns how many were delivered."""
        if self._transport is None or not self._queue:
            return 0
        batch = list(self._queue)
        if not self._transport.send(batch):
            return 0  # keep everything; try again next time
        self._queue.clear()
        return len(batch)

    @property
    def pending(self) -> int:
        return len(self._queue)
