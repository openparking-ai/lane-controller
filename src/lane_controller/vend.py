"""The assisted vend: the one route on this contract that opens a barrier.

**THE INVARIANT.** `POST /v1/lane/vend` is a NEW ROUTE TO A VEND on the exact
boundary every outside reviewer of this project has named, and it is safe only
because of two properties that live here rather than in whatever calls it:

  * **THE LANE APPLIES ITS OWN REFUSALS.** Presence is read off the arming loop
    NOW, not taken from the caller's body. The malfunction table is this lane's
    own. The arming geometry is the same function `run_once` calls before it
    arms. Every one of them is derived from the code an ordinary arrival goes
    through, so a refusal cannot exist on one path and not the other.

  * **THE LANE WRITES THE COMPLETED IDENTITY BEFORE THE RELAY MOVES.** That is
    `LaneController.complete_vend`, and its order is asserted by a test that
    records every call.

If the caller asserted the completion and this lane trusted it, the caller
would be `POST /sessions/open` with a microphone attached, and the fraud the
loops and the presence gate exist to close would be open one layer down.

**WHAT THIS LANE DOES NOT DO.** It verifies nothing about the ticket beyond its
shape -- no signature, no expiry, no issuer. It holds no key and mints no
ticket. The agent does both, and this lane's whole claim about a ticket is that
it looked like one and that a named authority said to open.

**WHAT NOTHING MEASURES.** The boom. `boom_did_not_rise` is `no_source` on this
lane's own health surface, so the answer is `vend_commanded`, never `opened`.
"""

from __future__ import annotations

import logging
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime

from .contract import (
    VEND_BLOCKING,
    VEND_IDENTITY_KINDS,
    HealthState,
    TransitState,
    VendAuthority,
    VendCommanded,
    VendRefusal,
    VendRefused,
    is_idempotency_key,
    is_ticket_ref,
)
from .decision import Outcome, decide
from .interfaces import VehicleIdentity

log = logging.getLogger(__name__)

#: How many idempotency keys and their answers are held, for this run.
#:
#: BOUNDED, and published in `docs/CONTRACT.md` rather than left to be
#: discovered, for the reason the read window's depth is published: an
#: unbounded store on a process that runs for months is a leak, and a bound
#: nobody states is a promise that quietly stops being kept. It is the same
#: number as the read window's depth, so a consumer holds one idea of how far
#: back this lane remembers anything.
#:
#: Only ACCEPTED vends are held. A refusal is not an answer to replay: every
#: one of them is about the world at the moment of the call -- a loop reading,
#: an active malfunction -- and freezing one against a key would refuse a
#: caller for ever on a condition that has already passed.
MAX_HELD_KEYS = 256

#: How many BYTES of keys and answers the store may hold, for this run.
#:
#: A COUNT ALONE IS NOT A BOUND ON MEMORY -- 256 entries of a key a caller chose
#: the size of is 256 times whatever that caller picked. The key now has a shape
#: (`is_idempotency_key`, the ticket's alphabet and bounds), so the two bounds
#: agree to within a small constant; both are here because the count is the one
#: a consumer reasons about and the bytes are the one the machine does, and a
#: future key shape cannot quietly widen the second by changing the first.
MAX_HELD_BYTES = 64 * 1024


class BadVendRequest(Exception):
    """The request is malformed. A 400, and never a 409.

    The difference matters to a caller: a 409 says this lane refused a
    well-formed completion for a reason about the lane or the vehicle, and it
    is worth retrying when that reason clears. A 400 says the caller is broken
    and retrying will not help.
    """


@dataclass(frozen=True, slots=True)
class VendRequest:
    """A parsed, well-formed completion. Nothing here has been AUTHORISED yet."""

    authorised_by: VendAuthority
    ticket_ref: str
    decision_at: str
    idempotency_key: str


