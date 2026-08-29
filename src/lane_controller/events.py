"""Event reporting.

The lane never waits on the network to make a decision, so reporting is a queue
that drains when it can and holds when it cannot.
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

#: How many recent LOG events the read contract's cursor can still serve. A
#: consumer that falls further behind than this is told so -- `reset` -- rather
#: than served a short answer. Same shape and same size as the Vehicle ID
#: service's read history, so one consumer holds one policy for both.
DEFAULT_HISTORY = 256


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
        self,
        transport: EventTransport | None = None,
        *,
        max_events: int = 10_000,
        history: int = DEFAULT_HISTORY,
    ) -> None:
        self._log: deque[LaneEvent] = deque(maxlen=max_events)
        self._sessions: deque[LaneEvent] = deque()
        self._transport = transport
        self.dropped = 0
        # The READ side of the contract, and it is NOT the outbox. `flush()`
        # clears the outbox because those items have been delivered; a consumer
        # reading `GET /v1/lane/events` has not, and clearing what it has not
        # collected yet would make the cursor mean nothing.
        #
        # In memory, bounded, and lost on a restart -- which is the same thing
        # the Vehicle ID service says about its own read store, in the same
        # words: a catch-up window for a consumer that blinked, not a record of
        # anything. This package still has no state store and this does not
        # become one.
        #
        # LOG EVENTS ONLY. A session action is not a log event: it becomes
        # POST /lane/sessions/open or /close, it carries the plate, and the
        # platform is the ledger that holds it. Putting it here would publish
        # the plate to every consumer of a READ contract -- which is a
        # different exposure from the one this window exists for, and one
        # nothing on this contract declares. What happened at the lane is
        # answered by the log events and by `GET /v1/lane/state`.
        self._history: deque[tuple[int, LaneEvent]] = deque(maxlen=history)
        # The cursor counts what this window SERVES, so it stays contiguous and
        # a consumer cannot infer, from a gap in it, that a session action it
        # is not being shown happened.
        self._cursor = 0

    @property
    def _queue(self) -> list[LaneEvent]:
        """Everything pending, in the order it happened."""
        return sorted([*self._sessions, *self._log], key=lambda e: e.at)

    def record(self, kind: str, lane_id: str, **detail: Any) -> LaneEvent:
        event = LaneEvent(kind=kind, lane_id=lane_id, at=time.time(), detail=detail)
        if kind in SESSION_KINDS:
            # Outbox only. Not the read window -- see `_history`.
            self._sessions.append(event)
            return event
        self._cursor += 1
        self._history.append((self._cursor, event))
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

    # --- the read side of the contract ------------------------------------

    @property
    def transport(self) -> EventTransport | None:
        """Where the outbox drains to, or None for a lane with no platform.

        Read by the contract to answer `has_platform`, so that capability is
        the WIRING rather than a flag somebody set. Standalone is a supported
        mode, and a lane in it should not have to declare itself one.
        """
        return self._transport

    @property
    def cursor(self) -> int:
        """Monotonic within one run, and not durable across a restart."""
        return self._cursor

    @property
    def history_depth(self) -> int:
        """How many log events the read window can hold, from the deque itself.

        Published on `GET /v1/lane` so a consumer can size its polling against
        the real bound rather than against a number in a document. Read off
        `maxlen`, so a lane built with a different window says so instead of
        repeating a constant.
        """
        return self._history.maxlen or 0

    @property
    def oldest(self) -> int | None:
        """The lowest cursor still held, or None when the window is empty.

        This is what makes an eviction visible. `since` cannot distinguish
        "nothing followed your cursor" from "everything that followed it has
        been evicted" -- both are an empty list -- so the caller compares
        against this and says `reset`.
        """
        return self._history[0][0] if self._history else None

    def since(self, cursor: int) -> list[tuple[int, LaneEvent]]:
        """Everything still held whose cursor is greater than `cursor`."""
        return [(seq, event) for seq, event in self._history if seq > cursor]
