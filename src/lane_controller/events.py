"""Event reporting.

The lane never waits on the network to make a decision, so reporting is a queue
that drains when it can and holds when it cannot.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class Picture:
    """A picture kept with an entry record (U7f): taken between the vend and the
    closing loop, or at the closing loop, and kept against the lane's event id
    of the entry record it belongs to -- a stay's open, a tailgater nobody
    identified, a banned car refused. The platform keeps it one week after the
    car left.

    NEVER a log event and never on the read window: a picture of a car shows
    its plate, and `GET /v1/lane/events` publishes no identity. It goes to the
    platform on its own route, and nowhere else.
    """

    event_id: str
    taken_at: float
    image_bytes: bytes
    # The lane's own id for the picture, so a resend after a timeout is the
    # same picture on the platform.
    picture_ref: str = field(default_factory=lambda: str(uuid.uuid4()))


class EventTransport(Protocol):
    """How events get to the server. Stubbed for now; HTTP later."""

    def send(self, events: list[LaneEvent]) -> bool:
        """Return True only if the server accepted every event in the batch."""
        ...


#: Kinds that move money. These are never dropped to make room.
SESSION_KINDS = frozenset({"session_open", "session_close"})

#: How many LOG events go to the platform in one send, at most. The platform
#: refuses a body over 1 MB with a 413, and an outage's worth of log events
#: used to go in ONE request: about 490 cars of outage was enough for the
#: whole backlog to be refused and dropped. A chunk leaves the queue as soon
#: as the platform has taken it; the transport splits one further when its
#: body is larger than the platform takes (`sync.LOG_CHUNK_BYTES`, and a 413).
LOG_CHUNK = 200

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
        max_pictures: int = 200,
        store=None,
    ) -> None:
        self._log: deque[LaneEvent] = deque(maxlen=max_events)
        # The pictures kept with an entry (U7f), bounded like the log: a lane
        # offline for a day keeps the newest, and a drop is counted.
        self._pictures: deque[Picture] = deque(maxlen=max_pictures)
        self.pictures_dropped = 0
        self._sessions: deque[LaneEvent] = deque()
        self._transport = transport
        # WHERE THE SESSION ACTIONS ARE KEPT ON DISK (`durable.OutboxStore`),
        # or None: memory only, as a standalone lane and the tests have it.
        # Kept as each is recorded and removed once the platform has taken it
        # or refused it, so a restart during an outage -- a power cut, a
        # watchdog, an update -- loses none of the stays it was holding. In
        # memory only, every queued open and close went with the process: the
        # cars it opened left free (their close found no stay) and the stays
        # it closed stayed open for ever. What the store held when the process
        # stopped is queued again here, in the order it happened.
        self._store = store
        if store is not None:
            self._sessions.extend(store.held())
        # TWO THREADS MEET HERE. The lane thread records; the outbox drains on
        # a thread of its own (`LaneRunner`), or on the assisted vend's settle
        # worker. `_state` guards the deques for the microseconds an append
        # or a removal takes -- never across the send, which is the one thing
        # here that waits on a network. `_flushing` serialises drains: two at
        # once would deliver one batch twice (harmless, every endpoint is
        # idempotent) and, worse, race each other's removal.
        self._state = threading.Lock()
        self._flushing = threading.Lock()
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
        with self._state:
            return sorted([*self._sessions, *self._log], key=lambda e: e.at)

    def record(self, kind: str, lane_id: str, **detail: Any) -> LaneEvent:
        event = LaneEvent(kind=kind, lane_id=lane_id, at=time.time(), detail=detail)
        if kind in SESSION_KINDS:
            self._keep(event)
        with self._state:
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
        with self._state:
            return len(self._sessions)

    def flush(self) -> int:
        """Try to deliver everything queued. Returns how many items left the
        queue: delivered, or refused by the platform and dropped (which the
        transport counts and logs).

        PART BY PART, AND A PART LEAVES AS SOON AS IT IS DONE: each session
        action on its own, in the order they happened, then the log events in
        chunks of `LOG_CHUNK` -- the order the transport always called them
        in. The first part the platform cannot be reached for stops the flush
        there; it and everything after it wait for the next try, and the next
        try starts from it. It used to be all or nothing: the whole queue in
        one send, and the first unreachable call kept everything, including
        what the platform had already taken. On a poor link a long backlog
        then never emptied (800 items never got through in 500 tries at 1 %
        of calls lost), and an item already refused and counted was sent and
        counted again on every try. Re-sending stays harmless -- every
        platform endpoint the transport calls is idempotent -- it is just no
        longer the only way through.

        WHAT IS REMOVED IS THE PART, NOT THE QUEUE. The send waits on a
        network, and the lane thread goes on recording while it does: a
        `clear()` afterwards would throw away every event recorded during the
        send -- a car served while the platform was slow, gone from the
        record. Only the events that were sent leave the deques.
        """
        with self._flushing:
            batch = self._queue
            if self._transport is None or not batch:
                return 0
            settled = 0
            for part in _parts(batch):
                if not self._transport.send(part):
                    break  # keep this part and everything after it; try again next time
                self._remove(part)
                settled += len(part)
            return settled

    def _remove(self, part: list[LaneEvent]) -> None:
        """Take one sent part out of its deque, and a session action out of the
        store too. A part is all session actions or all log events."""
        sent = {id(event) for event in part}
        queue = self._sessions if part[0].kind in SESSION_KINDS else self._log
        with self._state:
            kept = [e for e in queue if id(e) not in sent]
            queue.clear()
            queue.extend(kept)
        if self._store is not None and queue is self._sessions:
            try:
                self._store.remove([event.event_id for event in part])
            except Exception:  # noqa: BLE001 -- a disk that fails never costs the record
                # Left on disk, it is sent again after a restart, and the
                # platform deduplicates on its event id.
                log.exception(
                    "a delivered session action could not be removed from the outbox file"
                )

    def _keep(self, event: LaneEvent) -> None:
        """Write a session action to the store, when there is one. A write that
        fails is logged and the action stays queued in memory, as it always was."""
        if self._store is None:
            return
        try:
            self._store.add(event)
        except Exception:  # noqa: BLE001 -- a disk that fails never costs the record
            log.exception(
                "a %s could not be kept in the outbox file; it is queued in memory only",
                event.kind,
            )

    def queue_pictures(self, event_id: str, frames) -> int:
        """Keep these frames as pictures of the entry record `event_id`.

        Only frames that hold a picture are kept; the platform takes a JPEG or
        a PNG and nothing else. Returns how many were queued.
        """
        pictures = [
            Picture(event_id=event_id, taken_at=frame.captured_at, image_bytes=frame.image_bytes)
            for frame in frames
            if frame.image_bytes
        ]
        with self._state:
            for picture in pictures:
                full = self._pictures.maxlen is not None
                if full and len(self._pictures) == self._pictures.maxlen:
                    self.pictures_dropped += 1
                self._pictures.append(picture)
        return len(pictures)

    @property
    def pending_pictures(self) -> int:
        with self._state:
            return len(self._pictures)

    def flush_pictures(self) -> int:
        """Send the queued pictures, all or nothing, as `flush` sends events.

        A transport that cannot carry pictures (a lane with no platform, or one
        written before them) keeps none: there is nowhere for them to go.
        """
        with self._flushing:
            with self._state:
                batch = list(self._pictures)
            send = getattr(self._transport, "send_pictures", None)
            if send is None:
                with self._state:
                    self._pictures.clear()
                return 0
            if not batch or not send(batch):
                return 0
            delivered = {id(p) for p in batch}
            with self._state:
                kept = [p for p in self._pictures if id(p) not in delivered]
                self._pictures.clear()
                self._pictures.extend(kept)
            return len(batch)

    @property
    def pending(self) -> int:
        with self._state:
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
        """Everything still held whose cursor is greater than `cursor`.

        COPIED UNDER THE LOCK `record` TAKES, then filtered. Iterated in place
        while the lane thread recorded, the read raised "deque mutated during
        iteration" -- 358 times in 733,790 reads with one writer -- and the
        consumer of `GET /v1/lane/events` got an error for an ordinary poll.
        """
        with self._state:
            held = list(self._history)
        return [(seq, event) for seq, event in held if seq > cursor]


def _parts(batch: list[LaneEvent]):
    """What one flush sends, part by part: each session action alone, in the
    order they happened -- each is its own platform call -- then the log
    events, in that order, in chunks of `LOG_CHUNK`."""
    for event in batch:
        if event.kind in SESSION_KINDS:
            yield [event]
    plain = [event for event in batch if event.kind not in SESSION_KINDS]
    for start in range(0, len(plain), LOG_CHUNK):
        yield plain[start : start + LOG_CHUNK]