def parse(body: dict, idempotency_key: str | None) -> VendRequest:
    """Refuse anything this contract does not name, and say which field.

    Every closed set is compared against the ENUM, so a member added to one
    arrives here without anything being remembered.
    """
    if not isinstance(body, dict):
        raise BadVendRequest("the body must be a JSON object")

    if not is_idempotency_key(idempotency_key):
        # REQUIRED, and there is no generated fallback. A key this lane invented
        # would be unique per request, which is the same as having none: the
        # caller's retry would vend a second time.
        #
        # AND IT HAS A SHAPE, the ticket's own. It used to be any string that
        # was not blank: 43 characters of a name, a telephone number and a card
        # number were a legal key, and a 10 kB one was too. The key is the
        # caller's bookkeeping and this lane has no opinion about what it means,
        # so the only thing it can require is that it is an opaque token of a
        # size a lane can hold.
        raise BadVendRequest(
            "an Idempotency-Key header is required, and it must be 6 to 64 characters "
            "of A-Z, 0-9 and hyphen: it is an opaque token to this lane and is held "
            "only in this process's idempotency store"
        )

    authorities = tuple(authority.value for authority in VendAuthority)
    if body.get("authorised_by") not in authorities:
        raise BadVendRequest(f"authorised_by must be one of {', '.join(authorities)}")

    identity = body.get("identity")
    if not isinstance(identity, dict):
        raise BadVendRequest("identity must be an object")
    if identity.get("kind") not in VEND_IDENTITY_KINDS:
        raise BadVendRequest(
            f"identity.kind must be one of {', '.join(VEND_IDENTITY_KINDS)}; "
            "a plate is what the camera reads and is not something a caller may assert"
        )
    ticket_ref = identity.get("ticket_ref")
    if not is_ticket_ref(ticket_ref):
        # The VALUE is not named in the refusal, and the reason is not that it
        # is a secret -- it is not one. A `ticket_ref` is an OPAQUE IDENTIFIER:
        # it identifies one stay, the platform publishes it in full to any
        # holder of an operator token, and both repositories describe it the
        # same way. The secret is the SIGNED TOKEN the agent will mint, which
        # never leaves the agent and never reaches this lane.
        #
        # What it IS is personal data for as long as the stay exists, and a
        # refusal message is the one place a value reaches a log without
        # anybody deciding to put it there.
        raise BadVendRequest(
            "identity.ticket_ref must be 6 to 64 characters of A-Z, 0-9 and hyphen; "
            "this lane checks a ticket's shape and nothing else"
        )

    decision_at = body.get("decision_at")
    if not _instant(decision_at):
        raise BadVendRequest(
            "decision_at must be the ISO 8601 `at` of the decision being completed, "
            "with a UTC offset, exactly as GET /v1/lane/state published it"
        )

    return VendRequest(
        authorised_by=VendAuthority(body["authorised_by"]),
        ticket_ref=ticket_ref,
        decision_at=decision_at,
        idempotency_key=idempotency_key,
    )


