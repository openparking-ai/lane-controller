"""The lane contract. This module IS the lane's public read surface.

Everything else in this package is an implementation detail that may be
rewritten; the payloads below are not. They are what a consumer integrates
against -- our own intercom agent and a third party's alike -- and they are
versioned from the first release so a change to them is always visible as one.

**This round is READ ONLY.** There is no vend, no resolve, and no route that
changes anything. `Capabilities.can_vend` exists so a consumer can ASK, and it
is derived from the service's own act-route table rather than typed, so the
answer cannot drift from the routes that exist.

Four properties are the whole point, and each is enforced below rather than
described and hoped for:

  * **A state nobody derived is `unknown`, never `ok`.** `ok` and `active` are
    claims about a measurement and may only be carried by a code this build
    actually derives. Everything else says so out loud. A health surface that
    reports `ok` for a signal nothing produces is the standing acceptance --
    never wrong silently -- broken at the one seam an operator trusts.

  * **`reason` is an OPEN string with a REQUIRED closed subset.** Our lane's
    subset is `decision.Fallback`, derived here and never re-typed. A lane that
    is not ours may emit a reason outside it, and a consumer that does not
    recognise one ESCALATES -- it never guesses.

  * **`fallback` is DERIVED from `reason`, not stored beside it.** It carries
    the value only when the reason is a member of our closed set, so the two
    cannot come to disagree. A claim lives in exactly one place.

  * **Every malfunction code ships, every time.** The table is closed and a
    payload missing one of them is refused at construction. A code that is
    absent reads to a consumer exactly like a code that is fine.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum

from .decision import Fallback, Outcome
from .interfaces import Unavailable

#: Bumped whenever a payload's shape changes in a way a consumer could notice.
#: A consumer handed a version it does not recognise should say so and stop,
#: not guess which fields it still understands. Additive changes do not bump it
#: and a consumer ignores fields it does not know -- the same rule the Vehicle
#: ID contract states, so one consumer can hold one policy for both.
CONTRACT_VERSION = 1

#: The reasons that are FALLBACK codes, and the only ones `LastDecision.fallback`
#: will ever carry. Derived from the enum the lane decides with.
FALLBACK_REASONS: tuple[str, ...] = tuple(code.value for code in Fallback)

#: The reasons a lane MUST be able to emit, and the whole of what a consumer
#: may assume. DERIVED from the two enums the lane decides with, so this can
#: never become a hand-written list of a set the code defines.
#:
#: It is the fallback codes plus the outcomes that are not fallbacks: when a
#: lane did NOT fall back, `outcome` has already said what happened, and
#: `reason` repeats it rather than being empty -- so the field is total and a
#: consumer never meets a null it has to interpret. `Outcome.FALLBACK` is not
#: in here, because a lane that fell back names WHY in a fallback code.
#:
#: A lane may emit a reason outside this set -- a third-party lane has its own
#: vocabulary and will -- and the contract requires a consumer to ESCALATE on
#: one it does not recognise rather than map it to the nearest thing it knows.
REQUIRED_REASONS: tuple[str, ...] = (
    *FALLBACK_REASONS,
    *(outcome.value for outcome in Outcome if outcome is not Outcome.FALLBACK),
)

#: The causes a lane MUST be able to name when it obtained no read at all, and
#: `None` when it obtained one. Open in the same way and for the same reason as
#: `REQUIRED_REASONS`, and derived from the same place the lane records.
REQUIRED_CAUSES: tuple[str, ...] = tuple(cause.value for cause in Unavailable)

#: The outcomes, and this set is CLOSED. A consumer has to be able to branch on
#: what happened to the vehicle -- admitted, refused, handed to a human, or
#: nothing was there -- and a lane that could invent a fifth would leave every
#: consumer with a case it has no behaviour for. A lane's own vocabulary goes
#: in `reason`, which is open precisely so this one does not have to be.
OUTCOMES: tuple[str, ...] = tuple(outcome.value for outcome in Outcome)


def _text(value, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")
    return value


def _iso_utc(value, field_name: str) -> str:
    """An ISO 8601 timestamp that carries an offset.

    A naive timestamp is refused rather than assumed to be UTC -- the same rule
    the Vehicle ID contract applies, and for the same reason: assuming is how a
    lane in one timezone and a consumer in another come to disagree about when
    a car arrived, months later, with the money already collected.
    """
    text = _text(value, field_name)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not ISO 8601: {text!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} has no UTC offset: {text!r}")
    return text


# ---------------------------------------------------------------------------
# GET /v1/lane
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What this lane can and cannot do, so a consumer never has to assume.

    Every one of these is DERIVED from the lane it describes -- its declared
    geometry, its wiring, its route table -- and none is a constant somebody
    typed. A capability set that can be edited without the capability changing
    is a capability set that will one day be wrong.
    """

    #: Two loops after the barrier, crossed in order, can say whether a vehicle
    #: actually went through. `false` is an ordinary lane, not a broken one --
    #: and a consumer must have a case for a lane that cannot confirm anything,
    #: because a third-party lane usually cannot.
    confirms_entry: bool
    #: This lane is wired to an identification service. `false` means identity
    #: comes from somewhere else, or from nowhere.
    has_identity_service: bool
    #: This lane reports to a platform. `false` is standalone, which is a
    #: supported mode and not a degraded one.
    has_platform: bool
    #: A display this lane can put a per-arrival code on. `false` until a
    #: display seam exists; derived from the wiring, so the day one is wired
    #: this follows it.
    has_display: bool
    #: Whether this lane exposes a route that opens a barrier. FALSE for the
    #: whole of this contract version: there is no act surface. Derived from
    #: the service's act-route table, so a vend route cannot be added without
    #: this answer changing with it.
    can_vend: bool

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LaneDescription:
    """`GET /v1/lane` -- who this lane is and what it can do."""

    lane_id: str
    site_id: str
    direction: str
    geometry: dict
    capabilities: Capabilities
    contract_version: int = CONTRACT_VERSION

    def __post_init__(self) -> None:
        _text(self.lane_id, "lane_id")
        _text(self.site_id, "site_id")
        if self.direction not in ("entry", "exit"):
            raise ValueError(f"direction must be 'entry' or 'exit', got {self.direction!r}")
        if not isinstance(self.capabilities, Capabilities):
            raise ValueError("capabilities must be a Capabilities")

    def to_dict(self) -> dict:
        return {
            "lane_id": self.lane_id,
            "site_id": self.site_id,
            "direction": self.direction,
            "contract_version": self.contract_version,
            # The geometry is whatever `LoopConfig.as_published()` produced.
            # Not re-keyed, not re-ordered, not re-described: a second copy of
            # the geometry is a second thing to go stale, and the record the
            # lane writes on every vehicle already carries this exact dict.
            "geometry": self.geometry,
            "capabilities": self.capabilities.to_dict(),
        }


