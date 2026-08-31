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
from dataclasses import dataclass, replace

from .config import LaneConfig
from .contract import TransitState
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
    REASON_LOOP_DRIVER_TIMEOUT,
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
        #: Whether an assisted vend's settle has ever exceeded this lane's own
        #: deadline. `closing_loops_never_firing` is derived from it.
        self._loop_driver_timed_out = False

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
        )
        self.events.record(
            "decision",
            lane,
            outcome=decision.outcome.value,
            reason=decision.reason,
            fallback=decision.fallback.value if decision.fallback else None,
            rate_plan=decision.rate_plan,
        )
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
            )

        # Best effort, and after the barrier has already been told what to do.
        # Nothing above this line waits on the network.
        self.events.flush()
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
        self.events.flush()
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
        """Only A-then-B confirms. Nothing else is folded into it."""
        return crossing is ClosingSequence.FORWARD

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

    def _transit(self, state: TransitState, at: str) -> None:
        """Move the published transit, beside the event that says the same thing.

        One call per outcome and never a shared one, for the reason every other
        name in this file is unshared: an entry that was backed out of and one
        that was merely never confirmed are different facts, and a helper that
        collapsed them would be the first place they got confused.
        """
        self.transit_state = state.value
        self.transit_since = at

    @staticmethod
    def _identity_kind(identity) -> str:
        """What KIND of identity this is: read, or asserted.

        Derived from the identity itself rather than passed alongside it, so a
        record cannot say `plate` about a ticket. It is published on the pending
        event; the ticket REFERENCE is not -- see `begin_transit`.
        """
        return "ticket" if identity.ticket_ref else "plate"

    def _settle_transit(self, identity, at: str) -> None:
        """Record the pending entry, then let the closing loops decide its fate.

        Two halves, called as one here and separately by the assisted vend --
        which has to ANSWER its caller once the pending entry exists and then go
        on waiting for the crossing, because the confirmation window is ten
        seconds and an HTTP route that held one open for it would be reporting
        a settled transit as though it were an immediate one.
        """
        self.begin_transit(identity, at)
        self.resolve_transit(identity, at)

    def begin_transit(self, identity, at: str) -> None:
        """The vend created a PENDING entry. Nothing has decided its fate yet."""
        lane = self.config.lane_id
        names = _ENTRY if self.config.direction == "entry" else _EXIT
        geometry = self.config.loops.as_published()

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

    def resolve_transit(self, identity, at: str, *, claim=None) -> None:
        """What the loops after the barrier made of the pending entry.

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
                names.confirmed, lane, reason=REASON_FORWARD, at=at, geometry_assumed=geometry
            )
            self._transit(TransitState.CONFIRMED, at)
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
            self._transit(TransitState.BACKED_OUT, at)
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
            self._record_session(identity, at, confirmation=HELD)

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
        self.events.flush()

    @property
    def loop_driver_timed_out(self) -> bool:
        """Whether an assisted settle has ever exceeded this lane's deadline."""
        return self._loop_driver_timed_out

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
        """
        if identity.ticket_ref:
            return {"ticket_ref": identity.ticket_ref, "identity_kind": "ticket"}
        detail = {"plate": identity.plate, "identity_kind": "plate"}
        if with_region:
            # The open carries it and the close never has. Kept that way rather
            # than tidied: what the region is FOR is telling one jurisdiction's
            # plate from another's at the moment the vehicle is first recorded,
            # and the platform's close route neither reads it nor stores it.
            detail["plate_region"] = identity.plate_region
        return detail

    def _record_session(self, identity, at: str, *, confirmation: str) -> None:
        """Put the session action on the queue, saying what confirmed it."""
        lane = self.config.lane_id
        if self.config.direction == "entry":
            self.events.record(
                SESSION_OPEN,
                lane,
                **self._identity_detail(identity, with_region=True),
                at=at,
                entry_confirmation=confirmation,
            )
            return
        # Ask the platform which session this is, while the answer is still
        # unambiguous. If it cannot be reached the close goes out without an id
        # and the platform falls back to matching on the identity -- which
        # works, and is merely less precise.
        #
        # The lookup takes a PLATE, so a ticket identity skips it. That is not a
        # gap that can be reached in this version: the vend route is the only
        # thing that sets a ticket and it serves ENTRY lanes only, and no
        # identifier may supply one. The close still goes out carrying the
        # ticket, and the platform finds the open stay from it.
        session_id = None
        if self.session_lookup is not None and identity.plate:
            found = self.session_lookup(identity.plate)
            if found:
                session_id = found.get("session", {}).get("id")
        self.events.record(
            SESSION_CLOSE,
            lane,
            **self._identity_detail(identity, with_region=False),
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
            # SAMPLED ON EVERY POLL, arrival or not. A loop that is reading
            # occupied with nothing on it never produces an arrival, so the
            # timeout branch is the one an actually-stuck loop takes for ever --
            # and it is the branch where the observation matters most.
            self.observe_arming_loop()
            return None
        self.observe_arming_loop()

        lane = self.config.lane_id
        geometry = self.config.loops.as_published()
        if not self.arming_complete(self.arming_loop_b):
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
