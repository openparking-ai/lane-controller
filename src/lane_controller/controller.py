"""The lane sequence.

    arming loops -> grab frames -> identify -> decide -> vend -> closing loops

The barrier still closes itself on its own closing loop; the controller has no
part in that and no way to interfere with it. The two loops at the end of the
line above are a different pair -- after the gate, read and never driven -- and
they are what says whether a vehicle actually went through.

THE TICKET IS NOT THE ENTRY. A vend creates a PENDING entry. Two loops after
the barrier, crossed A then B inside the confirmation window, promote it to a
session. B then A is somebody backing out and closes it with its own reason.
The window elapsing with nothing is a third answer -- HELD -- and it is neither
of the other two: voiding it silently re-creates the abandoned-ticket fraud,
and turning it into a session is the phantom occupant that fills a garage on
paper before it fills in concrete.

A lane with no closing loops installed is not refused. It opens its sessions as
it always did, and every one of them carries `unconfirmable` and an
`entry_unconfirmable` event saying nothing could confirm it -- which is the
whole difference between a weakness that is configured and a weakness that is
recorded.

The whole sequence runs against simulated implementations of all three seams,
which is why `tests/` needs no hardware.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from .config import LaneConfig
from .decision import Decision, DecisionCache, Outcome, decide
from .events import EventQueue
from .interfaces import (
    CameraFeed,
    ClosingLoops,
    ClosingSequence,
    LoopInput,
    VehicleIdentifier,
    VendOutput,
)
from .sync import (
    ARMED,
    ARMING_INCOMPLETE,
    CONFIRMED,
    ENTRY_BACKED_OUT,
    ENTRY_CONFIRMED,
    ENTRY_HELD,
    ENTRY_PENDING,
    ENTRY_UNCONFIRMABLE,
    EXIT_BACKED_IN,
    EXIT_CONFIRMED,
    EXIT_HELD,
    EXIT_PENDING,
    EXIT_UNCONFIRMABLE,
    HELD,
    REASON_ARMING_INCOMPLETE,
    REASON_FORWARD,
    REASON_NO_CLOSING_LOOPS,
    REASON_REVERSE,
    REASON_WINDOW_ELAPSED,
    SESSION_CLOSE,
    SESSION_OPEN,
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

log = logging.getLogger(__name__)


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
        cache: DecisionCache | None = None,
        events: EventQueue | None = None,
        clock: Callable[[], float] = time.time,
        session_lookup: Callable[[str], dict | None] | None = None,
    ) -> None:
        self.config = config
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
        # Injected rather than reached for, so a lane can be built with no
        # platform at all and the tests need no network.
        self.session_lookup = session_lookup

    def handle_arrival(self) -> Decision:
        """One vehicle, from arming to vend. Assumes the loop has already armed."""
        lane = self.config.lane_id

        frames = self.camera.grab(self.config.camera.frames_per_read)
        self.events.record("frames_captured", lane, count=len(frames), camera=self.camera.camera_id)

        identity = self.identifier.identify(frames)
        self.events.record(
            "vehicle_identified",
            lane,
            plate=identity.plate,
            confidence=identity.confidence,
            make=identity.make,
            model=identity.model,
            color=identity.color,
        )

        decision = decide(
            identity,
            self.cache,
            confidence_threshold=self.config.confidence_threshold,
        )
        self.events.record(
            "decision",
            lane,
            outcome=decision.outcome.value,
            reason=decision.reason,
            fallback=decision.fallback.value if decision.fallback else None,
            rate_plan=decision.rate_plan,
        )

        if decision.should_vend:
            self.vend.vend(decision.reason)
            self.events.record("vended", lane, reason=decision.reason, plate=identity.plate)

            # The vend is where the ticket comes out, and the ticket is NOT the
            # entry. What goes on the queue here is a PENDING entry; the loops
            # after the gate decide whether it becomes a session.
            #
            # The timestamp is the lane's, not the platform's, and it is taken
            # HERE rather than after the confirmation: the car arrived when it
            # arrived, whatever time the server eventually hears about it, and
            # pricing a stay by when the network came back would be wrong.
            at = to_iso(self._clock())
            self._settle_transit(identity, at)

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
                plate=identity.plate,
                confidence=identity.confidence,
            )

        # Best effort, and after the barrier has already been told what to do.
        # Nothing above this line waits on the network.
        self.events.flush()
        return decision

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
        """Only A-then-B confirms. Nothing else is folded into it."""
        return crossing is ClosingSequence.FORWARD

    @staticmethod
    def _refutes(crossing: ClosingSequence) -> bool:
        """B-then-A: a vehicle backing out. An observation, not an absence."""
        return crossing is ClosingSequence.REVERSE

    @staticmethod
    def _arming_complete(loop_b: LoopInput | None) -> bool:
        """Both arming loops occupied together, or a lane that has only one."""
        return loop_b is None or loop_b.is_occupied()

    def _settle_transit(self, identity, at: str) -> None:
        """Record the pending entry, then let the closing loops decide its fate."""
        lane = self.config.lane_id
        names = _ENTRY if self.config.direction == "entry" else _EXIT
        geometry = self.config.loops.as_published()

        self.events.record(
            names.pending,
            lane,
            plate=identity.plate,
            plate_region=identity.plate_region,
            at=at,
            geometry_assumed=geometry,
        )

        if self.closing_loops is None:
            # No closing loops at this site, so nothing here can confirm or
            # refute anything. Saying so on every single vehicle is the point:
            # the previous generation of this weakness was true from the day it
            # was written, published in pieces across three documents, and
            # never once stated in a single place a reader would reach.
            self.events.record(
                names.unconfirmable,
                lane,
                reason=REASON_NO_CLOSING_LOOPS,
                at=at,
                geometry_assumed=geometry,
            )
            self._record_session(identity, at, confirmation=UNCONFIRMABLE)
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
        started = self._clock()
        crossing = self.closing_loops.wait_for_sequence(window)
        elapsed = self._clock() - started

        if self._confirms(crossing) and self._within_window(elapsed, window):
            self.events.record(
                names.confirmed, lane, reason=REASON_FORWARD, at=at, geometry_assumed=geometry
            )
            self._record_session(identity, at, confirmation=CONFIRMED)
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
            return

        # No confirmation inside the window: nothing crossed, or a FORWARD
        # arrived after it. At an ENTRY that is NEVER silently voided -- that is
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
            self._record_session(identity, at, confirmation=HELD)

    def _record_session(self, identity, at: str, *, confirmation: str) -> None:
        """Put the session action on the queue, saying what confirmed it."""
        lane = self.config.lane_id
        if self.config.direction == "entry":
            self.events.record(
                SESSION_OPEN,
                lane,
                plate=identity.plate,
                plate_region=identity.plate_region,
                at=at,
                entry_confirmation=confirmation,
            )
            return
        # Ask the platform which session this is, while the answer is still
        # unambiguous. If it cannot be reached the close goes out without an id
        # and the platform falls back to matching on the plate -- which works,
        # and is merely less precise.
        session_id = None
        if self.session_lookup is not None:
            found = self.session_lookup(identity.plate)
            if found:
                session_id = found.get("session", {}).get("id")
        self.events.record(
            SESSION_CLOSE,
            lane,
            plate=identity.plate,
            at=at,
            session_id=session_id,
            exit_confirmation=confirmation,
        )

    def run_once(self, timeout: float | None = None) -> Decision | None:
        """Wait for one vehicle and serve it. None if none arrived in time.

        Arming is where the two loops before the barrier do their work: BOTH
        must read occupied together, so the object has to span the gap between
        them. One loop alone arms nothing, and it is RECORDED -- a lane being
        worked by somebody laying metal on a loop shows up as a pattern in the
        log rather than as silence.
        """
        if not self.loop.wait_for_vehicle(timeout=timeout):
            return None

        lane = self.config.lane_id
        geometry = self.config.loops.as_published()
        if not self._arming_complete(self.arming_loop_b):
            log.info("lane %s: one arming loop only, not arming", lane)
            self.events.record(
                ARMING_INCOMPLETE,
                lane,
                reason=REASON_ARMING_INCOMPLETE,
                geometry_assumed=geometry,
            )
            self.events.flush()
            return None

        self.events.record(ARMED, lane, geometry_assumed=geometry)
        return self.handle_arrival()

    def run_forever(self, timeout: float | None = 1.0) -> None:  # pragma: no cover
        while True:
            self.run_once(timeout=timeout)
