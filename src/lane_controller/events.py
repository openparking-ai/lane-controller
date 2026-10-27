"""Event reporting.

The lane never waits on the network to make a decision, so reporting is a queue
that drains when it can and holds when it cannot.

AND IT NEVER WAITS ON A DISK EITHER. With an outbox file, every stay open and
close, and every picture kept with an entry, is written to disk -- by a thread
of its own (`DiskWriter`), never by the thread that records it. That thread is
the lane's: the open of one car is recorded after its crossing, and the next
car is decided on the same thread, so a write there was the next car waiting
on the disk -- up to SQLite's five-second lock timeout on a busy file, and for
as long as the disk hung on a failing one. What the lane thread does now is
append to memory and hand the write over; what the outbox delivers is read
from memory, so a hung disk costs the restart copy, never the delivery.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)

#: How long a DRAIN waits for the disk thread to catch up before it sends the
#: pictures (which it sends from the disk) and before `serve` makes its last
#: send. Never waited on the lane thread: a drain runs on its own thread under
#: `LaneRunner`, and inline only on a bare controller (the tests, the demo).
DISK_WAIT_S = 5.0

#: How many pictures one flush reads from the disk at a time. A picture is up
#: to a few megabytes, and an outage's worth of them must never be read into
#: memory at once.
PICTURE_BATCH = 8


class DiskWriter:
    """One thread that does the outbox file's writes, in the order handed in.

    `submit` appends a job and returns at once -- it waits on nothing a disk
    can hold -- and the thread runs each job in turn, so an open's write always
    lands before the removal of the same open once it is delivered. A job that
    raises is logged and the next one runs: a disk that fails costs the restart
    copy of one item, never the thread. `wait_idle` is how a drain, or `serve`
    on its way out, waits until everything handed in has been tried.

    THE WINDOW THIS LEAVES, STATED: an item is on disk once this thread has
    written it -- milliseconds after it was recorded on a healthy disk -- and a
    process killed inside those milliseconds loses it. It is the same class of
    window as the one between the vend and the record of the stay, which the
    lane already states (docs/CONTRACT.md, "What is NOT here"), and it is what
    keeping the disk off the barrier's thread costs.
    """

    def __init__(self, name: str = "lane-outbox-disk") -> None:
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()
        self._idle = threading.Condition()
        self._pending = 0
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def submit(self, job: Callable[[], None]) -> None:
        with self._idle:
            self._pending += 1
        self._jobs.put(job)

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                job()
            except Exception:  # noqa: BLE001 -- one failed write never stops the next
                log.exception("the outbox file could not be written; the item stays in memory")
            finally:
                with self._idle:
                    self._pending -= 1
                    if self._pending == 0:
                        self._idle.notify_all()

    @property
    def pending(self) -> int:
        with self._idle:
            return self._pending

    def wait_idle(self, timeout: float) -> bool:
        """Wait, at most `timeout`, until every job handed in has been run."""
        with self._idle:
            return self._idle.wait_for(lambda: self._pending == 0, timeout)

    def call(self, fn: Callable[[], Any], timeout: float) -> tuple[bool, Any]:
        """Run `fn` on this thread, after everything handed in before it, and
        wait at most `timeout` for its answer: `(True, result)`, or `(False,
        None)` when the disk did not answer in time and the caller goes on
        without it. What `fn` raises is raised here."""
        answered = threading.Event()
        box: dict[str, Any] = {}

        def job() -> None:
            try:
                box["result"] = fn()
            except Exception as err:  # noqa: BLE001 -- handed back to the caller
                box["error"] = err
            finally:
                answered.set()

        self.submit(job)
        if not answered.wait(timeout):
            return False, None
        if "error" in box:
            raise box["error"]
        return True, box.get("result")


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
    """A picture kept with an entry record (U7f): the arming read's frames,
    taken between the vend and the closing loop, or at the closing loop, and
    kept against the lane's event id of the entry record it belongs to -- a
    stay's open, a crossing the camera said no car made, a banned car refused.
    The platform keeps it one week after the car left.

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
        # The pictures kept with an entry (U7f). WITHOUT a store: in memory,
        # bounded like the log -- a lane offline for a day keeps the newest,
        # and a drop is counted. WITH one: on disk beside the stays they belong
        # to, not bounded and not dropped, and in memory only until the disk
        # thread has written them (`_unwritten`); a picture whose write failed
        # waits there, bounded the same way, and is sent from there.
        self._pictures: deque[Picture] = deque(maxlen=max_pictures)
        self._max_pictures = max_pictures
        self._unwritten: dict[str, Picture] = {}
        #: Pictures the platform has taken whose removal from the file has not
        #: been confirmed: never sent again, and their removal retried, so a
        #: delete that fails neither repeats a send nor stops the flush.
        self._sent_unremoved: set[str] = set()
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
        #
        # WRITTEN BY `_disk`, NEVER BY THE THREAD THAT RECORDS. See the module
        # docstring: the recording thread is the lane's.
        self._store = store
        self._disk = DiskWriter() if store is not None else None
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
            ids = [event.event_id for event in part]
            # On the disk thread, after the write of the same action: one queue,
            # in order, so a removal can never land before the add it undoes.
            self._disk.submit(lambda: self._store_remove(ids))

    def _store_remove(self, ids: list[str]) -> None:
        try:
            self._store.remove(ids)
        except Exception:  # noqa: BLE001 -- a disk that fails never costs the record
            # Left on disk, it is sent again after a restart, and the
            # platform deduplicates on its event id.
            log.exception("a delivered session action could not be removed from the outbox file")

    def _keep(self, event: LaneEvent) -> None:
        """Hand a session action to the disk thread, when there is a store, and
        return at once: the caller is the lane thread. A write that fails is
        logged and the action stays queued in memory, as it always was."""
        if self._store is None:
            return
        self._disk.submit(lambda: self._store_add(event))

    def _store_add(self, event: LaneEvent) -> None:
        try:
            self._store.add(event)
        except Exception:  # noqa: BLE001 -- a disk that fails never costs the record
            log.exception(
                "a %s could not be kept in the outbox file; it is queued in memory only",
                event.kind,
            )

    def wait_written(self, timeout: float = DISK_WAIT_S) -> bool:
        """Wait, at most `timeout`, for the disk thread to write what it was
        handed. True at once with no store. For a drain and for `serve` on its
        way out -- never for the lane thread."""
        return self._disk is None or self._disk.wait_idle(timeout)

    def queue_pictures(self, event_id: str, frames) -> int:
        """Keep these frames as pictures of the entry record `event_id`.

        Only frames that hold a picture are kept; the platform takes a JPEG or
        a PNG and nothing else. Returns how many were queued.

        With a store, each is held in memory until the disk thread has written
        it, and handed to that thread -- and this returns at once, whatever
        the disk is doing.
        """
        pictures = [
            Picture(event_id=event_id, taken_at=frame.captured_at, image_bytes=frame.image_bytes)
            for frame in frames
            if frame.image_bytes
        ]
        if self._store is None:
            with self._state:
                for picture in pictures:
                    full = self._pictures.maxlen is not None
                    if full and len(self._pictures) == self._pictures.maxlen:
                        self.pictures_dropped += 1
                    self._pictures.append(picture)
            return len(pictures)
        with self._state:
            for picture in pictures:
                self._hold_unwritten(picture)
        for picture in pictures:
            # The job carries the REF, not the picture: what memory holds for a
            # disk that is behind is `_unwritten`, and that is bounded.
            self._disk.submit(lambda ref=picture.picture_ref: self._write_picture(ref))
        return len(pictures)

    def _hold_unwritten(self, picture: Picture) -> None:
        """Into memory until the disk has it -- bounded at `max_pictures`, so a
        disk that has stopped taking writes cannot take the lane's memory with
        it: past that, the oldest picture not on disk is dropped and counted.
        A disk that keeps up never comes near it: a car's pictures are written
        before the next car's are taken. Under `_state`."""
        while len(self._unwritten) >= self._max_pictures:
            self._unwritten.pop(next(iter(self._unwritten)))
            self.pictures_dropped += 1
        self._unwritten[picture.picture_ref] = picture

    def _write_picture(self, ref: str) -> None:
        """On the disk thread. Written, the picture leaves memory; not written,
        it stays there and the drain sends it from there. A picture already
        sent from memory, or dropped for room, is not written."""
        with self._state:
            picture = self._unwritten.get(ref)
        if picture is None:
            return
        try:
            dropped = self._store.add_picture(picture)
        except Exception:  # noqa: BLE001 -- a disk that fails never costs the picture
            log.exception("a picture could not be kept in the outbox file; it waits in memory")
            return
        with self._state:
            self._unwritten.pop(ref, None)
            # THE CEILING: the oldest pictures the file let go to keep room for
            # the stays, counted -- a gap nobody knows about is worse.
            self.pictures_dropped += dropped
        if dropped:
            log.warning(
                "the outbox file's pictures reached their ceiling; %d of the oldest dropped",
                dropped,
            )

    def _remove_pictures(self, refs: list[str]) -> None:
        """On the disk thread: pictures the platform has taken or refused. A
        removal that fails leaves them in `_sent_unremoved`: not sent again, and
        their removal tried again by the next flush."""
        try:
            self._store.remove_pictures(refs)
        except Exception:  # noqa: BLE001 -- left on disk, never sent again
            log.exception("a delivered picture could not be removed from the outbox file")
            return
        with self._state:
            self._sent_unremoved.difference_update(refs)

    @property
    def pending_pictures(self) -> int:
        """Every picture not yet sent: in memory and -- with a store -- on
        disk, read on the disk thread, waiting at most `DISK_WAIT_S`. For a
        drain, a test or an operator, never for the lane thread."""
        with self._state:
            in_memory = len(self._pictures) + len(self._unwritten)
        if self._store is None:
            return in_memory
        answered, on_disk = self._disk.call(self._store.picture_count, DISK_WAIT_S)
        return in_memory + (on_disk if answered else 0)

    def flush_pictures(self) -> int:
        """Send the kept pictures, ONE AT A TIME, oldest first. Returns how
        many left: taken by the platform, or refused by it (counted and logged
        by the transport, and not sent again).

        A PICTURE LEAVES AS SOON AS IT IS DONE, and the first one the platform
        cannot be reached for stops the flush -- it and everything after it wait
        for the next try. It used to be all or nothing: every picture in one
        go, kept whole on the first failure, so on a poor link a backlog never
        emptied and pictures already taken were sent again on every try.

        With a store the pictures are read from the disk a few at a time, and
        removed from it as each is done -- both ON THE DISK THREAD, after
        everything handed to it before, and waited for at most `DISK_WAIT_S`:
        a disk that does not answer costs this flush its disk part, never the
        drain thread, which the stays' delivery runs on too. Then the pictures
        not on disk -- the disk could not take them, or has not yet -- go from
        memory. A transport that cannot carry pictures (a lane with no
        platform, or one written before them) keeps none: there is nowhere for
        them to go.
        """
        with self._flushing:
            send = getattr(self._transport, "send_pictures", None)
            if send is None:
                with self._state:
                    self._pictures.clear()
                    self._unwritten.clear()
                return 0
            if self._store is None:
                return self._flush_memory_pictures(send)
            sent, stopped = self._flush_disk_pictures(send)
            if stopped:
                return sent
            with self._state:
                waiting = list(self._unwritten.values())
            for picture in waiting:
                if not send([picture]):
                    return sent
                with self._state:
                    self._unwritten.pop(picture.picture_ref, None)
                sent += 1
            return sent

    def _flush_disk_pictures(self, send) -> tuple[int, bool]:
        """The pictures on disk, oldest first, a page at a time BY POSITION,
        each removed once done. Returns (how many left, whether the platform
        stopped it).

        By position, and not "the oldest eight": a picture whose removal
        failed is still the oldest row, and reading the oldest eight again
        handed back the same eight for ever -- the flush stopped there, and
        every picture behind them waited for good. Now a page starts after the
        last row read, and a picture already taken (`_sent_unremoved`) is not
        sent again: only its removal is tried again."""
        sent = 0
        after = 0
        while True:
            try:
                answered, held = self._disk.call(
                    lambda position=after: self._store.held_pictures(PICTURE_BATCH, after=position),
                    DISK_WAIT_S,
                )
            except Exception:  # noqa: BLE001 -- a disk that fails never costs the drain
                log.exception("the pictures in the outbox file could not be read")
                return sent, False
            if not answered:
                log.warning(
                    "the outbox file did not answer in %.1fs; its pictures wait", DISK_WAIT_S
                )
                return sent, False
            fresh = [(position, picture) for position, picture in held if position > after]
            if not fresh:
                return sent, False
            for position, picture in fresh:
                after = position
                ref = picture.picture_ref
                with self._state:
                    taken = ref in self._sent_unremoved
                if not taken:
                    if not send([picture]):
                        return sent, True
                    with self._state:
                        self._sent_unremoved.add(ref)
                    sent += 1
                # Removed on the disk thread, ahead of the next read there.
                self._disk.submit(lambda ref=ref: self._remove_pictures([ref]))

    def _flush_memory_pictures(self, send) -> int:
        """The memory-only lane's pictures, one at a time, as above."""
        with self._state:
            batch = list(self._pictures)
        sent = 0
        for picture in batch:
            if not send([picture]):
                break
            with self._state:
                kept = [p for p in self._pictures if p is not picture]
                self._pictures.clear()
                self._pictures.extend(kept)
            sent += 1
        return sent

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
