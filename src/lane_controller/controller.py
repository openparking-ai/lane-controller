"""The lane sequence.

    arming loops -> grab frames -> identify -> decide -> vend -> closing loops

The barrier still closes itself on its own closing loop; the controller has no
part in that and no way to interfere with it. The closing loops at the end of
the line above are READ and never driven, and they are what says whether a
vehicle actually went through: either the barrier's own closing loop, read
through its detector's second output (the standard installation), or a pair of
confirmation loops after the gate.

THE TICKET IS NOT THE ENTRY. A vend creates a PENDING entry. The closing loop
reading a vehicle inside the confirmation window promotes it to a session; at a
lane with two loops, so does A then B, and B then A is somebody backing out and
closes it with its own reason. The window elapsing with nothing is a third
answer -- HELD -- and it is neither of the other two: voiding it silently
re-creates the abandoned-ticket fraud, and turning it into a session is the
phantom occupant that fills a garage on paper before it fills in concrete. One
loop cannot see a car that reaches it and then backs out; that car's stay has
no exit, as any open stay, and `closing_loops: 1` is on every event that says so.

A lane with no closing loops installed is not refused. It opens its sessions as
it always did, and every one of them carries `unconfirmable` and an
`entry_unconfirmable` event saying nothing could confirm it -- which is the
whole difference between a weakness that is configured and a weakness that is
recorded.

A CROSSING WITH NOTHING PENDING IS RECORDED TOO. The three answers above are
what becomes of a vend, and until `observe_closing_loops` existed the loops
were read only after one -- so a car the lane refused that drove in anyway, a
car following the one admitted, and every car through a barrier that is simply
up crossed loops nobody was reading and were in no record at all. Now the loops
are polled on every idle turn of `run_once` while nothing is pending, and a
crossing found that way that would have confirmed a vend -- forward over two
loops, an occupancy of one -- is an `entry_unadmitted` event: an entry, because a
vehicle is inside; a different one, because nothing admitted it. It moves no
transit state, and it is never folded into `entry_confirmed`. A stream of them
is what a broken boom looks like from here.

AND EVERY SUCH CAR IS A STAY. The owner's rule (2026-10-26): the activation
loop, the closing loop and the pictures make the entry, and the barrier plays
no part in the record. So the crossing with no open command opens a stay like
a confirmed entry's -- `confirmed`, because the loops saw it; `tailgater:
true`, because no open command let it in -- with its closing-loop pictures and
the identity read from them, a plate or "unreadable". A held vend is still no
stay: that car did not cross. A lane with no closing loop is unchanged: every
stay `unconfirmable`.

The whole sequence runs against simulated implementations of all three seams,
which is why `tests/` needs no hardware.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from .config import LaneConfig
from .contract import TransitState
from .decision import (
    Decision,
    DecisionCache,
    Fallback,
    Outcome,
    closed_to,
    decide,
    plate_read,
)
from .events import EventQueue, LaneEvent
from .exit_pricing import price_exit
from .interfaces import (
    BannedMatcher,
    CameraFeed,
    ClosingLoops,
    ClosingSequence,
    LoopInput,
    VehicleIdentifier,
    VehicleIdentity,
    VendOutput,
)
from .reader import ReaderScreen
from .sync import (
    ARMED,
    ARMING_INCOMPLETE,
    ARMING_SUPPRESSED,
    ARMING_SUPPRESSION_ENDED,
    CONFIRMED,
    ENTRY_BACKED_OUT,
    ENTRY_CONFIRMED,
    ENTRY_HELD,
    ENTRY_PENDING,
    ENTRY_UNADMITTED,
    ENTRY_UNCONFIRMABLE,
    EXIT_BACKED_IN,
    EXIT_CONFIRMED,
    EXIT_HELD,
    EXIT_PENDING,
    EXIT_UNCONFIRMABLE,
    HELD,
    REASON_ARMING_INCOMPLETE,
    REASON_BANNED,
    REASON_CLOSING_LOOP_OCCUPIED,
    REASON_FORWARD,
    REASON_LOOP_DRIVER_TIMEOUT,
    REASON_NO_CLOSING_LOOPS,
    REASON_NO_PENDING_ENTRY,
    REASON_REVERSE,
    REASON_VEHICLE_TOO_CLOSE,
    REASON_WINDOW_ELAPSED,
    SESSION_CLOSE,
    SESSION_OPEN,
    SUPPRESSION_ENDED_ARMED,
    SUPPRESSION_ENDED_ARMING_LOOP_CLEARED,
    UNCONFIRMABLE,
    to_iso,
)


@dataclass(frozen=True, slots=True)
class _TransitNames:
    """The four event names for one direction of travel.

    Two directions, four outcomes, eight names, and not one of them shared. An
    entry that was backed out of and an exit that was reversed back into the
    garage are different facts about different money, and a single name for
    both would be the first place they got confused.
    """

    pending: str
    confirmed: str
    reversed_out: str
    held: str
    unconfirmable: str
    session_kind: str


#: A→B at an ENTRY lane means the vehicle went into the garage.
_ENTRY = _TransitNames(
    pending=ENTRY_PENDING,
    confirmed=ENTRY_CONFIRMED,
    reversed_out=ENTRY_BACKED_OUT,
    held=ENTRY_HELD,
    unconfirmable=ENTRY_UNCONFIRMABLE,
    session_kind=SESSION_OPEN,
)

#: A→B at an EXIT lane means the vehicle LEFT the garage -- the same forward
#: crossing, a different fact. B→A there is a vehicle that reversed back inside
#: rather than one that backed out of an entry, so it gets its own name.
_EXIT = _TransitNames(
    pending=EXIT_PENDING,
    confirmed=EXIT_CONFIRMED,
    reversed_out=EXIT_BACKED_IN,
    held=EXIT_HELD,
    unconfirmable=EXIT_UNCONFIRMABLE,
    session_kind=SESSION_CLOSE,
)

#: What each closing-loop geometry can report. A lane that receives anything
#: else from its driver did not get an answer it can use: one loop cannot have
#: seen a direction, and a two-loop lane is never confirmed on one occupancy.
_ANSWERS = {
    1: frozenset({ClosingSequence.OCCUPIED, ClosingSequence.NONE}),
    2: frozenset({ClosingSequence.FORWARD, ClosingSequence.REVERSE, ClosingSequence.NONE}),
}



def _confirmed_by(crossing: ClosingSequence) -> str:
    """The reason a confirmed transit carries, by what confirmed it.

    Two names, not one: a confirmation that also saw the direction and one that
    did not are different evidence, and the record says which. Read at call
    time, not built into a table at import, so the module's names are the ones
    in force.
    """
    if crossing is ClosingSequence.FORWARD:
        return REASON_FORWARD
    return REASON_CLOSING_LOOP_OCCUPIED


log = logging.getLogger(__name__)

#: The pictures kept with an entry (U7f), from the lane's camera: between the
#: vend and the closing loop, at the closing loop, and of a banned car refused
#: at the gate. A few of each -- enough to see the car and, for a tailgater,
#: to read it. AND THE ARMING READ'S OWN FRAMES (`[camera] frames_per_read`),
#: the ones the car was identified from: kept with the stay it opens, because
#: they are the pictures its colour, its shape and any damage were seen in --
#: read and thrown away, they were the one record of the car at the moment it
#: was decided on.
BETWEEN_PICTURES = 2
CLOSING_PICTURES = 2
REFUSED_PICTURES = 2


class LaneController:
    def __init__(
        self,
        config: LaneConfig,
        *,
        loop: LoopInput,
        camera: CameraFeed,
        vend: VendOutput,
        identifier: VehicleIdentifier,
        arming_loop_b: LoopInput | None = None,
        closing_loops: ClosingLoops | None = None,
        deactivate_loop: LoopInput | None = None,
        cache: DecisionCache | None = None,
        events: EventQueue | None = None,
        clock: Callable[[], float] = time.time,
        reader: ReaderScreen | None = None,
        banned_matcher: BannedMatcher | None = None,
    ) -> None:
        self.config = config
        # The banned-car comparison (U7f). None: this lane refuses no car as
        # banned, whatever list it holds.
        self.banned_matcher = banned_matcher
        self.loop = loop
        self.camera = camera
        self.vend = vend
        self.identifier = identifier
        # The config PUBLISHES the geometry and the wiring SUPPLIES it, and a
        # lane where those two disagree is refused at construction rather than
        # at 3am. A second arming loop that is wired but not declared is a loop
        # nothing in the record knows about; one declared but not wired would
        # make a single-loop lane look like a two-loop lane, which is the one
        # thing this must never do.
        self.arming_loop_b = arming_loop_b
        self.closing_loops = closing_loops
        if (config.loops.arming_loops == 2) != (arming_loop_b is not None):
            raise ValueError(
                f"loops.arming_loops = {config.loops.arming_loops} but "
                f"arming_loop_b was {'supplied' if arming_loop_b else 'not supplied'}: "
                "the declared geometry and the wired hardware must agree"
            )
        if config.loops.confirms_entry != (closing_loops is not None):
            raise ValueError(
                f"loops.closing_loops = {config.loops.closing_loops} but closing_loops was "
                f"{'supplied' if closing_loops else 'not supplied'}: "
                "the declared geometry and the wired hardware must agree"
            )
        # The third loop, held to the same rule: declared and not wired would
        # be a lane that publishes a hold it can never make; wired and not
        # declared would be a hold the record never explains.
        self.deactivate_loop = deactivate_loop
        if config.loops.has_deactivate_loop != (deactivate_loop is not None):
            raise ValueError(
                f"loops.deactivate_loops = {config.loops.deactivate_loops} but deactivate_loop "
                f"was {'supplied' if deactivate_loop else 'not supplied'}: "
                "the declared geometry and the wired hardware must agree"
            )
        # `cache or DecisionCache(...)` would be wrong, and was: DecisionCache
        # defines __len__, so a freshly synced cache that happens to hold zero
        # plate rules is falsy and would be silently thrown away and replaced
        # with an empty, never-refreshed one -- which then reports itself STALE
        # and sends every vehicle to fallback. A transient garage syncs exactly
        # zero plate rules, so this is the normal case, not an edge case.
        self.cache = (
            cache
            if cache is not None
            else DecisionCache(max_age_seconds=config.rules_max_age_seconds)
        )
        self.events = events if events is not None else EventQueue()
        # Injectable so a demo or a test can put a car through a three-hour
        # stay without waiting three hours. The lane's own clock is what stamps
        # session times, which is the point: the platform must price the stay
        # from when the car was there, not from when it heard about it.
        self._clock = clock
        # THE READ SIDE, and it is deliberately nothing more than this. The
        # contract publishes the last decision and the current transit, and
        # both live here, in memory, for exactly as long as this process does.
        #
        # No state store was added and none is intended: a restart returns
        # `None` and `TransitState.NONE`, and `GET /v1/lane/state` says so
        # honestly rather than reporting the last thing it happens to remember.
        self.last_decision: Decision | None = None
        self.last_decision_at: str | None = None
        #: WHEN the last decision was COMPLETED by `POST /v1/lane/vend`, or
        #: `None` while it has not been. A decision is one case and one case is
        #: one vend: this is what `already_completed` refuses against, what
        #: `GET /v1/lane/state` publishes as `decision.completed`, and it is
        #: cleared by the next arrival because a new decision is a new case.
        self.last_decision_completed_at: str | None = None
        #: WHAT THE READER WAS LAST TOLD, as `(decision_at, record)`, or `None`
        #: when it was told to show nothing. Set by `show_reader()` whether or
        #: not this lane has a reader, and published on `GET /v1/lane/state` as
        #: `exit_fee` -- so a second screen at this exit draws from the same
        #: hand-off the reader does, at the same moments.
        self.exit_screen: tuple[str, dict] | None = None
        # WHO DRAINS THE OUTBOX. None means "this thread, now" -- the bare
        # controller of the tests and the demo, where `run_once` returns with
        # the record delivered. `LaneRunner` installs a drain of its own so
        # that no thread the barrier runs on ever waits on the network; see
        # `deliver()`.
        self._drain: Callable[[], None] | None = None
        # THE CARD READER'S SCREEN at an exit, or None where there is no
        # reader. It is handed the exit decision's own record and nothing
        # else (`reader.py`), and -- like the outbox -- WHICH THREAD waits on
        # it is the runner's to say: None here means "this thread, now", and
        # `LaneRunner` installs a hand-off to a thread of its own, so the
        # barrier never waits on a reader. See `show_reader()`.
        self.reader = reader
        self._reader_hand: Callable[[dict | None], None] | None = None
        self.last_read_ref: str | None = None
        self.last_cause: str | None = None
        self.transit_state: str = TransitState.NONE.value
        self.transit_since: str | None = None
        #: The last identity a human or a display code COMPLETED through
        #: `POST /v1/lane/vend`, held here from before the relay was pulsed
        #: until the transit it opened is settled. It is the lane's own record
        #: of what it was told, and it is NOT published on any read route: the
        #: reference travels on the session action to the platform, where the
        #: retention purge can reach it.
        self.last_assisted: dict | None = None
        #: WHEN this lane first observed the arming loop occupied, across the
        #: run of observations that is still unbroken -- `None` whenever an
        #: observation read it clear. `arming_loop_stuck_occupied` is derived
        #: from it; see `observe_arming_loop`.
        self._arming_occupied_since: float | None = None
        #: The same measurement on the DEACTIVATE loop, for
        #: `deactivate_loop_stuck_occupied`; see `observe_deactivate_loop`.
        self._deactivate_occupied_since: float | None = None
        #: WHEN the current held arming interval began, on this lane's clock,
        #: or `None` when no interval is open. Opened by `run_once` when a
        #: vehicle is at the arming loop and the deactivate loop reads
        #: occupied; closed by `run_once` on the next turn that finds either
        #: loop changed. A LEVEL, re-read every turn, never an edge: an edge
        #: would fire once and leave two queued cars waiting for each other.
        self._suppressed_since: float | None = None
        #: Whether an assisted vend's settle has ever exceeded this lane's own
        #: deadline. `closing_loops_never_firing` is derived from it.
        self._loop_driver_timed_out = False
        #: HOW MANY READS OF THE LOOPS AFTER THE GATE ARE OUTSTANDING. A vend's
        #: settle counts itself in before its blocking read and out when the
        #: driver returns; the idle poll reads only while this is zero and
        #: otherwise skips its turn. Two readers of one loop board would be a
        #: crossing that belongs to a pending entry consumed by the poll and
        #: recorded as unadmitted, with the entry then held for a car that did
        #: go through -- one crossing, two wrong answers. The count, under the
        #: lock below, is what stops it; what the lock proves and what it does
        #: not is stated at the end of this comment, and "impossible" is used
        #: there only for the part it proves.
        #:
        #: A COUNT, NOT A MUTEX, and the difference took a lane down. This was
        #: a `threading.Lock` held across the settle's read. `vend.py` bounds a
        #: settle by ABANDONING a worker whose driver never returns -- and that
        #: worker was still inside the read, holding the lock, so every later
        #: vend's settle waited behind it until its own deadline (`unconfirmable`,
        #: no session, one more leaked thread each) and the next ordinary
        #: arrival's `run_once` waited on it with no deadline at all: the barrier
        #: opened and the lane's one loop thread hung. The vend's read must never
        #: wait on anything a hung thread can hold. It reads unconditionally,
        #: exactly as it did before the idle poll existed; the poll is the only
        #: reader that stands down, and it stands down without waiting.
        #:
        #: The count is released when the DRIVER RETURNS, never at abandonment:
        #: an abandoned worker is still blocked on this board, and a poll that
        #: read behind it would be the second reader the count exists to stop.
        #: So a driver that never returns leaves the idle read of the loops off
        #: at this lane until restart -- and nothing else: vends settle,
        #: arrivals are served, and `closing_loops_never_firing` is already
        #: `active` from the same event.
        #:
        #: `_board` is the bookkeeping lock for this count, for the published
        #: transit state, and for the poll's claim -- and it is held across
        #: NOTHING that leaves this process. Not across `wait_for_sequence`,
        #: not across `poll_sequence`, not across the relay. Held by exactly
        #: four things: the poll across its two checks (nothing pending,
        #: nothing reading) AND the claim that follows them (`_poll_reading`);
        #: the poll again, after its read, to release the claim and decide
        #: whose the crossing is; the vend's read across `+= 1` (after waiting
        #: for a poll read in flight to release its claim) and across `-= 1`;
        #: and `_transit` across the two assignments that publish a transit
        #: state. So the waits left in this design are: a vend counting itself
        #: in behind a poll's read in flight, on the settle thread, bounded by
        #: the settle's own deadline; and everything else waiting on a handful
        #: of assignments. THE VEND ROUTE WAITS ON NO DRIVER. The version before
        #: this one held `_board` across `poll_sequence` itself, and `_transit`
        #: publishes PENDING under `_board`, so a poll driver that hung held
        #: `POST /v1/lane/vend` inside `complete_vend` AFTER the relay had
        #: pulsed: no 202, a retry refused `already_completed`, the lane `busy`
        #: for as long as the driver hung, and the record of who opened the
        #: barrier sitting unflushed (measured 2026-09-19: no answer in 3 s at
        #: `0c91190`, 202 in 20 ms at `df982e2`). The seam says `poll_sequence`
        #: never blocks, and `wait_for_sequence` said the same, and this lane
        #: bounded the settle anyway because a driver breaks its contract on
        #: the day somebody is being paged about it.
        #:
        #: THE RULE, because it was broken twice in one round: A CHECK AND THE
        #: CLAIM THAT DEPENDS ON IT ARE ONE CRITICAL SECTION. The first version
        #: checked the count and then read, two moments, and a vend that began
        #: between them lost its crossing. The second checked "nothing pending"
        #: OUTSIDE this lock and read inside it: a poll that passed that check,
        #: was parked by the interpreter, and read after a vend had begun and
        #: its car had crossed, took that car's crossing -- the vend held, no
        #: session, and an `entry_unadmitted` written for a car that WAS
        #: admitted (measured: 23 to 42 of 200 with the crossing placed the
        #: instant the vend route answered). Now `_transit` publishes PENDING
        #: under this lock and the poll checks it under this lock, so either
        #: the poll's check-and-claim completes before PENDING exists, or it
        #: sees PENDING and stands down. THAT interleaving cannot happen, and
        #: `tests/test_unadmitted.py` holds the poll inside its critical section
        #: to prove a beginning vend waits for the claim -- and only for it.
        #:
        #: WHAT HAPPENS TO A CROSSING THE POLL IS HOLDING WHEN A VEND BEGAN
        #: DURING ITS READ: it is that vend's, and it is taken by that vend's
        #: read and by no read carrying a different `at` (what makes two
        #: differ, and the one way they might not, is stated at `_handed`).
        #: The poll claimed first, so the vend's own
        #: read waits behind it; when the poll's driver returns, the poll
        #: checks under `_board` whether a transit was published while it was
        #: out, and if one was it hands the crossing over (`_handed`) WITH THE
        #: TRANSIT'S `at` -- the timestamp `begin_transit` published and
        #: `resolve_transit` carries -- and a read takes the slot only when its
        #: own `at` is the slot's. Unbound, the slot went to whichever read
        #: reached it first: when the vend it was handed for had already been
        #: answered by the settle's deadline, the next read through either
        #: door -- a second assisted vend, or the loop thread's own next ALLOW
        #: arrival -- took it, and that car was `confirmed` with a session
        #: before it had crossed, its own crossing recorded `entry_unadmitted`
        #: at the next idle read (measured 2026-09-19: 18 to 38 of 200, no
        #: pause planted). Neither discarded -- that loses the promotion --
        #: nor labelled `entry_unadmitted` -- that mislabels an admitted car,
        #: the one thing this event exists not to do. WHAT A HANDED CROSSING
        #: BECOMES WHEN ITS VEND NEVER RETURNS -- the deadline answered it
        #: `unconfirmable` before the poll's driver came back -- is nothing:
        #: its own read, abandoned but still waiting, takes the slot, finds its
        #: claim gone and records nothing, exactly as a driver that returns
        #: after the deadline has always been treated. The lane admitted that
        #: car (the relay pulsed), so it is not `entry_unadmitted`; nothing
        #: confirmed it inside the deadline, so it is not `entry_confirmed`;
        #: the crossing is invisible in the record, and `entry_unconfirmable`
        #: is the only thing that says why. A RESIDUAL, stated, not called
        #: complete. What the hand-over itself costs is the residual
        #: `observe_closing_loops` states already: a car nothing admitted that
        #: crossed inside the poll's read, in the same instant a vend began, is
        #: promoted as the vend's car, and the vend's own crossing is the next
        #: idle read's -- count right, attribution one car off, in a window the
        #: seam's own contract puts at microseconds. Nothing here says that
        #: cannot happen.
        #:
        #: WHAT THE LOCK DOES NOT PROVE, said here so nobody stops checking: the
        #: relay is pulsed BEFORE `begin_transit` publishes PENDING
        #: (`complete_vend`, and it is the order the vend tests pin), so a poll
        #: whose whole check, claim and read complete in the microseconds
        #: between the relay firing and PENDING being published keeps a
        #: crossing that completed in that gap. That is a barrier that rose and
        #: a car that crossed two loops inside one function call; no
        #: measurement here says it happens, and nothing here proves it cannot.
        self._reads_outstanding = 0
        #: Whether the idle read's driver call is in flight: claimed under
        #: `_board`, released under it, and the one thing a vend's read waits
        #: for. A `Condition` over the same lock so that wait is a wait and
        #: not a spin.
        self._poll_reading = False
        #: A crossing the poll read while a vend began, held for that vend's
        #: read with that vend's `at`, so no read carrying a different `at`
        #: can take it. One slot is enough: at most one transit can be
        #: published during one poll read. The loop thread is inside the
        #: read, so only the route can publish; the route's first vend
        #: consumed the decision, so a second POST -- even after the settle's
        #: deadline has cleared `_in_progress` -- is refused
        #: `already_completed` (`vend.py`, one decision one vend), and a new
        #: decision needs `handle_arrival`, which is the loop thread, which is
        #: inside the read. And the poll does not read again while a transit
        #: is pending or a read is outstanding. Not "a second vend cannot
        #: begin while the first is pending": the first stops being pending at
        #: the settle's deadline, and a second transit can then begin through
        #: either door with the slot still full -- which is why the `at`
        #: travels with the crossing.
        #:
        #: WHAT MAKES TWO `at`s DIFFER, since the binding is an equality test
        #: on them: nothing in this file. The key is `to_iso(self._clock())`,
        #: a wall-clock timestamp at ONE MICROSECOND of resolution (that is
        #: `to_iso`'s, whatever the clock's), and two transits' samples are
        #: separated by at least the first's `begin_transit` and a whole
        #: door's work -- an arrival served, or an HTTP request -- so on a
        #: clock that only moves forward they never share a microsecond. The
        #: clock is `time.time`, which is NOT guaranteed to move forward: a
        #: wall-clock step backwards that lands a later transit's sample on
        #: the exact microsecond of a slot still full would let that read take
        #: the earlier transit's crossing -- one pair's attribution, the
        #: outcome this binding exists to prevent, in a window that needs the
        #: clock to step onto a single microsecond while an abandoned worker
        #: is between its notify and its take. Not guarded here; STATED, a
        #: residual smaller than the ones above it. An injected clock that
        #: does not advance would collide every time, and `demo.py` injects
        #: frozen ones -- it runs one arrival per lane and never an idle turn,
        #: so nothing is ever handed over there. Measured 2026-09-19 on the
        #: re-gate's box: 2000 of 2000 transits through the loop door carried
        #: distinct `at`s.
        self._handed: tuple[ClosingSequence, str] | None = None
        self._crossings_handed = 0
        #: How many transit states have ever been published. The poll notes
        #: it at its claim and compares after its read: a vend that began
        #: during the read moved it -- whether that vend is still pending or
        #: the lane's deadline has already answered it -- and the crossing
        #: in hand is that vend's either way.
        self._transits_published = 0
        self._board = threading.Condition()
        #: The held vend (its `transit_since`) whose late crossing has already
        #: been taken for its car, so a second crossing after it is not.
        self._late_taken_for: str | None = None
        #: A refused car's arming read, `(decision_at, frames)`, held until the
        #: next arrival (`_keep_refused_arming`); and the one a person's
        #: completion set aside for its transit, `(at, frames)`
        #: (`take_completion_arming`).
        self._held_arming: tuple[str, list] | None = None
        self._completion_arming: tuple[str, list] | None = None

    def now(self) -> float:
        """This lane's clock, which is the authority for WHEN.

        Public because the vend route ages a completion against it, and it must
        be the same clock the events are stamped with -- a test that puts a car
        through a three-hour stay in a millisecond injects one, and a second
        source of time here would make the two disagree.
        """
        return self._clock()

    def handle_arrival(self) -> Decision:
        """One vehicle, from arming to vend. Assumes the loop has already armed."""
        lane = self.config.lane_id

        frames = self.camera.grab(self.config.camera.frames_per_read)
        self.events.record("frames_captured", lane, count=len(frames), camera=self.camera.camera_id)

        identity = self.identifier.identify(frames)
        if identity.ticket_ref is not None:
            # THE SEAM. `VehicleIdentity` is what the vision stage BELIEVES IT
            # SAW, and a ticket is not a reading -- it is asserted by a person
            # or by a display code, through `POST /v1/lane/vend`, and the LANE
            # is what sets it. An identifier that could set it would be minting
            # a parking identity through the interface that exists to report
            # measurements, and that identity would reach the platform's
            # `vehicles` table on the session action below.
            #
            # The VALUE is not logged. Naming it would copy the very text this
            # exists to keep out of anything the lane owns, exactly as the
            # `unrecognised_cause` seam refuses to name what it rejected.
            log.warning(
                "an identifier supplied a ticket_ref; a ticket is asserted, never measured. "
                "Dropping it: only the assisted vend route may complete an identity"
            )
            identity = replace(identity, ticket_ref=None)
        self.events.record(
            "vehicle_identified",
            lane,
            confidence=identity.confidence,
            make=identity.make,
            model=identity.model,
            color=identity.color,
        )

        decision = decide(
            identity,
            self.cache,
            confidence_threshold=self.config.confidence_threshold,
            direction=self.config.direction,
            day_at=self.now(),
        )
        # THE QR SEAM (Phase 1b, not built): at a monthly/pass-only garage a
        # car no register covers is decided DENY with `REASON_NOT_REGISTERED`.
        # That decision is where a registration QR scanned at this lane will be
        # presented to the pass module's enrolment, before the gate's answer is
        # final; a QR it accepts turns the DENY into an open for this car.
        # Today there is no scanner seam, and the DENY stands.
        banned = self._banned(identity) if decision.should_vend else None
        if banned is not None:
            # A CAR ON THE GARAGE'S BANNED LIST (U7f): the gate stays shut,
            # under its own reason. The driver who presses help is told the
            # car was not admitted -- the line every refusal gets.
            decision = Decision(outcome=Outcome.DENY, reason=REASON_BANNED, identity=identity)
        if self.config.direction == "exit" and decision.should_vend and identity.plate:
            # THE EXIT'S LOCAL DECISION, BEFORE THE BARRIER MOVES: covered from
            # the cache, priced on the box, or said to be unpriceable here --
            # from memory and the in-process engine, and from nothing on the
            # network (`exit_pricing`). The vend below does not wait on it in
            # the sense that matters: it takes microseconds, and it is what
            # the fee on the screens will be read from.
            decision = replace(
                decision, exit_pricing=price_exit(identity.plate, self.cache, now=self.now())
            )
        decided = self.events.record(
            "decision",
            lane,
            outcome=decision.outcome.value,
            reason=decision.reason,
            fallback=decision.fallback.value if decision.fallback else None,
            rate_plan=decision.rate_plan,
            **(
                {"exit_pricing": decision.exit_pricing.to_detail()}
                if decision.exit_pricing is not None
                else {}
            ),
            # A CAR LET IN WITH NO READ AT ALL -- the engine not reached, no
            # frames -- says which failure, as `fallback_needs_human` does for
            # one that was not let in: somebody has to go and fix it, and the
            # car getting in is not a reason for the record to stop saying so.
            # A member of `Unavailable`, never text: this detail is append-only.
            **(
                {"cause": identity.unavailable.value}
                if identity.unavailable is not None and decision.should_vend
                else {}
            ),
        )
        if banned is not None:
            # Recorded with its pictures: taken now, the car at the gate, and
            # kept against the decision that refused it.
            self.events.queue_pictures(decided.event_id, self._take_pictures(REFUSED_PICTURES))
        # Held so the read contract can publish it. Taken here rather than
        # reconstructed from the event queue, which `flush()` empties: a
        # consumer asking what the lane last decided must not get a different
        # answer depending on whether the platform happened to be reachable.
        self.last_decision = decision
        self.last_decision_at = to_iso(self._clock())
        # A NEW CASE, so nothing has completed it. This is the only way back to
        # a completable decision, and it is the whole of "one decision, one
        # vend": the second vend a caller can legitimately get is the one that
        # belongs to the second car.
        self.last_decision_completed_at = None
        self.last_read_ref = identity.read_ref
        self.last_cause = identity.unavailable.value if identity.unavailable else None
        # A refused car's arming read is held until the next arrival, for the
        # stay it may yet get -- by a person's word, or by driving in anyway.
        self._keep_refused_arming(decision, frames)

        if self.config.direction == "exit" and decision.outcome is not Outcome.NO_VEHICLE:
            # THE FEE ON THE READER, BEFORE THE BARRIER MOVES -- handed over,
            # not waited on. The record is the exit decision's own, the same
            # words the `decision` event above carries and the close will
            # carry as `local_decision`; a car this lane made no money
            # decision about hands `None`, which takes down whatever the
            # reader was showing for the car before it.
            self.show_reader(
                decision.exit_pricing.to_detail() if decision.exit_pricing is not None else None
            )

        if decision.should_vend:
            self.vend.vend(decision.reason)
            self.events.record("vended", lane, reason=decision.reason)

            # The vend is where the ticket comes out, and the ticket is NOT the
            # entry. What goes on the queue here is a PENDING entry; the loops
            # after the gate decide whether it becomes a session.
            #
            # The timestamp is the lane's, not the platform's, and it is taken
            # HERE rather than after the confirmation: the car arrived when it
            # arrived, whatever time the server eventually hears about it, and
            # pricing a stay by when the network came back would be wrong.
            #
            # AT AN EXIT THE LANE PRICED, IT IS THE PRICE'S OWN EXIT TIME: one
            # reading of the clock for the fee and the close. Read twice --
            # once for the price, again after the reader and the relay -- the
            # two differed by the relay's time, and the platform's reconciler,
            # which compares them to the millisecond, flagged nearly every
            # lane-priced close `inputs_disagree` with the fee right.
            exit_at = getattr(decision.exit_pricing, "exit_at", None)
            at = exit_at if exit_at else to_iso(self._clock())
            # The arming read's frames ride with the transit, to be kept with
            # the stay it opens -- and with nothing if it opens none.
            self._settle_transit(
                identity,
                at,
                exit_pricing=decision.exit_pricing,
                arming=frames if self.config.direction == "entry" else (),
            )

        elif decision.outcome is Outcome.NO_VEHICLE:
            # D3. Nothing was there, so nothing happens: no ticket, no session,
            # no vend. But it is RECORDED, and that is the point of the event --
            # a lane being worked by someone tripping the loop with a piece of
            # metal shows up as a pattern in the log instead of as silence.
            #
            # No plate and no image in the detail. `events` is append-only by
            # grant, so the retention purge cannot reach what is written here.
            log.info("lane %s refusing to transact: %s", lane, decision.reason)
            self.events.record(
                "arming_rejected",
                lane,
                reason=decision.reason,
                presence=False,
                camera=self.camera.camera_id,
            )

        elif decision.outcome is Outcome.FALLBACK:
            # Not a guess and not a silent drop. The fallback is a named path
            # with an event behind it, so an operator can see it happened and
            # the record shows why the lane declined to decide.
            #
            # The event is the whole of the fallback for now: the human/phone
            # path that answers it belongs with Claim Check and is not built.
            # It is a stub that LOGS, not a stub that pretends.
            log.info("lane %s falling back: %s", lane, decision.reason)
            self.events.record(
                "fallback_needs_human",
                lane,
                reason=decision.reason,
                fallback=decision.fallback.value if decision.fallback else None,
                confidence=identity.confidence,
                # WHICH failure, when the fallback is `engine_unreachable`, and
                # None for every fallback that did get a read. One code says
                # what the lane did; this says what somebody has to go and fix,
                # and a code with nothing behind it would send whoever answers
                # the intercom to look at the wrong thing.
                #
                # Note what it sits beside: on this branch `confidence` is 0.0
                # and is NOT a measurement -- nothing measured it -- so a
                # reader that takes the number without this field is reading a
                # marginal plate that does not exist.
                cause=identity.unavailable,
                # A LANE THE OWNER CLOSED, and how: `full` or `everyone`, as
                # the lane acted on it. This event is the one record of an
                # arrival the closing turned away, so it says so -- and, like
                # every event, names no plate and holds no picture.
                **(
                    {"closed_reason": closed_to(self.cache, self.config.direction)}
                    if decision.fallback is Fallback.LANE_CLOSED
                    else {}
                ),
            )

        # Best effort, and after the barrier has already been told what to do.
        # Nothing above this line waits on the network.
        self.deliver()
        return decision

    # ------------------------------------------------------------------
    # The ASSISTED vend: an identity a human or a display code completed.
    # ------------------------------------------------------------------

    def complete_vend(self, identity, *, authorised_by: str, assisted: dict) -> str:
        """Write the identity, pulse the relay, record it, open a pending entry.

        **THE ORDER IS THE SAFETY PROPERTY AND IT IS THE WHOLE OF THIS METHOD.**
        The identity is recorded BEFORE the relay is asked to move. Every other
        ordering is a barrier that opened with nothing in the record saying who
        said so, and that is the vend this project's outside reviewers named:

            1. `assisted_identity`   who authorised it, which KIND of identity
                                     it is, the caller's idempotency key, and
                                     the decision it completes
            2. `vend.vend(reason)`   the relay, with the AUTHORITY as its reason
            3. `vended`              the lane's ordinary record of a vend
            4. the pending entry      via `begin_transit` -- the ticket is not
                                     the entry here either

        `tests/test_vend.py` records every one of those calls and refuses any
        order but this one.

        The ticket REFERENCE is not in the event at step 1. It is held on this
        controller and it travels on the session action, which becomes
        `POST /lane/sessions/open` and lands in a column the platform's
        retention purge redacts. `events` is append-only by grant there, so a
        reference written into a detail would be the one identity nothing could
        ever remove.

        Returns the lane's own timestamp for the vend, which is what
        `resolve_transit` must be given: the car arrived when it arrived,
        whatever time anything else eventually hears about it.
        """
        lane = self.config.lane_id
        # FIRST, and before anything moves. Held on the controller as well as
        # recorded, because the record is what an operator reads and this is
        # what the session action is built from.
        self.last_assisted = dict(assisted)
        self.events.record("assisted_identity", lane, **assisted)

        self.vend.vend(authorised_by)
        self.events.record("vended", lane, reason=authorised_by)

        at = to_iso(self._clock())
        # THE DECISION IS CONSUMED HERE, beside the relay pulse it authorised
        # rather than in the route that asked for it, so nothing can vend
        # without consuming and nothing can consume without vending.
        arming = self._take_held_arming()
        self._completion_arming = (at, arming) if arming else None
        self.last_decision_completed_at = at
        self.begin_transit(identity, at)

        # AND THE RECORD LEAVES THE BOX, exactly as `handle_arrival` ends.
        # `EventQueue.record` appends to two in-memory deques and nothing else;
        # `flush()` is the only thing that calls the transport, and this path
        # had none. A barrier opened and the platform held nothing -- not the
        # session that bills the stay, and not the `assisted_identity` that is
        # the only record of who authorised the barrier moving -- until some
        # later ordinary arrival flushed, which at a lane using the intercom may
        # never come. There is no state store behind the queue, so a restart
        # before that lost the record entirely.
        #
        # Best effort and after the barrier has already been told what to do,
        # for the same reason as `handle_arrival`: nothing above this line waits
        # on the network. WHAT A KILL AT EACH POINT LEAVES is asserted in
        # `tests/test_vend.py` and stated in `docs/CONTRACT.md`.
        self.deliver()
        return at

    # ------------------------------------------------------------------
    # From the vend to the session, or to one of the three other answers.
    # ------------------------------------------------------------------

    def _confirmation_window(self) -> float:
        """How long the crossing may take. The site's setting, never a constant."""
        return self.config.loops.confirmation_window_seconds

    @staticmethod
    def _within_window(elapsed: float, window: float) -> bool:
        """Whether the crossing arrived inside the window the event stamps.

        The comparison the event's `geometry_assumed` describes. A FORWARD that
        took longer is not a confirmation -- the window is what makes one mean
        "a vehicle went through in a plausible time" rather than "something
        happened here eventually" -- and it falls through to HELD like any other
        window that elapsed.
        """
        return elapsed <= window

    @staticmethod
    def _confirms(crossing: ClosingSequence) -> bool:
        """A-then-B over two loops, or the closing loop occupied. Nothing else.

        ONE function for both ends and for both reads: the vend's (entry and
        exit alike, through `resolve_transit`) and the idle poll's, which
        records what would have confirmed a vend as an entry nothing admitted.
        Which geometry may give which answer is `_as_declared`'s.
        """
        return crossing in (ClosingSequence.FORWARD, ClosingSequence.OCCUPIED)

    def _as_declared(self, crossing: ClosingSequence) -> ClosingSequence:
        """The driver's answer, if this lane's declared geometry can give it.

        A one-loop lane whose driver says FORWARD is claiming a direction no
        loop at this lane can see; a two-loop lane whose driver says OCCUPIED
        would be confirmed on one occupancy, which is weaker than what that lane
        declared. Either is a driver that does not match the lane, and its
        answer is not used: the transit takes the path an absent crossing takes,
        and the log says why.
        """
        if crossing in _ANSWERS[self.config.loops.closing_loops]:
            return crossing
        log.error(
            "lane %s: the closing-loop driver reported %s, which a lane with %d closing "
            "loop(s) cannot report; not used",
            self.config.lane_id,
            crossing.value,
            self.config.loops.closing_loops,
        )
        return ClosingSequence.NONE

    @staticmethod
    def _refutes(crossing: ClosingSequence) -> bool:
        """B-then-A: a vehicle backing out. An observation, not an absence."""
        return crossing is ClosingSequence.REVERSE

    @staticmethod
    def arming_complete(loop_b: LoopInput | None) -> bool:
        """Both arming loops occupied together, or a lane that has only one.

        Public because `POST /v1/lane/vend` applies it too, and it applies THIS
        function rather than a copy: a completion that could open a barrier on
        an arming geometry `run_once` would have refused is the refusal
        existing in one place and not in the other.
        """
        return loop_b is None or loop_b.is_occupied()

    @staticmethod
    def held_by_deactivate_loop(deactivate_loop: LoopInput | None) -> bool:
        """The deactivate loop reads occupied NOW: a vehicle too close behind.

        Public for the same reason as `arming_complete`, and applied by
        `POST /v1/lane/vend` as `vehicle_too_close`: a completion that opened
        the boom with a second car on this loop is the tailgate arriving
        through the intercom instead of through the camera. A lane with no
        deactivate loop is never held, and says so with `deactivate_loops: 0`
        rather than with silence.
        """
        return deactivate_loop is not None and deactivate_loop.is_occupied()

    def _transit(self, state: TransitState, at: str) -> None:
        """Move the published transit, beside the event that says the same thing.

        One call per outcome and never a shared one, for the reason every other
        name in this file is unshared: an entry that was backed out of and one
        that was merely never confirmed are different facts, and a helper that
        collapsed them would be the first place they got confused.

        UNDER `_board`, because the idle poll's "nothing pending" check reads
        this state under the same lock: the check and the poll's claim are one
        critical section, and publishing PENDING is the other side of it. Two
        assignments, nothing that can block, and nothing that waits on a
        driver: `_board` is never held across one, so this returns in the time
        it takes a handful of assignments elsewhere to finish. No caller holds
        `_board` when it gets here -- `resolve_transit` has released it inside
        `_read_closing_loops` before it decides anything, and the poll never
        moves the transit.
        """
        with self._board:
            self.transit_state = state.value
            self.transit_since = at
            self._transits_published += 1

    @staticmethod
    def _identity_kind(identity) -> str:
        """What KIND of identity this is: read, asserted, or a plate nobody
        could read.

        Derived from the identity itself rather than passed alongside it, so a
        record cannot say `plate` about a ticket. It is published on the pending
        event; the ticket REFERENCE is not -- see `begin_transit`. Called on
        the identity a stay carries (`_stay_identity`), so `plate` means a
        plate this lane stands behind and `unread` means there is none.
        """
        if identity.ticket_ref:
            return "ticket"
        return "plate" if identity.plate else "unread"

    def _settle_transit(self, identity, at: str, *, exit_pricing=None, arming=()) -> None:
        """Record the pending entry, then let the closing loops decide its fate.

        Two halves, called as one here and separately by the assisted vend --
        which has to ANSWER its caller once the pending entry exists and then go
        on waiting for the crossing, because the confirmation window is ten
        seconds and an HTTP route that held one open for it would be reporting
        a settled transit as though it were an immediate one.

        `exit_pricing` is THIS car's local decision, handed down explicitly so
        the close carries the decision the barrier moved on -- not whatever
        `last_decision` holds by the time a settle records the session, which
        on a thread of its own could be the next car's. `arming` is THIS car's
        arming read, handed down for the same reason, to be kept with its stay.
        """
        self.begin_transit(identity, at)
        self.resolve_transit(identity, at, exit_pricing=exit_pricing, arming=arming)

    def begin_transit(self, identity, at: str) -> None:
        """The vend created a PENDING entry. Nothing has decided its fate yet."""
        lane = self.config.lane_id
        names = _ENTRY if self.config.direction == "entry" else _EXIT
        geometry = self.config.loops.as_published()

        if self.config.direction == "entry":
            # What the stay will carry. An exit records its pending close on
            # the read as it came, exactly as before this rule.
            identity = self._stay_identity(identity)
        self.events.record(
            names.pending,
            lane,
            plate_region=identity.plate_region,
            # WHICH KIND of identity opened this, and never the ticket itself.
            # This event reaches `GET /v1/lane/events` and the platform's
            # `events` table, which is append-only by grant -- so a reference
            # written here would be the one identity nothing could ever redact.
            # Publish less: the session action carries the value, and it lands
            # in a column the retention purge reaches.
            identity_kind=self._identity_kind(identity),
            at=at,
            geometry_assumed=geometry,
        )
        self._transit(TransitState.PENDING, at)

    def resolve_transit(
        self, identity, at: str, *, claim=None, exit_pricing=None, arming=()
    ) -> None:
        """What the loops after the barrier made of the pending entry.

        `arming` is the arming read's frames, kept with the stay this transit
        opens -- confirmed, or unconfirmable at a lane with no closing loop --
        and with nothing when it opens none.

        `claim` is how the assisted vend bounds a loop driver that does not
        return: the settle waits for this call under its own deadline and, when
        the deadline wins, records the outcome itself. Exactly one of the two
        may publish, so a driver that returns an hour late finds the claim taken
        and records nothing -- otherwise one transit would get two outcomes, and
        the second of them would be a confirmed billable session for a crossing
        this lane had already published as unconfirmable.

        `None` is the ordinary arrival's path, which has one settler and needs
        no claim.
        """
        lane = self.config.lane_id
        names = _ENTRY if self.config.direction == "entry" else _EXIT
        geometry = self.config.loops.as_published()

        def mine() -> bool:
            return claim is None or claim.take()

        if self.closing_loops is None:
            # No closing loops at this site, so nothing here can confirm or
            # refute anything. Saying so on every single vehicle is the point:
            # the previous generation of this weakness was true from the day it
            # was written, published in pieces across three documents, and
            # never once stated in a single place a reader would reach.
            if not mine():
                return
            self.events.record(
                names.unconfirmable,
                lane,
                reason=REASON_NO_CLOSING_LOOPS,
                at=at,
                geometry_assumed=geometry,
            )
            self._transit(TransitState.UNCONFIRMABLE, at)
            opened = self._record_session(
                identity, at, confirmation=UNCONFIRMABLE, exit_pricing=exit_pricing
            )
            if opened is not None and arming:
                # No closing loop to picture the car at: the arming read's
                # frames are the stay's pictures.
                self.events.queue_pictures(opened.event_id, arming)
            return

        window = self._confirmation_window()
        # Read either side of the call, because the window is OURS to apply.
        # `interfaces.ClosingLoops` asks an implementation not to report FORWARD
        # for a crossing slower than the window, and an obligation on the other
        # side of a seam is a comment, not a check: a loop board that reports a
        # crossing late -- because it is faulty, because its own clock drifted,
        # or because somebody wrote it that way -- got a confirmed, billable
        # session out of a window this lane published on the event and never
        # applied.
        # PICTURES BETWEEN THE VEND AND THE CLOSING LOOP (U7f), at an entry:
        # the car moving through the gate. Kept with the stay if the closing
        # loop confirms it, and with nothing otherwise.
        between = self._take_pictures(BETWEEN_PICTURES) if self.config.direction == "entry" else []
        started = self._clock()
        crossing = self._as_declared(self._read_closing_loops(window, at))
        elapsed = self._clock() - started

        # THE DRIVER RETURNED. Whether this call is still the one that may say
        # what happened is a different question -- see `claim` above.
        if not mine():
            log.warning(
                "lane %s: the closing-loop driver returned after this lane's settle deadline; "
                "the transit was already resolved and this crossing is not recorded",
                lane,
            )
            return

        if self._confirms(crossing) and self._within_window(elapsed, window):
            self.events.record(
                names.confirmed,
                lane,
                reason=_confirmed_by(crossing),
                at=at,
                geometry_assumed=geometry,
            )
            self._transit(TransitState.CONFIRMED, at)
            opened = self._record_session(
                identity, at, confirmation=CONFIRMED, exit_pricing=exit_pricing
            )
            if opened is not None:
                # The arming read's frames, and at the closing loop the car
                # that confirmed the entry.
                self.events.queue_pictures(
                    opened.event_id,
                    [*arming, *between, *self._take_pictures(CLOSING_PICTURES)],
                )
            return

        if self._refutes(crossing):
            # A vehicle that backed out. There is no session and no occupancy,
            # and the pending entry is closed with its own reason rather than
            # being left to time out as though nothing had been observed --
            # something was observed, and it was the opposite of an entry.
            log.info("lane %s: backed out of the gate, no session", lane)
            self.events.record(
                names.reversed_out, lane, reason=REASON_REVERSE, at=at, geometry_assumed=geometry
            )
            self._transit(TransitState.BACKED_OUT, at)
            return

        # No confirmation inside the window: nothing crossed, or a FORWARD or
        # an occupancy arrived after it. At an ENTRY that is NEVER silently voided -- that is
        # the abandoned-ticket fraud, exactly the ticket no car ever followed --
        # and NEVER turned into a session, which is the phantom occupant. Held
        # and flagged. What an attendant does about it is the intercom's job and
        # is not built.
        log.warning(
            "lane %s: no crossing confirmed inside the %.1fs window (%.1fs elapsed); %s HELD",
            lane,
            window,
            elapsed,
            self.config.direction,
        )
        self.events.record(
            names.held, lane, reason=REASON_WINDOW_ELAPSED, at=at, geometry_assumed=geometry
        )
        self._transit(TransitState.HELD, at)
        if self.config.direction == "exit":
            # AN EXIT IS THE OTHER WAY ROUND, and this is the one asymmetry in
            # the file. The vend at an exit IS the payment moment and the
            # barrier opened: the car has left whatever the loops saw. Leaving
            # the session open would mean an unbilled stay and a vehicle counted
            # as inside for ever -- so installing the loops would make a site
            # worse than one without them, which is the opposite of what they
            # are for. It closes, it bills, and it says `held` with the
            # `exit_held` event above beside it: a flag for a human, not a hole
            # in the ledger.
            self._record_session(identity, at, confirmation=HELD, exit_pricing=exit_pricing)

    def transit_timed_out(self, identity, at: str, deadline: float) -> None:
        """The loop driver did not return inside this lane's own deadline.

        A FOURTH ANSWER, and it is deliberately not one of the other three. It
        is not `confirmed` -- nothing crossed as far as this lane knows. It is
        not `backed_out` -- that is an observation, and this is the absence of
        one. It is not `held` either: `held` means the window elapsed with the
        loops answering, which is a car that did not go through, and this is the
        loops not answering at all. The lane cannot tell those apart, and
        `unconfirmable` is the honest name for a transit nothing could confirm
        or refute -- the same word a lane with no closing loops uses, for the
        same reason, with its own reason code beside it saying which.

        NO SESSION IS OPENED. A lane with no closing loops opens one because it
        never had a way to confirm and its records say so on every vehicle; this
        lane DECLARED loops and did not hear from them, so billing a stay off
        that would be the phantom occupant with a fault report attached.
        """
        lane = self.config.lane_id
        names = _ENTRY if self.config.direction == "entry" else _EXIT
        log.error(
            "lane %s: the closing-loop driver did not return inside %.1fs "
            "(window + settle_grace_s); the transit is unconfirmable",
            lane,
            deadline,
        )
        # MEASURED FROM EXACTLY THIS, and from nothing else. See
        # `LaneService._closing_loops_never_firing`.
        self._loop_driver_timed_out = True
        self.events.record(
            names.unconfirmable,
            lane,
            reason=REASON_LOOP_DRIVER_TIMEOUT,
            at=at,
            geometry_assumed=self.config.loops.as_published(),
            settle_deadline_s=deadline,
        )
        self._transit(TransitState.UNCONFIRMABLE, at)
        self.deliver()

    @property
    def loop_driver_timed_out(self) -> bool:
        """Whether an assisted settle has ever exceeded this lane's deadline."""
        return self._loop_driver_timed_out

    # ------------------------------------------------------------------
    # A crossing with nothing pending: the read nobody made.
    # ------------------------------------------------------------------

    def _read_closing_loops(self, window: float, at: str) -> ClosingSequence:
        """The vend's read of the loops after the gate: counted in, counted out.

        It waits for exactly one thing: a poll's read in flight, which claimed
        the board first and must return before a second driver call is made on
        it -- microseconds by the seam's promise, and as long as the driver
        takes if the driver breaks it. That wait is on THIS thread: the
        `assisted-vend-resolve` worker, which `vend.py` abandons at the settle
        deadline exactly as it abandons a hung blocking read, or the loop
        thread on an ordinary arrival, which is the poll's own thread and so
        never finds one in flight. It is never the vend route's wait.

        Then: if the poll handed over a crossing that completed while THIS
        transit was beginning -- the slot carries the transit's `at`, and only
        a read carrying the same `at` takes it; a read carrying a different
        `at` leaves it and asks the driver. What makes a later transit's `at`
        differ is stated at `_handed`, with the one way it might not -- that
        crossing is this read's answer and the driver is not asked. Otherwise
        the count goes up, the driver is asked, and the
        count comes down when -- and only when -- the driver returns. A driver
        that never returns leaves the count up for ever, which is one idle poll
        standing down at this lane for ever, and that is the whole of its
        cost; see `_reads_outstanding`.

        Its own method so the fail-control can put the mutex back and watch
        the lane starve.
        """
        with self._board:
            self._board.wait_for(lambda: not self._poll_reading)
            handed = None
            if self._handed is not None and self._handed[1] == at:
                handed, self._handed = self._handed[0], None
            self._reads_outstanding += 1
        try:
            if handed is not None:
                return handed
            return self.closing_loops.wait_for_sequence(window)
        finally:
            with self._board:
                self._reads_outstanding -= 1

    def _nothing_pending(self) -> bool:
        """Whether an idle read of the loops may happen right now.

        Not while a vend is pending: from `begin_transit` until the settle
        thread reaches its blocking read there is a moment where nothing is
        counted outstanding and the crossing that arrives belongs to that
        vend. The published transit state covers that moment -- PROVIDED this
        is read under `_board`, which is where `_transit` publishes it. The poll
        calls this holding `_board`, so its answer and the claim that follows
        are one moment relative to `begin_transit`; read unlocked, the answer
        is stale by the time it is used, and that is the gap that let a poll
        take a beginning vend's crossing. The claim covers the read itself,
        and a vend that begins during the read gets what the read found.
        """
        return self.transit_state != TransitState.PENDING.value

    def _nothing_reading(self) -> bool:
        """Whether no read of the loops is outstanding right now. Never waits.

        The poll calls this holding `_board`, so its answer and the claim that
        follows are one moment; anything else may call it unlocked. The poll's
        own read in flight is not counted here -- it is `_poll_reading`, and
        the poll cannot be asking while it is out.
        """
        return self._reads_outstanding == 0

    def observe_closing_loops(self) -> str | None:
        """Poll the loops after the gate while nothing is pending, and record what crossed.

        Called on every IDLE turn of `run_once` -- a poll of the arming loop
        that produced no arrival -- and not on a turn that serves one. A
        crossing sitting unread when an arrival comes in is therefore consumed
        by that arrival's vend and promoted as though it were that vehicle, and
        the vehicle's own crossing is then the one recorded here: the count is
        right and the attribution is one car off, for at most one poll interval
        of unread crossing. The same one-car-off, in a window of microseconds
        rather than a poll interval, when a vend begins WHILE this read is in
        flight: the crossing this read comes back with is then handed to that
        vend rather than recorded here (see `_reads_outstanding`), because the
        alternative is an admitted car written down as one nothing admitted.
        Reading before the arrival is served would fix the attribution and is
        not done, because nothing in this package can say whether a crossing it
        holds happened before or after the arming loop fired -- the seam
        reports sequences, not times.

        Returns the kind recorded, or None when nothing was: because there are
        no loops, because a read of them is outstanding, because nothing
        crossed, or because what crossed was handed to a vend that began
        during the read.

        WHAT IT RECORDS. A crossing at an ENTRY lane that would have confirmed
        a vend (`_confirms`: FORWARD over two loops, OCCUPIED over one) is
        `entry_unadmitted`, with its own reason and with what this lane last
        decided and what its last vend became beside it -- the lane cannot bind
        the crossing to either, and the detail says what it knew rather than
        guessing which car this was. No plate on the event: no identity text
        goes on the event stream.

        AND IT OPENS A STAY. A car that crossed the loops after the gate is
        inside, whatever opened the barrier, and the owner's rule is that the
        loops and the pictures make the entry: `session_open`, `confirmed` (the
        loops saw it), `tailgater: true` (no open command let it in -- except
        the car this lane admitted and held, crossing late: `_late_admitted`),
        with the closing-loop pictures and the identity read from them, a plate
        or "unreadable" (`_crossing_identity`). Only a crossing the camera says
        no car made opens nothing. It moves no
        transit state, because that enum says what became of the last VEND.
        A REVERSE crossing with nothing pending is a vehicle leaving through
        an entry the wrong way, which is not an entry of any kind and has no
        name on the platform yet; it is logged, and it is the one thing this
        method sees and does not record. An EXIT lane's unpended crossing is
        a car that left without paying, a different fact about different
        money with no kind on the platform yet; logged, not recorded.
        """
        if self.closing_loops is None:
            return None
        with self._board:
            # BOTH CHECKS AND THE CLAIM UNDER ONE LOCK. `_transit` publishes
            # PENDING under this same lock, so a vend cannot begin between the
            # pending check and the claim: either this whole block runs first,
            # or the vend's PENDING is already published and this stands down.
            # Checked outside the lock, "nothing pending" was true when asked
            # and false by the time the board was read, and the poll took a
            # crossing that belonged to the vend that had just begun.
            if not self._nothing_pending() or not self._nothing_reading():
                # A vend is pending, or a settle is reading -- or was, and its
                # driver has not come back. Whatever crosses now is that vend's
                # answer. Skip the turn; never wait for it.
                return None
            self._poll_reading = True
            claimed_at = self._transits_published
        # THE READ, OUTSIDE THE LOCK. Under it, a poll driver that hung held
        # `_transit`, and with it `POST /v1/lane/vend` after the relay had
        # pulsed. The claim above is what a vend's read waits for instead.
        try:
            crossing = self._as_declared(self.closing_loops.poll_sequence())
        except BaseException:
            with self._board:
                self._poll_reading = False
                self._board.notify_all()
            raise
        with self._board:
            self._poll_reading = False
            # A VEND BEGAN WHILE THIS WAS OUT. Its PENDING went up under this
            # lock after the claim -- and, if the driver was out long enough,
            # the lane's deadline may already have answered it -- so the
            # crossing in hand, if there is one, is that vend's: it is handed
            # over, not recorded, WITH THAT TRANSIT'S `at` (`transit_since`
            # is still its `at` after the deadline, which republishes under
            # the same one), and only the read carrying that `at` takes it
            # before asking the driver. That vend's read, if the deadline has
            # already answered it, finds its claim taken and records nothing,
            # exactly as a late driver return; a later transit's read leaves
            # the slot alone. Whose the crossing REALLY was is the residual
            # stated in this method's docstring.
            handed = (
                crossing is not ClosingSequence.NONE and self._transits_published != claimed_at
            )
            if handed:
                self._handed = (crossing, self.transit_since)
                self._crossings_handed += 1
            self._board.notify_all()
        if handed:
            log.info(
                "lane %s: a vend began during the idle read; the crossing it found is the vend's",
                self.config.lane_id,
            )
            return None
        if crossing is ClosingSequence.NONE:
            return None

        lane = self.config.lane_id
        if not self._confirms(crossing):
            log.warning(
                "lane %s: the loops after the gate reported %s with nothing pending; "
                "not an entry, not recorded",
                lane,
                crossing.value,
            )
            return None
        if self.config.direction != "entry":
            log.warning(
                "lane %s: a vehicle crossed this EXIT lane's loops with nothing pending; "
                "a stay left unbilled, and no event kind names it yet",
                lane,
            )
            return None

        at = to_iso(self._clock())
        log.warning(
            "lane %s: a vehicle crossed the loops after the gate with nothing pending; "
            "recorded as an entry nothing admitted",
            lane,
        )
        # A CAR THROUGH THE GATE WITH NO OPEN COMMAND IS A STAY (the owner's
        # rule, 2026-10-26: the loops and the pictures make the entry, the
        # barrier plays no part in the record). Pictures at the closing loop,
        # and the car's identity from them: a plate read at the lane's
        # confidence, or "unreadable" -- a value of the car's identity, never a
        # reason to leave it out of the record. The tailgater, the refused car
        # that drove in anyway, the car through a barrier that is up: each is a
        # stay like any other, pays at the exit, and says no open command let
        # it in (`tailgater: true`). The only crossing that opens nothing is
        # one the camera says no car made (`_crossing_identity`).
        frames = self._take_pictures(CLOSING_PICTURES)
        identity = self._crossing_identity(frames)
        read = (
            plate_read(identity, self.config.confidence_threshold) if identity is not None else None
        )
        unadmitted = self.events.record(
            ENTRY_UNADMITTED,
            lane,
            reason=REASON_NO_PENDING_ENTRY,
            # Whether the plate was read: a fact, not whether there is a stay.
            identified=read is not None,
            stay_opened=identity is not None,
            at=at,
            geometry_assumed=self.config.loops.as_published(),
            # What this lane knew, not which car this was. `last_decision` is
            # the outcome of the last vehicle it identified -- `deny` here is a
            # refused car that may have driven in; `allow` is a car that was
            # admitted and a second one that may have followed it. `last_transit`
            # is what that vend became: `held` here is a car that may have gone
            # through late. "May" every time, and the detail says so by naming
            # the lane's state rather than a vehicle.
            last_decision=self.last_decision.outcome.value if self.last_decision else None,
            last_decision_at=self.last_decision_at,
            last_transit=self.transit_state,
        )
        if identity is not None:
            # TWO MARKS, NOT ONE. `no_open_command`: no open command of this
            # lane let the car in -- every such car, read or not. `tailgater`
            # keeps its own meaning (U7f): such a car whose plate was READ, the
            # one a garage that bans tailgaters may list. A car nobody could
            # read is never a tailgater: a monthly with a dirty plate through a
            # raised barrier must not be locked out on a match nobody can check.
            # The car this lane admitted and held, crossing late, carries
            # neither: its open command let it in.
            late = self._late_admitted(identity)
            opened = self._record_session(
                identity,
                at,
                confirmation=CONFIRMED,
                tailgater=not late and read is not None,
                no_open_command=not late,
            )
            # And the arming read of the car this lane last refused, when this
            # crossing may be that car driving in anyway.
            arming = [] if late else self._refused_arming_for(identity)
            self.events.queue_pictures(opened.event_id, [*arming, *frames])
        else:
            self.events.queue_pictures(unadmitted.event_id, frames)
        self._unadmitted_leaves_the_box()
        return ENTRY_UNADMITTED

    def _unadmitted_leaves_the_box(self) -> None:
        """Flush, exactly as every other path that writes a record ends.

        Its own method so the fail-control can remove it: an idle poll that
        recorded and did not flush leaves the record on a box whose next
        ordinary arrival may be hours away, and at a lane where the barrier is
        up that arrival never vends. Same reason as `complete_vend`.
        """
        self.deliver()

    def observe_arming_loop(self) -> float | None:
        """Sample the arming loop, and return how long it has read occupied.

        `None` when this observation reads it clear, which is also what RESETS
        the run: the value is the age of the first observation in an UNBROKEN
        run of occupied readings, and nothing else. That is the measurement, and
        it is what `docs/CONTRACT.md` says rather than "continuously occupied" --
        this lane samples when it is asked and when it polls, and it cannot
        claim anything about the gaps between.
        """
        if not self.loop.is_occupied():
            self._arming_occupied_since = None
            return None
        now = self._clock()
        if self._arming_occupied_since is None:
            self._arming_occupied_since = now
            return 0.0
        return now - self._arming_occupied_since

    def observe_deactivate_loop(self) -> float | None:
        """Sample the deactivate loop, and return how long it has read occupied.

        The same measurement as `observe_arming_loop`, on the loop before it,
        and `None` when there is no such loop as well as when it reads clear:
        a lane without one has nothing to observe, and
        `deactivate_loop_stuck_occupied` answers `unknown` for it.
        """
        if self.deactivate_loop is None or not self.deactivate_loop.is_occupied():
            self._deactivate_occupied_since = None
            return None
        now = self._clock()
        if self._deactivate_occupied_since is None:
            self._deactivate_occupied_since = now
            return 0.0
        return now - self._deactivate_occupied_since

    @property
    def arming_suppressed(self) -> bool:
        """Whether a held arming interval is open right now."""
        return self._suppressed_since is not None

    def _hold_arming(self, geometry: dict) -> None:
        """Open a held interval, or leave the open one open. Recorded ONCE.

        The start is the event; the turns that follow while both loops still
        read the same are not, because an interval is one thing and a record
        that repeats it every poll is a log nobody can read. What each of
        those turns DOES do is re-read the level, so the hold ends the moment
        it should and not on the next arrival.
        """
        if self._suppressed_since is not None:
            return
        self._suppressed_since = self._clock()
        log.info(
            "lane %s: a vehicle is on the deactivate loop behind the one at the arming loop; "
            "not arming until it backs off",
            self.config.lane_id,
        )
        self.events.record(
            ARMING_SUPPRESSED,
            self.config.lane_id,
            reason=REASON_VEHICLE_TOO_CLOSE,
            geometry_assumed=geometry,
        )
        self.deliver()

    def _release_arming(self, ended_by: str, geometry: dict) -> None:
        """Close the held interval, saying which of its two ends this was.

        `armed`: the deactivate loop cleared with the vehicle still at the
        arming loop, and the lane arms for it on this same turn.
        `arming_loop_cleared`: the vehicle left without being armed for -- it
        backed out, or it followed the car ahead through. Either way the
        record closes; an interval that only ever started would be a car
        nobody photographed and a record that never ends.
        """
        held_for = self._clock() - self._suppressed_since
        self._suppressed_since = None
        self.events.record(
            ARMING_SUPPRESSION_ENDED,
            self.config.lane_id,
            ended_by=ended_by,
            held_for_s=round(held_for, 3),
            geometry_assumed=geometry,
        )
        self.deliver()

    def _identity_detail(self, identity, *, with_region: bool) -> dict:
        """The identity fields a SESSION ACTION carries, and only those.

        EXACTLY ONE identity, which is the shape the platform's `vehicles` row
        now has (`vehicles_exactly_one_identity`). A ticket action carries no
        `plate` key at all rather than `plate: null`: the record says what the
        identity WAS, and a null plate beside a ticket reads as a plate the lane
        failed to get rather than as a lane that was never looking for one.

        These are the only events in this package that have ever carried
        identity text, and they are not on the read contract -- they become
        `POST /lane/sessions/open` and `/close`.

        A PLATE NOBODY COULD READ is the third shape, and an open only: no
        plate key and no ticket key, `plate_unreadable: true` -- the value the
        owner's rule makes of it ("the plate is unreadable, and that is part of
        the ID"). The platform holds such a stay as a car of its own (0036);
        the descriptor and the pictures are what identify it further.
        """
        if identity.ticket_ref:
            return {"ticket_ref": identity.ticket_ref, "identity_kind": "ticket"}
        if not identity.plate:
            return {"plate_unreadable": True, "identity_kind": "unread"}
        detail = {"plate": identity.plate, "identity_kind": "plate"}
        if with_region:
            # The open carries it and the close never has. Kept that way rather
            # than tidied: what the region is FOR is telling one jurisdiction's
            # plate from another's at the moment the vehicle is first recorded,
            # and the platform's close route neither reads it nor stores it.
            detail["plate_region"] = identity.plate_region
        return detail

    @staticmethod
    def _session_descriptor(identity) -> dict:
        """The appearance descriptor an OPEN carries, and only when there is one.

        Beside the identity, not part of it: a descriptor is a READ of the car
        that arrived, and it is kept whichever way the car was identified -- a
        plate the camera read, or a ticket a person completed for a car whose
        plate it could not. The exit's search compares descriptors against
        every open stay, and a ticket stay is a stay.

        NO KEY when there is none, rather than `descriptor: None`, for the
        reason `_identity_detail` gives for the plate: the record says what was
        measured, and a lane whose service does not produce descriptors sends
        exactly the open it sent before this field existed.

        BOTH ENDS. The open carries the entry read's, and the close carries the
        exit read's -- on the CLOSE and on no other channel, because the two
        channels a lane reports on arrive at the platform in no specified
        order and the search that compares the two snapshots inside the close.

        A seam on purpose (`BREAK_DESCRIPTOR=record` in the fail-control): the
        one place the descriptor crosses from a reading into a session action.
        """
        if identity.descriptor is None:
            return {}
        return {"descriptor": identity.descriptor}

    def set_drain(self, drain: Callable[[], None] | None) -> None:
        """Install who drains the outbox -- `LaneRunner` hands its own thread's
        signal here. None restores draining inline."""
        self._drain = drain

    def deliver(self) -> None:
        """The record leaves the box -- through whoever drains the outbox.

        Every path that records and used to call `events.flush()` calls this
        instead, and the difference is WHICH THREAD WAITS. A flush is a
        network call with the client's timeout behind it (5 s by default)
        for every queued session action; against a platform that refuses it
        costs a third of a millisecond, against one that black-holes -- the
        ordinary shape of a site outage -- it costs the whole timeout, per
        action, and it used to cost it ON THE LANE THREAD, after the vend:
        not the car being served, the car behind it, when the queue is
        longest. Under `LaneRunner` the drain is a thread of its own and this
        is a signal. With no drain installed it is the flush itself, so a
        bare controller still returns from `run_once` with the record
        delivered -- the offline suite's shape.

        Resolved at call time, not captured: the fail-controls that remove a
        flush do so by patching `EventQueue.flush` on the class.
        """
        if self._drain is None:
            self.events.flush()
            self.events.flush_pictures()
        else:
            self._drain()

    def _take_pictures(self, count: int) -> list:
        """`count` frames from the lane's camera, to keep with an entry (U7f).
        A camera that fails costs the pictures, never the transit."""
        try:
            return list(self.camera.grab(count))
        except Exception:  # noqa: BLE001 -- a picture is never worth a transit
            log.exception(
                "lane %s: the camera failed taking pictures; none kept", self.config.lane_id
            )
            return []

    def _crossing_identity(self, frames):
        """The identity of the car that crossed with nothing pending, from its
        closing-loop pictures -- what a stay opened for it carries.

        A plate read at the lane's confidence, or "unreadable": the stay is
        opened either way (`_stay_identity` says what it carries, and
        `_unsure_plate` what it keeps of a read below the confidence). The
        appearance descriptor goes with it whenever the read produced one.
        No pictures, or an identifier that fails, is still a car the loops saw
        cross: an unreadable stay with no descriptor. None ONLY when the camera
        says no vehicle was there (`presence: false`, measured against the
        lane's reference view) -- the camera's word over the loop's, as at the
        arming read, so a loop tripped by something that is not a car is no
        phantom occupant."""
        read = VehicleIdentity(plate=None)
        if frames:
            try:
                read = self.identifier.identify(frames)
            except Exception:  # noqa: BLE001 -- a failed read is an unreadable plate
                log.exception("lane %s: identifying a crossing car failed", self.config.lane_id)
        if read.presence is False:
            return None
        # A ticket is asserted, never read: the seam `handle_arrival` keeps.
        # The READ as it came, a plate below the confidence included: the stay
        # makes it "unreadable" and keeps what was seen beside it
        # (`_record_session`).
        return replace(read, ticket_ref=None)

    #: The longest plate text the platform takes (its `PLATE_MAX`): what was
    #: read below the confidence is kept only within it, so a read nobody can
    #: use never costs the car its stay (a refused open is a lost stay).
    UNSURE_PLATE_MAX = 32

    def _unsure_plate(self, identity) -> dict:
        """What the camera read of a plate BELOW the lane's confidence, with that
        confidence, for an unread car's open: `{"unsure_plate", "unsure_plate_
        confidence"}`, or `{}`.

        NOT AN IDENTITY (platform 0036): never looked up here or there, never
        the stay's plate, never compared to a register, never ban-listed. It is
        what was seen, kept for the exit's matching -- dropped, a car read the
        same way at the exit had nothing to be matched on. Nothing for a ticket,
        a plate this lane stands behind, a read that never happened, or a text
        the platform would refuse."""
        if (
            identity.ticket_ref
            or not identity.plate
            or identity.unavailable is not None
            or identity.presence is False
            or plate_read(identity, self.config.confidence_threshold)
        ):
            return {}
        text = identity.plate
        confidence = identity.confidence
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > self.UNSURE_PLATE_MAX
            or not isinstance(confidence, int | float)
            or not 0.0 <= float(confidence) <= 1.0
        ):
            return {}
        return {"unsure_plate": text, "unsure_plate_confidence": round(float(confidence), 4)}

    def _keep_refused_arming(self, decision, frames) -> None:
        """Keep a refused car's arming read, with the decision it belongs to,
        until the next arrival: if that car is let in by a person
        (`take_completion_arming`) or drives in anyway (`_refused_arming_for`),
        the pictures it was identified from go with its stay."""
        self._held_arming = (
            (self.last_decision_at, list(frames))
            if self.config.direction == "entry"
            and not decision.should_vend
            and decision.outcome is not Outcome.NO_VEHICLE
            and frames
            else None
        )

    def _take_held_arming(self) -> list:
        held, self._held_arming = self._held_arming, None
        if held is None or held[0] != self.last_decision_at:
            return []
        return held[1]

    def _refused_arming_for(self, identity) -> list:
        """The arming read of the car this lane last refused, for a crossing
        that may be that car driving in anyway: no arrival since, the refusal
        not completed by anybody, and no plate read on both that says they are
        two cars. Taken once. Not a binding -- the lane cannot prove whose
        crossing this was -- which is why a contradicting plate keeps them out."""
        if (
            self._held_arming is None
            or self.last_decision is None
            or self.last_decision.should_vend
            or self.last_decision_completed_at is not None
        ):
            return []
        refused = plate_read(self.last_decision.identity, self.config.confidence_threshold)
        crossing = plate_read(identity, self.config.confidence_threshold)
        if refused and crossing and refused.upper() != crossing.upper():
            return []
        return self._take_held_arming()

    def take_completion_arming(self, at: str) -> list:
        """The arming read of the car a person's completion let in, for the
        transit that completion began at `at` -- taken once (`complete_vend`
        set it aside). The assisted vend's settle hands it to `resolve_transit`."""
        held, self._completion_arming = self._completion_arming, None
        if held is None or held[0] != at:
            return []
        return held[1]

    def _stay_identity(self, identity):
        """The identity a stay carries: what this lane stands behind.

        A ticket a person or a display asserted, as it was. Otherwise the plate
        when it was read at the lane's confidence (`decision.plate_read`), and
        when it was not, NO plate: "unreadable" is the value, and the open says
        so (`_identity_detail`). A plate read below the threshold never becomes
        a stay's plate -- on the platform a plate is a vehicle, and a misread
        one would land this car on another car's open stay. Everything else
        that was seen -- the descriptor, the make, the colour -- stays."""
        if identity.ticket_ref or plate_read(identity, self.config.confidence_threshold):
            return identity
        return replace(identity, plate=None, plate_region=None)

    def _late_admitted(self, identity) -> bool:
        """Whether the car at the closing loop is the one this lane last
        ADMITTED and held -- vended, but it reached the loop after the window.
        That car was let in: its stay is not a tailgater's, and it is never
        put on a banned list for being slow.

        Same plate as the held vend's, when both were read. When either was
        not, nothing here can tell the two apart, and the crossing is taken
        for the admitted car: a stay with the tailgater flag off loses a ban
        the owner may want, while one wrongly on puts a car this lane let in
        on the banned list. Once per held vend: the first crossing after it is
        the one that may be its car, and a second is not."""
        if (
            self.transit_state != TransitState.HELD.value
            or self.last_decision is None
            or not self.last_decision.should_vend
            or self._late_taken_for == self.transit_since
        ):
            return False
        held = plate_read(self.last_decision.identity, self.config.confidence_threshold)
        crossing = plate_read(identity, self.config.confidence_threshold)
        if held and crossing and held.upper() != crossing.upper():
            return False
        self._late_taken_for = self.transit_since
        return True

    def _banned(self, identity) -> str | None:
        """The ban an arriving car matches, at an entry; None for every other car.

        Asked only of a car the lane would admit, with a descriptor, at a lane
        holding a list and wired to the comparison. A comparison that could not
        be made refuses nobody: it is logged, and the car is decided as before.
        """
        if (
            self.config.direction != "entry"
            or self.banned_matcher is None
            or not identity.descriptor
            or not self.cache.banned
        ):
            return None
        try:
            return self.banned_matcher.match(identity.descriptor, self.cache.banned)
        except Exception:  # noqa: BLE001 -- a failed comparison is not a refusal
            log.exception(
                "lane %s: the banned-car comparison failed; this car is not refused on it",
                self.config.lane_id,
            )
            return None

    def _record_session(
        self,
        identity,
        at: str,
        *,
        confirmation: str,
        exit_pricing=None,
        tailgater: bool = False,
        no_open_command: bool = False,
    ) -> LaneEvent | None:
        """Put the session action on the queue, saying what confirmed it -- and,
        at an exit, what the barrier decided (`local_decision`). Returns the
        entry's open, which its pictures are kept against; None at an exit."""
        lane = self.config.lane_id
        if self.config.direction == "entry":
            # What this lane stands behind, and nothing it does not: a plate
            # read below its confidence is "unreadable" on the stay -- and what
            # was read is kept beside it, as a field that is not an identity.
            unsure = self._unsure_plate(identity)
            identity = self._stay_identity(identity)
            return self.events.record(
                SESSION_OPEN,
                lane,
                **self._identity_detail(identity, with_region=True),
                **self._session_descriptor(identity),
                **unsure,
                at=at,
                entry_confirmation=confirmation,
                # A car no vend let in whose plate was READ (U7f). Only when
                # true, so every other open is the record it always was.
                **({"tailgater": True} if tailgater else {}),
                # A car the loops saw go in that no open command let in, read
                # or not (platform 0036). Only when true.
                **({"no_open_command": True} if no_open_command else {}),
            )
        # WHICH SESSION THIS IS is asked of the platform when the close is
        # DELIVERED, by the transport, on the thread that drains the outbox
        # (`sync.PlatformTransport`) -- never here. Asked here, it was a
        # network call on the lane's thread with the client's timeout behind
        # it: in an outage every exit waited five seconds before the next car
        # was served. The close is recorded without an id, and one the
        # platform cannot be asked about goes out without one, as it always
        # did offline: the platform matches it on the identity.
        session_id = None
        # THE DECISION THE BARRIER MOVED ON rides the close, and no other
        # channel: the platform's close CONSUMES it (0017) -- writes its fee,
        # runs no engine -- so what the screen showed and what the row says are
        # one computation. It is the whole of `exit_pricing.to_detail()`, the
        # same words the `decision` event carries: the status, what it was
        # computed from (the cache's `synced_at`), and for a priced stay the
        # fee, the plan version and the instants it priced between -- what the
        # platform's reconciler re-derives the number from. A close with no
        # decision (an exit that read no plate) carries none, and the platform
        # prices it as it always has.
        self.events.record(
            SESSION_CLOSE,
            lane,
            **self._identity_detail(identity, with_region=False),
            **self._session_descriptor(identity),
            **({"local_decision": exit_pricing.to_detail()} if exit_pricing is not None else {}),
            # What the reader put up for this stay, SEALED as the close is
            # recorded (A2.2): the platform records a held validation only
            # when this is its discounted fee.
            **self._reader_shown(exit_pricing),
            at=at,
            session_id=session_id,
            exit_confirmation=confirmation,
        )
        # AND THE STAY IS CLOSED HERE TOO. Left in the lane's list until the
        # platform's next delta -- or for hours offline -- a car that backed up
        # and pulled forward was priced a second time for the same stay, and
        # the reader put the fee up again.
        if exit_pricing is not None:
            self.cache.drop_stay(exit_pricing.session_id)
        # The car this fee belonged to has gone through: the reader shows
        # nothing until the next exit decision, so the next driver never
        # reads this one's fee.
        self.show_reader(None)

    def _reader_shown(self, exit_pricing) -> dict:
        """`{"reader_shown": ...}` from a screen that keeps what it showed, or {}."""
        seal = getattr(self.reader, "seal", None)
        session_id = getattr(exit_pricing, "session_id", None)
        if seal is None or not session_id:
            return {}
        shown = seal(session_id)
        return {"reader_shown": shown} if shown is not None else {}

    def set_reader_hand(self, hand: Callable[[dict | None], None] | None) -> None:
        """Install who presents to the reader -- `LaneRunner` hands its own
        thread's slot here. None restores presenting inline."""
        self._reader_hand = hand

    def show_reader(self, record: dict | None) -> None:
        """The exit decision's record -- or `None`, nothing to show -- to the reader.

        Under `LaneRunner` this is a hand-off to the reader's own thread,
        returned from at once: a reader is a device on the network, and the
        barrier does not wait on one. With no hand installed -- the bare
        controller of the tests and the demo -- the reader is called here, and
        a reader that raises costs its own screen and nothing else: the vend
        and the record go on without it.
        """
        # RECORDED FIRST, and whether or not a reader is fitted: what goes on
        # the read contract is what this lane decided to put in front of the
        # driver, and an exit with a display and no reader still has a fee.
        self.exit_screen = (
            (self.last_decision_at, record)
            if record is not None and self.last_decision_at is not None
            else None
        )
        if self.reader is None:
            return
        if self._reader_hand is not None:
            self._reader_hand(record)
            return
        try:
            self.reader.present_exit(record)
        except Exception:  # noqa: BLE001 -- a screen may not stop a barrier
            log.exception("the reader could not be shown the exit decision; the lane goes on")

    def run_once(self, timeout: float | None = None) -> Decision | None:
        """Wait for one vehicle and serve it. None if none arrived in time.

        Arming is where the two loops before the barrier do their work: BOTH
        must read occupied together, so the object has to span the gap between
        them. One loop alone arms nothing, and it is RECORDED -- a lane being
        worked by somebody laying metal on a loop shows up as a pattern in the
        log rather than as silence.
        """
        if not self.loop.wait_for_vehicle(timeout=timeout):
            # SAMPLED ON EVERY POLL, arrival or not. A loop that is reading
            # occupied with nothing on it never produces an arrival, so the
            # timeout branch is the one an actually-stuck loop takes for ever --
            # and it is the branch where the observation matters most.
            self.observe_arming_loop()
            self.observe_deactivate_loop()
            if self._suppressed_since is not None:
                # THE HELD INTERVAL, RE-READ ON THE IDLE TURN, because a loop
                # driver that reports an ARRIVAL once per vehicle will never
                # report the held one again: this turn is the only place its
                # hold can end. Two ends. The vehicle left the arming loop --
                # backed out, or followed the car ahead -- and the interval
                # closes the second way, now, not when the next car comes.
                # Or the deactivate loop cleared with the vehicle still there,
                # and the lane arms for it on this turn exactly as it would
                # have on the arrival it already served.
                geometry = self.config.loops.as_published()
                if not self.loop.is_occupied():
                    self._release_arming(SUPPRESSION_ENDED_ARMING_LOOP_CLEARED, geometry)
                elif not self.held_by_deactivate_loop(self.deactivate_loop):
                    self._release_arming(SUPPRESSION_ENDED_ARMED, geometry)
                    return self._arm(geometry)
            # THE LOOPS AFTER THE GATE, on the idle turns. Every turn that
            # serves no arrival asks them whether anything crossed since they
            # were last read, and a lane with its barrier up is a lane where
            # every turn is one of these.
            self.observe_closing_loops()
            return None
        self.observe_arming_loop()
        self.observe_deactivate_loop()

        geometry = self.config.loops.as_published()
        if self.held_by_deactivate_loop(self.deactivate_loop):
            # A vehicle is at the arming loop and another is on the deactivate
            # loop behind it. The boom stays down: an arm now is a vend the
            # second car follows through. Recorded once per interval, and the
            # LEVEL is read again on every turn -- this branch when the driver
            # reports the vehicle again, the idle branch above when it does
            # not -- so the hold ends the moment the second car backs off and
            # not on some later arrival. An edge here would leave both cars
            # waiting for each other.
            self._hold_arming(geometry)
            return None
        if self._suppressed_since is not None:
            # The deactivate loop cleared with the vehicle still at the arming
            # loop: the first way an interval ends, and the lane arms for it
            # now, through the same checks as any arrival.
            self._release_arming(SUPPRESSION_ENDED_ARMED, geometry)
        return self._arm(geometry)

    def _arm(self, geometry: dict) -> Decision | None:
        """A vehicle is at the arming loop and nothing holds it: arm, or say why not.

        The tail of `run_once` in its own method, because it is now reached
        from two places -- the arrival the driver reported, and the idle turn
        on which a held vehicle's hold ended -- and the arming check must be
        the same check on both.
        """
        lane = self.config.lane_id
        if not self.arming_complete(self.arming_loop_b):
            log.info("lane %s: one arming loop only, not arming", lane)
            self.events.record(
                ARMING_INCOMPLETE,
                lane,
                reason=REASON_ARMING_INCOMPLETE,
                geometry_assumed=geometry,
            )
            self.deliver()
            return None

        self.events.record(ARMED, lane, geometry_assumed=geometry)
        return self.handle_arrival()

    def run_forever(self, timeout: float | None = 1.0) -> None:  # pragma: no cover
        while True:
            self.run_once(timeout=timeout)