def _instant(value) -> datetime | None:
    """An aware datetime, or None. A naive timestamp is not an instant.

    The same rule `contract._iso_utc` applies to everything this lane
    publishes: assuming a naive time is UTC is how a lane in one timezone and a
    caller in another come to disagree about when a car arrived.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


class AssistedVend:
    """The refusals, the order they are applied in, and the one vend behind them.

    Held by `LaneService` and reached only through `POST /v1/lane/vend`.
    """

    def __init__(self, service) -> None:
        self._service = service
        self._state = threading.Lock()
        self._in_progress = False
        self._answers: OrderedDict[str, dict] = OrderedDict()
        #: The thread settling the current transit, exposed so a test can join
        #: it. A route that answered before the crossing had been waited for
        #: and then left the wait unobservable would be untestable by
        #: construction.
        self.settling: threading.Thread | None = None

    # --- what the lane refuses, in order ---------------------------------

    def complete(self, request: VendRequest) -> tuple[int, dict]:
        """`(status, body)`. 202 for a commanded vend, 409 for a named refusal."""
        # BEFORE every refusal, because a replay is not a new attempt. The car
        # that was on the loop when the first call vended has driven off by the
        # time a retry arrives, so a replay checked against the loop would be
        # answered `no_vehicle` -- a caller told its own successful vend failed.
        held = self._held(request.idempotency_key)
        if held is not None:
            return 202, held

        refusal = self._refuse(request)
        if refusal is not None:
            return 409, refusal.to_dict()

        return self._accept(request)

    def _refuse(self, request: VendRequest) -> VendRefused | None:
        """The seven refusals, in the order the contract publishes them.

        Each is derived from the code an ordinary arrival goes through. None of
        them is a second copy, and `scripts/vend_fail_control.py` proves it by
        perturbing the shared function and requiring the route's answer to move
        with it.
        """
        controller = self._service.controller

        # 1. no_vehicle -- through `decide()`, from a presence read NOW.
        #
        # The loop is asked at the moment of the call. Nothing in the body is
        # consulted: a caller that could assert presence is a caller that can
        # open a barrier for a lane with nothing in front of it, which is the
        # whole fraud this project has spent its rounds on.
        probe = VehicleIdentity(plate=None, presence=controller.loop.is_occupied())
        if (
            decide(
                probe, controller.cache, confidence_threshold=controller.config.confidence_threshold
            ).outcome
            is Outcome.NO_VEHICLE
        ):
            return VendRefused(
                code=VendRefusal.NO_VEHICLE.value,
                error="the arming loop reads unoccupied; there is no vehicle to admit",
            )

        # 2. malfunction_active -- this lane's own health table, and only the
        # PUBLISHED SUBSET `VEND_BLOCKING`: the five codes about the physical
        # act of opening safely. Every other code, including every one this
        # build measures, no longer refuses a completion, because they concern
        # the reading and the record rather than the barrier -- and the assisted
        # vend exists for the driver whose reading failed.
        #
        # `NEVER_ALARM` is not tested here any more and does not need to be:
        # `reference_not_recognised` is not in the subset, so the code that
        # covers an ordinary car arriving cannot refuse one. The two sets are
        # compared in `tests/test_lane_contract.py`, both ways.
        for code, state in sorted(
            self._service.derived_states().items(), key=lambda item: item[0].value
        ):
            if state is HealthState.ACTIVE and code in VEND_BLOCKING:
                return VendRefused(
                    code=VendRefusal.MALFUNCTION_ACTIVE.value,
                    error=f"this lane has an active malfunction: {code.value}",
                    malfunction=code.value,
                )

        # 3. geometry_incomplete -- `run_once`'s own check, not a copy of it.
        if not controller.arming_complete(controller.arming_loop_b):
            return VendRefused(
                code=VendRefusal.GEOMETRY_INCOMPLETE.value,
                error=(
                    "one arming loop only is occupied; an object that cannot span the gap "
                    "does not arm this lane and does not complete one either"
                ),
            )

        # 4. decision_in_future, then decision_stale -- a completion is an
        # answer to a driver who is at the barrier NOW.
        #
        # ONE CLOCK AGES A COMPLETION and it is this lane's. `decision_at` is
        # stamped by `handle_arrival` from `self._clock()`, and the subtraction
        # below reads the same clock, so a caller's clock never enters it.
        max_age = controller.config.completion_max_age_s
        age = (
            datetime.fromtimestamp(controller.now(), tz=UTC) - _instant(request.decision_at)
        ).total_seconds()
        if age < 0:
            # `age > max_age` has no lower bound of its own, so a lane whose
            # clock stepped BACKWARDS would accept a decision of any age at all:
            # the older it got the more negative the age became. A negative age
            # is never a fresher decision.
            return VendRefused(
                code=VendRefusal.DECISION_IN_FUTURE.value,
                error=(
                    f"the decision named is {-age:.0f}s AHEAD of this lane's clock; a lane "
                    "that cannot say how old a decision is does not complete one"
                ),
            )
        if age > max_age:
            return VendRefused(
                code=VendRefusal.DECISION_STALE.value,
                error=(
                    f"the decision named is {age:.0f}s old and this lane completes one for at "
                    f"most {max_age:.0f}s ([lane] completion_max_age_s)"
                ),
            )

        # 5. decision_mismatch -- and a lane that has decided NOTHING is the
        # same fact to a caller: there is no decision here to complete.
        #
        # The INSTANTS are compared, not the strings. A caller that parsed the
        # `at` this lane published and re-serialised it names the same moment,
        # and refusing it for a `Z` where this lane wrote `+00:00` would be a
        # completion refused over a formatting difference.
        last = _instant(controller.last_decision_at)
        if last is None or last != _instant(request.decision_at):
            return VendRefused(
                code=VendRefusal.DECISION_MISMATCH.value,
                error=(
                    "decision_at is not the moment of this lane's last decision; read "
                    "GET /v1/lane/state and complete the decision it publishes"
                ),
            )

        # 6. already_completed -- the decision named is this lane's own, and it
        # has been consumed. ONE DECISION, ONE VEND: a second completion of one
        # arrival is a second ticket, a second billable stay and a second
        # occupant for one car, and the key it carries makes no difference --
        # the commonest idempotency bug there is regenerates the key on retry.
        # The only way to another vend is another arrival, which is another
        # decision. `GET /v1/lane/state` publishes `decision.completed` so a
        # consumer can see this without discovering it here.
        if controller.last_decision_completed_at is not None:
            return VendRefused(
                code=VendRefusal.ALREADY_COMPLETED.value,
                error=(
                    "the decision named has already been completed; a decision is one case "
                    "and one case is one vend, whatever Idempotency-Key this call carries"
                ),
            )

        # 7. not_completable -- there is nothing to complete.
        outcome = controller.last_decision.outcome
        if outcome is Outcome.ALLOW:
            return VendRefused(
                code=VendRefusal.NOT_COMPLETABLE.value,
                error="the last decision was `allow`; it has already vended",
            )
        if outcome is Outcome.DENY and request.authorised_by is not VendAuthority.HUMAN_OPEN_NOW:
            return VendRefused(
                code=VendRefusal.NOT_COMPLETABLE.value,
                error=(
                    "the last decision was `deny`, which is a rule; only `human_open_now` "
                    "overrides one, and it is recorded as an override"
                ),
            )
        return None

    # --- the vend --------------------------------------------------------

    def _accept(self, request: VendRequest) -> tuple[int, dict]:
        """Take the lane, write the identity, move the relay, answer.

        `busy` is applied HERE, last of the seven, because it is the only
        refusal that is about this lane's own occupancy rather than about the
        vehicle or the case -- and because taking the lane before the case has
        been judged would make an already-refused call block the next one.
        """
        controller = self._service.controller
        with self._state:
            if self._in_progress:
                return 409, VendRefused(
                    code=VendRefusal.BUSY.value,
                    error="a vend is in progress on this lane",
                ).to_dict()
            # Re-checked under the lock. Two concurrent calls carrying one key
            # both pass the fast path above, and without this the second would
            # be answered `busy` for a vend that is its own.
            held = self._answers.get(request.idempotency_key)
            if held is not None:
                return 202, held
            self._in_progress = True

        try:
            outcome = controller.last_decision.outcome
            assisted = {
                # THE KIND, never the reference. See `complete_vend`.
                "identity_kind": "ticket",
                "authorised_by": request.authorised_by.value,
                # THE COMPLETION'S OWN IDENTIFIER, minted here, and NOT the
                # caller's `Idempotency-Key`.
                #
                # The key used to be recorded on this event, which reaches
                # `GET /v1/lane/events` and the platform's `events` table --
                # append-only by grant there, so whatever a caller wrote could
                # never be removed by anybody. A caller using the ticket as its
                # key published the ticket; a caller using a name and a
                # telephone number published those. The contract's own sentence
                # about that route ("no plate text goes in it, on any route of
                # this contract") cannot be true of a field the caller writes.
                #
                # This value is unrelated to the key and to the ticket: it is
                # random, it identifies this completion in the record, and it
                # is the lane's to mint.
                "completion_id": secrets.token_hex(8),
                "decision_at": request.decision_at,
                # A human overturning a rule is recorded as one, by name, on
                # the event that is written before the barrier moves. `null`
                # on every other completion rather than absent, so a reader
                # cannot mistake a missing key for "not an override".
                "override_of": outcome.value if outcome is Outcome.DENY else None,
            }
            identity = VehicleIdentity(
                # NO PLATE. This lane did not read one, and inventing one here
                # would put a measurement nobody made into the money record.
                plate=None,
                presence=True,
                ticket_ref=request.ticket_ref,
            )
            at = controller.complete_vend(
                identity, authorised_by=request.authorised_by.value, assisted=assisted
            )
            body = VendCommanded(
                event_cursor=controller.events.cursor,
                # What it IS at the moment of the answer: a pending entry, made
                # by the vend, waiting on a crossing that has not happened yet.
                transit=TransitState.PENDING.value,
            ).to_dict()
            self._hold(request.idempotency_key, body)
        except Exception:
            with self._state:
                self._in_progress = False
            raise

        # The crossing is waited for OFF this route. The window is ten seconds
        # by default and a caller holding an HTTP connection open for it would
        # be told the outcome of a transit the answer says is pending.
        self.settling = threading.Thread(
            target=self._settle,
            args=(identity, at),
            name="assisted-vend-settle",
            daemon=True,
        )
        self.settling.start()
        return 202, body

    def _settle(self, identity, at: str) -> None:
        """Resolve the transit UNDER THIS LANE'S OWN DEADLINE.

        `_in_progress` used to clear in a `finally`, which clears it if
        `resolve_transit` returns or raises -- and never if it does not return.
        `resolve_transit` blocks in `closing_loops.wait_for_sequence(window)`,
        and `ClosingLoops` only ASKS an implementation to bound itself. Its own
        comment already says why that is not enough: "an obligation on the other
        side of a seam is a comment, not a check". A loop driver that answered
        LATE was handled; one that never answered left this lane refusing every
        assisted vend `busy` until a restart, with `transit` reading `pending`
        for ever -- which is also what a legitimate in-window transit reads, so
        nothing on any surface said which of the two it was.

        The bound is the confirmation window plus `[lane] settle_grace_s`. Past
        it the transit is `unconfirmable` with reason `loop_driver_timeout`, and
        `closing_loops_never_firing` -- which the contract said nobody measured
        -- becomes MEASURED from exactly this one event.

        THE HUNG THREAD IS NOT KILLED, because a Python thread cannot be. It is
        ABANDONED: the claim below is taken by whichever of the two arrives
        first, so a driver that returns an hour late finds the transit already
        resolved and records nothing. The lane recovers; the leaked thread is
        named in `docs/CONTRACT.md` rather than pretended away.
        """
        controller = self._service.controller
        claim = _Once()
        deadline = controller.config.loops.confirmation_window_seconds + (
            controller.config.settle_grace_s
        )

        def resolve() -> None:
            try:
                controller.resolve_transit(identity, at, claim=claim)
            except Exception:  # noqa: BLE001
                # A settling thread that died silently would leave the transit
                # `pending` for ever with nothing anywhere saying why.
                log.exception("the assisted vend's transit could not be settled")

        worker = threading.Thread(target=resolve, name="assisted-vend-resolve", daemon=True)
        worker.start()
        worker.join(deadline)
        try:
            if worker.is_alive() and claim.take():
                controller.transit_timed_out(identity, at, deadline)
            else:
                # THE SETTLE HALF FLUSHES TOO. `complete_vend` delivered the
                # identity, the relay's record and the pending entry before the
                # route answered; the session action that BILLS THE STAY does
                # not exist until here, and without this it sat in the outbox
                # until some later ordinary arrival -- which at a lane using the
                # intercom may never come.
                controller.events.flush()
        finally:
            with self._state:
                self._in_progress = False

    # --- the idempotency store -------------------------------------------

    def _held(self, key: str) -> dict | None:
        with self._state:
            return self._answers.get(key)

    def _hold(self, key: str, body: dict) -> None:
        with self._state:
            self._answers[key] = body
            while self._answers and (
                len(self._answers) > MAX_HELD_KEYS or self._bytes() > MAX_HELD_BYTES
            ):
                self._answers.popitem(last=False)

    def _bytes(self) -> int:
        """What the store is holding, in bytes. Both bounds are real bounds."""
        return sum(len(key) + len(repr(body)) for key, body in self._answers.items())


class _Once:
    """A claim exactly one caller can take. Two settlers, one outcome.

    The transit is resolved either by the loop driver returning or by this
    lane's own deadline expiring, and both can happen -- a driver that hangs for
    an hour and then returns arrives long after the deadline recorded
    `loop_driver_timeout`. Without this, that late return would record a SECOND
    outcome for one transit: a confirmed, billable session opened by a crossing
    the lane had already published as unconfirmable.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._taken = False

    def take(self) -> bool:
        with self._lock:
            if self._taken:
                return False
            self._taken = True
            return True


__all__ = [
    "MAX_HELD_BYTES",
    "MAX_HELD_KEYS",
    "AssistedVend",
    "BadVendRequest",
    "VendRequest",
    "parse",
]
