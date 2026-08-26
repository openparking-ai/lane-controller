"""Event reporting: every lane event reaches the server, eventually.

Eventually is the operative word. The lane never waits on the network to make a
decision, so reporting is a queue that drains when it can and holds when it
cannot.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class LaneEvent:
    kind: str
    lane_id: str
    at: float
    detail: dict[str, Any] = field(default_factory=dict)
    # Generated on the lane, unique forever, and the reason a reconnecting lane
    # can safely re-send everything it could not confirm: the platform
    # deduplicates on it. Without this, "flush the queue again" means "bill the
    # customer again".
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventTransport(Protocol):
    """How events get to the server. Stubbed for now; HTTP later."""

    def send(self, events: list[LaneEvent]) -> bool:
        """Return True only if the server accepted every event in the batch."""
        ...


#: Kinds that move money. These are never dropped to make room.
SESSION_KINDS = frozenset({"session_open", "session_close"})


class EventQueue:
    """Bounded for the log, unbounded for the money.

    Bounded because a lane offline for a week must not run the controller out of
    memory. But the original policy -- one queue, oldest dropped first -- drops
    the OLDEST items first, and in a long outage the oldest items are the
    session opens. Losing those means cars that entered have no session, exit to
    a refusal, and park free, while the log entries that would have explained it
    are the ones still in the queue. The cheapest thing to throw away was
    exactly the most expensive.

    So session actions go in their own queue and are never dropped. Only log
    events are, and a drop is counted rather than silent: a gap nobody knows
    about is worse than one that is measured.
    """

    def __init__(
        self, transport: EventTransport | None = None, *, max_events: int = 10_000
    ) -> None:
        self._log: deque[LaneEvent] = deque(maxlen=max_events)
        self._sessions: deque[LaneEvent] = deque()
        self._transport = transport
        self.dropped = 0

    @property
    def _queue(self) -> list[LaneEvent]:
        """Everything pending, in the order it happened."""
        return sorted([*self._sessions, *self._log], key=lambda e: e.at)

    def record(self, kind: str, lane_id: str, **detail: Any) -> LaneEvent:
        event = LaneEvent(kind=kind, lane_id=lane_id, at=time.time(), detail=detail)
        if kind in SESSION_KINDS:
            self._sessions.append(event)
            return event
        if self._log.maxlen is not None and len(self._log) == self._log.maxlen:
            self.dropped += 1
        self._log.append(event)
        return event

    @property
    def pending_sessions(self) -> int:
        return len(self._sessions)

    def flush(self) -> int:
        """Try to deliver everything queued. Returns how many were delivered.

        All or nothing on purpose. A partial flush would need per-item
        bookkeeping to avoid re-sending; re-sending is free here because every
        platform endpoint the transport calls is idempotent, so the simple
        thing is also the correct thing.
        """
        batch = self._queue
        if self._transport is None or not batch:
            return 0
        if not self._transport.send(batch):
            return 0  # keep everything; try again next time
        self._log.clear()
        self._sessions.clear()
        return len(batch)

    @property
    def pending(self) -> int:
        return len(self._log) + len(self._sessions)