# ---------------------------------------------------------------------------
# GET /v1/lane/state
# ---------------------------------------------------------------------------


class TransitState(StrEnum):
    """What became of the last vend, or `none` if there has not been one.

    The five outcomes the lane already names, plus the honest sixth. `held` is
    neither a confirmation nor a refutation and is never folded into either --
    voiding it silently re-creates the abandoned-ticket fraud, and promoting it
    is the phantom occupant.
    """

    #: A vend happened and nothing has decided its fate yet.
    PENDING = "pending"
    #: Two loops after the gate saw a vehicle cross them forward, in the window.
    CONFIRMED = "confirmed"
    #: The window elapsed with no crossing. Flagged, never voided, never promoted.
    HELD = "held"
    #: The loops saw the crossing go the other way. At an `exit` lane that is a
    #: vehicle that reversed back inside -- read this with `direction`, which is
    #: on `GET /v1/lane`, because the two are different facts about different money.
    BACKED_OUT = "backed_out"
    #: This lane has no closing loops, so nothing could confirm or refute it.
    UNCONFIRMABLE = "unconfirmable"
    #: No vend has happened in this process. NOT a claim that none ever did:
    #: this lane keeps no state store, so a restart returns to `none`.
    NONE = "none"


@dataclass(frozen=True, slots=True)
class Transit:
    """The current transit. `since` is null exactly when the state is `none`."""

    state: str
    since: str | None = None

    def __post_init__(self) -> None:
        if self.state not in tuple(s.value for s in TransitState):
            raise ValueError(
                f"transit state must be one of {tuple(TransitState)}, got {self.state!r}"
            )
        if self.state == TransitState.NONE:
            if self.since is not None:
                raise ValueError("transit state is 'none'; there is no moment for it to be since")
            return
        if self.since is None:
            raise ValueError(f"transit state {self.state!r} carries no `since`")
        _iso_utc(self.since, "transit.since")

    def to_dict(self) -> dict:
        return {"state": self.state, "since": self.since}


