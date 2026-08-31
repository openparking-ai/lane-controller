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
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime

from .contract import (
    NEVER_ALARM,
    VEND_IDENTITY_KINDS,
    HealthState,
    TransitState,
    VendAuthority,
    VendCommanded,
    VendRefusal,
    VendRefused,
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

    if not idempotency_key or not isinstance(idempotency_key, str) or not idempotency_key.strip():
        # REQUIRED, and there is no generated fallback. A key this lane invented
        # would be unique per request, which is the same as having none: the
        # caller's retry would vend a second time.
        raise BadVendRequest("an Idempotency-Key header is required")

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
        # The VALUE is not named in the refusal. It is the one field on this
        # request that is a secret, and a message is the one place a secret
        # reaches a log without anybody deciding to put it there.
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
        idempotency_key=idempotency_key.strip(),
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
        # codes it MEASURES can be `active` at all. A `never_alarm` code does
        # not refuse: `reference_not_recognised` covers an ordinary car
        # arriving, and refusing on it would turn a car into a locked barrier.
        for code, state in sorted(
            self._service.derived_states().items(), key=lambda item: item[0].value
        ):
            if state is HealthState.ACTIVE and code not in NEVER_ALARM:
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

        # 4. decision_stale -- a completion is an answer to a driver who is at
        # the barrier NOW.
        max_age = controller.config.completion_max_age_s
        age = (
            datetime.fromtimestamp(controller.now(), tz=UTC) - _instant(request.decision_at)
        ).total_seconds()
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

        # 6. not_completable -- there is nothing to complete.
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
                "idempotency_key": request.idempotency_key,
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
        try:
            self._service.controller.resolve_transit(identity, at)
        except Exception:  # noqa: BLE001
            # A settling thread that died silently would leave this lane
            # `busy` for ever and the transit `pending` for ever, with nothing
            # anywhere saying why.
            log.exception("the assisted vend's transit could not be settled")
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
            while len(self._answers) > MAX_HELD_KEYS:
                self._answers.popitem(last=False)


__all__ = [
    "MAX_HELD_KEYS",
    "AssistedVend",
    "BadVendRequest",
    "VendRequest",
    "parse",
]