@dataclass(frozen=True, slots=True)
class LastDecision:
    """The last decision this lane made, exactly as it made it.

    `fallback` is not a field. It is DERIVED from `reason` below, so our closed
    code and the open string can never come to disagree -- which they would, the
    first time one of them was set and the other was not.
    """

    outcome: str
    reason: str
    at: str
    #: Whether a VEHICLE was there. `None` means nobody measured it, and a
    #: consumer that reads `None` as `False` turns every lane without a
    #: reference view into one that refuses every customer.
    presence: bool | None = None
    #: WHICH failure stopped the lane getting a read, or `None` when it got
    #: one. Open, with `REQUIRED_CAUSES` as the subset a consumer may assume.
    cause: str | None = None
    #: The identification this decision was made from, when there was one.
    #: `None` when no read was obtained, and when the identifier does not
    #: supply one.
    read_ref: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}, got {self.outcome!r}")
        _text(self.reason, "reason")
        _iso_utc(self.at, "at")
        if self.presence is not None and not isinstance(self.presence, bool):
            raise ValueError(f"presence must be true, false or null, got {self.presence!r}")
        if self.cause is not None:
            _text(self.cause, "cause")
        if self.read_ref is not None:
            _text(self.read_ref, "read_ref")

    @property
    def fallback(self) -> str | None:
        """`reason` when it is one of ours, `None` when it is not.

        The single most useful field for a consumer written against this lane,
        and the single easiest one to get wrong if it were stored: a lane whose
        `reason` moved and whose `fallback` did not would report a code that
        contradicts the sentence beside it. So it is computed, here, once.

        A third-party lane emitting its own reason gets `null` here, which is
        the signal to escalate rather than to map it onto the nearest code. So
        does an outcome that is not a fallback: an admitted vehicle has no
        fallback code, and this is not the place to invent one.
        """
        return self.reason if self.reason in FALLBACK_REASONS else None

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "fallback": self.fallback,
            "cause": self.cause,
            "presence": self.presence,
            "at": self.at,
            "read_ref": self.read_ref,
        }


@dataclass(frozen=True, slots=True)
class LaneState:
    """`GET /v1/lane/state` -- the last decision, and the current transit."""

    decision: LastDecision | None
    transit: Transit

    def __post_init__(self) -> None:
        if self.decision is not None and not isinstance(self.decision, LastDecision):
            raise ValueError("decision must be a LastDecision or null")
        if not isinstance(self.transit, Transit):
            raise ValueError("transit must be a Transit")

    def to_dict(self) -> dict:
        return {
            "contract_version": CONTRACT_VERSION,
            "decision": self.decision.to_dict() if self.decision else None,
            "transit": self.transit.to_dict(),
        }


# ---------------------------------------------------------------------------
# GET /v1/lane/health
# ---------------------------------------------------------------------------


class MalfunctionCode(StrEnum):
    """Every malfunction this lane's health surface names.

    The set is CLOSED and every member ships on every response. A code that is
    absent from a payload reads to a consumer exactly like a code that is fine,
    so absence is refused at construction rather than left to be noticed.

    None of them is folded into another. Two faults with the same name are two
    faults an operator cannot tell apart, and the repairs are different.
    """

    BOOM_DID_NOT_RISE = "boom_did_not_rise"
    BOOM_DID_NOT_CLOSE = "boom_did_not_close"
    VEND_RELAY_FAULT = "vend_relay_fault"
    ARMING_LOOP_STUCK_OCCUPIED = "arming_loop_stuck_occupied"
    ARMING_LOOPS_DISAGREE = "arming_loops_disagree"
    CLOSING_LOOPS_NEVER_FIRING = "closing_loops_never_firing"
    CAMERA_FEED_LOST = "camera_feed_lost"
    CAMERA_FEED_FROZEN = "camera_feed_frozen"
    LENS_OBSTRUCTED_OR_DARK = "lens_obstructed_or_dark"
    REFERENCE_NOT_RECOGNISED = "reference_not_recognised"
    IDENTITY_SERVICE_DOWN = "identity_service_down"
    IDENTITY_SERVICE_DEGRADED = "identity_service_degraded"
    IDENTITY_SERVICE_UNMEASURED_WEIGHTS = "identity_service_unmeasured_weights"
    PLATFORM_UNREACHABLE = "platform_unreachable"
    LANE_GONE_QUIET = "lane_gone_quiet"
    OUTBOX_DEPTH_GROWING = "outbox_depth_growing"
    SESSION_ACTIONS_DEAD_LETTERED = "session_actions_dead_lettered"
    INTERCOM_REGISTRATION_LOST = "intercom_registration_lost"
    CONTROLLER_ON_BATTERY = "controller_on_battery"
    DISK_NEARLY_FULL = "disk_nearly_full"
    CLOCK_SKEW_REJECTED = "clock_skew_rejected"


class Source(StrEnum):
    """Where a code's answer comes from, published beside every answer.

    This is the field that stops a health surface lying by omission. A code
    nothing produces and a code that is fine look identical without it.
    """

    #: This build derives a state for the code from something it observes.
    MEASURED = "measured"
    #: A signal exists -- and the contract names where -- but this build does
    #: not read it, so it has nothing to say. NOT the same as `ok`.
    NOT_MEASURED = "not_measured"
    #: Nothing in this system produces this signal at all. It will stay
    #: `unknown` until something is built that can answer it.
    NO_SOURCE = "no_source"


class HealthState(StrEnum):
    OK = "ok"
    ACTIVE = "active"
    #: Nobody knows. A consumer may NEVER read this as `ok`: at a lane, `ok`
    #: means somebody measured and found nothing, and this means nobody
    #: measured. `false` is not a safe default and neither is `ok`.
    UNKNOWN = "unknown"


#: WHERE each code's answer comes from in this build. One copy, and the payload
#: is built from it -- so a code cannot ship with a source that disagrees with
#: what the service actually reads.
#:
#: `not_measured` entries name their source in `docs/CONTRACT.md`; the point of
#: separating them from `no_source` is that closing one is reading something
#: that already exists, and closing the other is building it.
SOURCES: dict[MalfunctionCode, Source] = {
    MalfunctionCode.BOOM_DID_NOT_RISE: Source.NO_SOURCE,
    MalfunctionCode.BOOM_DID_NOT_CLOSE: Source.NO_SOURCE,
    MalfunctionCode.VEND_RELAY_FAULT: Source.NO_SOURCE,
    MalfunctionCode.ARMING_LOOP_STUCK_OCCUPIED: Source.NO_SOURCE,
    MalfunctionCode.ARMING_LOOPS_DISAGREE: Source.NOT_MEASURED,
    MalfunctionCode.CLOSING_LOOPS_NEVER_FIRING: Source.NOT_MEASURED,
    MalfunctionCode.CAMERA_FEED_LOST: Source.NOT_MEASURED,
    MalfunctionCode.CAMERA_FEED_FROZEN: Source.NO_SOURCE,
    MalfunctionCode.LENS_OBSTRUCTED_OR_DARK: Source.NOT_MEASURED,
    MalfunctionCode.REFERENCE_NOT_RECOGNISED: Source.NOT_MEASURED,
    MalfunctionCode.IDENTITY_SERVICE_DOWN: Source.MEASURED,
    MalfunctionCode.IDENTITY_SERVICE_DEGRADED: Source.NOT_MEASURED,
    MalfunctionCode.IDENTITY_SERVICE_UNMEASURED_WEIGHTS: Source.NO_SOURCE,
    MalfunctionCode.PLATFORM_UNREACHABLE: Source.NO_SOURCE,
    MalfunctionCode.LANE_GONE_QUIET: Source.NOT_MEASURED,
    MalfunctionCode.OUTBOX_DEPTH_GROWING: Source.MEASURED,
    MalfunctionCode.SESSION_ACTIONS_DEAD_LETTERED: Source.MEASURED,
    MalfunctionCode.INTERCOM_REGISTRATION_LOST: Source.NO_SOURCE,
    MalfunctionCode.CONTROLLER_ON_BATTERY: Source.NO_SOURCE,
    MalfunctionCode.DISK_NEARLY_FULL: Source.NO_SOURCE,
    MalfunctionCode.CLOCK_SKEW_REJECTED: Source.NOT_MEASURED,
}

#: Codes a monitor must NOT page a human on, each with the caveat that says why.
#: Membership and caveat come from this one mapping, so a code cannot be marked
#: safe in one place and alarmed on in another.
#:
#: `reference_not_recognised` is here by decision (SETTLED 3d(i)): one reason
#: covers several causes, and one of them is an ordinary car arriving. A gate
#: that pages a technician because a car arrived is the failure that decision
#: exists to prevent.
NEVER_ALARM: dict[MalfunctionCode, str] = {
    MalfunctionCode.REFERENCE_NOT_RECOGNISED: (
        "NOT an alarm. This code originates in the identification engine, which publishes what "
        "the one reason covers in its own `camera_faults_caveat` -- read that, not this. One of "
        "the causes it covers is an ordinary car arriving, so paging a human on this code is the "
        "failure the caveat exists to prevent."
    ),
}


@dataclass(frozen=True, slots=True)
class HealthEntry:
    """One malfunction code, its state, and where the answer came from.

    `never_alarm` and `caveat` are DERIVED from `NEVER_ALARM`, not stored, so a
    code cannot be published as safe in one payload and alarmed on in the next.
    """

    code: str
    state: str

    def __post_init__(self) -> None:
        if self.code not in tuple(c.value for c in MalfunctionCode):
            raise ValueError(f"{self.code!r} is not a malfunction code in this contract")
        if self.state not in tuple(s.value for s in HealthState):
            raise ValueError(
                f"health state must be one of {tuple(HealthState)}, got {self.state!r}"
            )
        # The invariant this surface exists to hold. `ok` and `active` are
        # claims about a measurement; a code this build does not derive has no
        # standing to make either. Without this line the honest thing and the
        # dangerous thing are one keystroke apart.
        if self.state != HealthState.UNKNOWN and self.source != Source.MEASURED:
            raise ValueError(
                f"{self.code} is {self.source.value} but claims state {self.state!r}. "
                "Only a code this build derives may answer anything but 'unknown'."
            )

    @property
    def source(self) -> Source:
        return SOURCES[MalfunctionCode(self.code)]

    @property
    def never_alarm(self) -> bool:
        return MalfunctionCode(self.code) in NEVER_ALARM

    @property
    def caveat(self) -> str | None:
        return NEVER_ALARM.get(MalfunctionCode(self.code))

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "state": self.state,
            "source": self.source.value,
            "never_alarm": self.never_alarm,
            "caveat": self.caveat,
        }


@dataclass(frozen=True, slots=True)
class LaneHealth:
    """`GET /v1/lane/health` -- every code in the table, every time.

    Refused at construction if a code is missing or duplicated. An absent code
    is indistinguishable from a healthy one to whoever reads this, which is the
    whole reason the set is closed.
    """

    entries: tuple[HealthEntry, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        seen = [entry.code for entry in self.entries]
        if len(seen) != len(set(seen)):
            raise ValueError("a malfunction code appears twice in one payload")
        missing = {code.value for code in MalfunctionCode} - set(seen)
        if missing:
            raise ValueError(
                f"health payload is missing {sorted(missing)}. Every code ships every time: "
                "one that is absent reads exactly like one that is fine."
            )

    def to_dict(self) -> dict:
        return {
            "contract_version": CONTRACT_VERSION,
            "codes": [entry.to_dict() for entry in self.entries],
        }


# ---------------------------------------------------------------------------
# GET /v1/lane/events?since=N
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EventPage:
    """`GET /v1/lane/events?since=N` -- the cursor shape Vehicle ID publishes.

    Deliberately the same semantics, field for field, so one consumer can hold
    one cursor policy for both surfaces:

      * the cursor is monotonic within one run and is NOT durable across a
        restart -- it is a catch-up window for a consumer that blinked, not a
        record of anything;
      * `since` ahead of the lane's own cursor sets `reset`, which is what says
        the lane restarted and a saved position no longer refers to anything.
        An empty list without that flag is indistinguishable from "nothing
        happened", which is how a consumer silently misses everything after a
        restart.

    `dropped` is the lane's own count of LOG events discarded because the
    bounded log reached its limit. Session actions are never dropped. It is
    published because a gap nobody knows about is worse than one that is
    counted.
    """

    cursor: int
    reset: bool
    dropped: int
    events: tuple[dict, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "contract_version": CONTRACT_VERSION,
            "cursor": self.cursor,
            "reset": self.reset,
            "dropped": self.dropped,
            "events": list(self.events),
        }
