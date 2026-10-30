"""The decision stage: local rules, local cache, no network on the hot path.

A lane with no internet still has to work. Every decision below is made from
rules already on the box; the server is how the cache gets refreshed and where
events are reported afterwards, never something the barrier waits on.

THE ENTRY, BY WHAT THE GARAGE TAKES. The owner's rule, 2026-10-26: a garage
that takes transients opens for every car -- the pictures and what was seen
are the record, and there is no reason not to open -- and a garage that is
monthly and pass only opens for the cars its registers cover today and keeps
the gate shut for every other. A pass-only garage's car whose pass this lane
cannot confirm -- its plate unread or read below the confidence, or no
register held -- goes to a person, unless the driver scans the car's
registration QR (`qr_may_open`, which also covers the car no register
covers). So at an entry lane whose garage has STATED which it is
(`transient_available`, `garage_admits`), the decision is `_decide_entry`:
low confidence, no plate read, an unknown vehicle, an engine that did not
answer and rules past their age are not reasons to call a person at a garage
that takes transients. What still keeps the gate shut is the garage full
(`closed: full`, which still lets in a car a register covers), the owner's
closing to everyone, a car on the banned list (`controller._banned`), an
owner's per-plate deny rule, and -- at a pass-only garage -- a car read with
confidence that no register covers. Nothing present is still no open. A
garage that has not stated it, and every exit, decide exactly as before.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from enum import StrEnum

from .interfaces import VehicleIdentity


class Outcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    FALLBACK = "fallback"
    #: Nothing was there. NOT a fallback: a fallback means a human deals with a
    #: vehicle we could not identify, and it ends in a ticket. This ends in
    #: nothing at all -- no ticket, no session, no vend -- because there is no
    #: car. Conflating the two is how a person on foot with a piece of metal
    #: gets issued a ticket they can walk a car out on.
    NO_VEHICLE = "no_vehicle"


class Fallback(StrEnum):
    """Why we are not deciding, so the lane can do the right visible thing.

    Every one of these is an explicit path with an operator-visible
    consequence. None of them is "open anyway" and none is "guess".
    """

    LOW_CONFIDENCE = "low_confidence"
    NO_PLATE_READ = "no_plate_read"
    UNKNOWN_VEHICLE = "unknown_vehicle"
    STALE_RULES = "stale_rules"
    #: No read was obtained AT ALL: the identification engine was not reached,
    #: did not answer in time, answered with something this build cannot read,
    #: or was never asked because there was nothing to send it.
    #:
    #: A DEAD ENGINE IS NOT A MARGINAL READ. Both used to arrive here as
    #: confidence 0.0 and leave as `low_confidence`, so an operator surface --
    #: and the intercom agent that will read these codes -- could not tell a
    #: driver whose plate needs wiping from a service that is switched off, and
    #: would tell the second one to wipe their plate.
    #:
    #: WHICH failure is in the event detail, not in a second enum member: the
    #: lane's response is the same for all of them (a human, never a guess) and
    #: a member per cause would be a set of codes nobody can act on differently.
    ENGINE_UNREACHABLE = "engine_unreachable"
    #: THE OWNER CLOSED THIS LANE, and the car in front of it is not one the
    #: closing lets in: closed to everyone, or closed `full` and no pass or
    #: monthly register covers this car today. The lane opens for it by
    #: nobody's say but a person's -- `human_open_now` or `human_open_and_flag`
    #: through `POST /v1/lane/vend`, so a driver can always get help. A display
    #: code does not complete it (`vend.py`), because a ticket is a driver
    #: serving themselves and a closing is the owner saying they may not.
    LANE_CLOSED = "lane_closed"


#: A lane nobody has closed. What a cache holds until the platform says
#: otherwise, and what it goes back to when it holds nothing.
OPEN_LANE: dict = {"state": "open", "reason": None, "message": None, "closed_at": None}

#: The closing reasons this lane acts on. `full` lets in a car a register covers
#: today and nobody else; `everyone` lets in nobody. ANY OTHER CLOSED STATE --
#: a reason this build does not know, or `full` at an exit, which the platform
#: refuses -- is acted on as `everyone`: a person still opens it, and a closing
#: read as less than it says would let in the cars the owner closed it to.
CLOSED_FULL = "full"
CLOSED_EVERYONE = "everyone"


def lane_from_payload(value) -> dict | None:
    """The `lane` object as the platform sends it, or None when it is not one.

    `None` means "the payload said nothing this build can read": the caller
    keeps what it holds rather than reading a malformed answer as open.
    """
    if not isinstance(value, dict):
        return None
    if value.get("state") == "open":
        return dict(OPEN_LANE)
    if value.get("state") != "closed":
        return None
    reason, message = value.get("reason"), value.get("message")
    if not isinstance(reason, str) or not isinstance(message, str):
        return None
    closed_at = value.get("closed_at")
    return {
        "state": "closed",
        "reason": reason,
        "message": message,
        "closed_at": closed_at if isinstance(closed_at, str) else None,
    }


def board_from_payload(value) -> dict | None:
    """The `board` object as the platform sends it, or None when it is not one.

    Only the fields this lane uses are kept; a message without readable text is
    dropped rather than shown as something else.
    """
    if not isinstance(value, dict):
        return None
    messages = []
    for item in value.get("messages") or []:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        messages.append({
            "id": item.get("id") if isinstance(item.get("id"), str) else None,
            "text": item["text"],
            "starts_at": item.get("starts_at") if isinstance(item.get("starts_at"), str) else None,
            "ends_at": item.get("ends_at") if isinstance(item.get("ends_at"), str) else None,
        })
    return {"prices": value.get("prices") is True, "messages": messages}


@dataclass(frozen=True, slots=True)
class Decision:
    outcome: Outcome
    reason: str
    identity: VehicleIdentity
    fallback: Fallback | None = None
    rate_plan: str | None = None
    #: At an EXIT that will vend: what the lane decided about money, from the
    #: cache alone (`exit_pricing.ExitPricing`). None at an entry, on a
    #: fallback, or on a lane built before this existed. Not part of the read
    #: contract yet: the display round is what publishes it.
    exit_pricing: object | None = None
    #: A refusal a registration QR may still turn into an open: a pass-only
    #: garage's car no register covers, or whose pass this lane could not
    #: confirm (`_decide_entry`). Set where the decision is made and nowhere
    #: else; `qr_may_open` adds the exit's case.
    qr_may_open: bool = False

    @property
    def should_vend(self) -> bool:
        return self.outcome is Outcome.ALLOW


def qr_may_open(decision: Decision, direction: str | None) -> bool:
    """Whether a QR scanned for this decision's car may be presented.

    At an entry, only the refusals `_decide_entry` marks: a pass-only garage's
    car that no register covers or whose pass could not be confirmed. Never a
    banned car, an owner's deny rule, a full garage or a lane closed to
    everyone -- the QR registers a car, it does not overrule the owner. At an
    exit, any car the lane did not let out but a closing: the module decides
    whether this garage enrols there (`set-garage-enrols-at exit`), and at an
    exit it is never refused, only covered or not.
    """
    if decision.should_vend or decision.outcome is Outcome.NO_VEHICLE:
        return False
    if direction == "exit":
        return (
            decision.outcome is Outcome.FALLBACK and decision.fallback is not Fallback.LANE_CLOSED
        )
    return decision.qr_may_open


@dataclass(frozen=True, slots=True)
class Rule:
    """One cached allow/pricing rule."""

    plate: str
    allow: bool
    rate_plan: str | None = None


#: The letters the plate reader never outputs, and the digit it reads each as.
#: Its alphabet leaves out I and O, so a registered GO4IT arrives as G041T.
_READER_FOLD = str.maketrans({"O": "0", "I": "1"})


def normalise_identity(text: str) -> str:
    """The one folding under which a plate the camera read and an identity a
    registrar typed are compared: upper-case, letters and digits only, and O
    read as 0 and I as 1. `AB-123`, `ab 123` and `AB123` are one identity
    here, and so are `GO4IT` and `G041T`.

    O AND I ARE FOLDED BECAUSE THE READER CANNOT TELL THEM APART: its alphabet
    has no I and no O, so it reads a registered `GO4IT` as `G041T`. Folded on
    both sides -- the read and the registrar's text -- a holder whose plate
    has an I or an O matches their own pass or agreement instead of paying as
    a stranger on every visit.

    AN ASSUMPTION, STATED: garage-pass stores an identity as the registrar
    typed it and monthly-billing folds it under the garage's own rule
    (`identity_normalised`), and the lane knows neither rule. This folding is
    the widest reading both admit, so a holder is not sent to pay over a
    hyphen. AND NOTHING CATCHES IT WHEN IT IS TOO WIDE. A `covered` decision
    is consumed by the platform's close without either door being asked
    (0017); the reconciliation route (0017) lists that row and the sweep
    (0018) records it `covered`, and neither re-consults the modules -- so a
    car this folding let out covered that the modules would not have covered
    is a fee that was never charged, and nothing in the record contradicts
    it. What the record does carry is the CLAIM: `decided_by = 'lane'`, the
    pass or agreement the lane says it matched, and an `exit_covered` event
    whose actor is `lane:decision`. A reader can take those to the modules;
    nothing on either side does it for them.
    """
    return "".join(ch for ch in text.upper() if ch.isalnum()).translate(_READER_FOLD)


def _register_heads(module: str, register: dict) -> dict[str, dict]:
    """The pass or agreement entries of a register, by id, as the modules'
    `show-garage-register` verbs list them (garage-pass G27: `passes[]` keyed
    `pass`; monthly-billing G48: `agreements[]` keyed `agreement`)."""
    if module == "garage_pass":
        return {p.get("pass"): p for p in register.get("passes") or [] if p.get("pass")}
    return {a.get("agreement"): a for a in register.get("agreements") or [] if a.get("agreement")}


#: The default ceiling on a pass-only garage's registers kept past the cache's
#: bound while the platform cannot be reached (`config` publishes it as
#: `[lane] pass_registers_offline_days`, 7).
PASS_REGISTERS_OFFLINE_S = 7 * 86_400.0

#: Full, in basis points: how full a garage can be, and no fuller.
FULL_BP = 10_000

#: What a garage takes at its entry, as `garage_admits` reads the cache: every
#: car (transients), or only the cars its pass and monthly registers cover.
ADMITS_TRANSIENTS = "transients"
ADMITS_PASSES_ONLY = "passes_only"

#: THE QR SEAM (Phase 1b). The reason a monthly/pass-only garage keeps the gate
#: shut for a car: no register covers it today. A decision a scanned
#: registration QR may turn into an open (`qr_may_open`): the driver scans it
#: at the lane's scanner, the lane presents it through the platform to the pass
#: module's enrolment (`redeem-enrolment`: valid today, for this garage, spent
#: once, one car), and a QR it answers covered for opens
#: (`LaneController.observe_qr`). With no scanner, or the platform out of
#: reach, the DENY stands and a person's `human_open_now` is the way past it.
REASON_NOT_REGISTERED = "pass or monthly only; no pass or monthly register covers this car"


def garage_admits(cache: DecisionCache) -> str | None:
    """What this garage takes at its entry, or None when it has not said.

    `transient_available: false` is a monthly/pass-only garage. `true` is a
    garage that takes transients -- unless its default for a car with no rule
    is `deny`, which is an operator saying in so many words that strangers are
    not let in; read as pass-only, so a covered car still opens and no other
    car does (the stricter of the two readings, and the one that never opens a
    gate the owner set to deny). None -- a lane that has never been told, a
    platform older than the field -- decides exactly as before.
    """
    if cache.transient_available is False:
        return ADMITS_PASSES_ONLY
    if cache.transient_available is True:
        return ADMITS_PASSES_ONLY if cache.default_action == "deny" else ADMITS_TRANSIENTS
    return None


def plate_seen(identity: VehicleIdentity) -> str | None:
    """Whatever plate text the camera read, AT ANY CONFIDENCE, of a car that
    was there -- or None. Never an identity and never looked up to OPEN
    anything: it is what the owner's deny rule is checked against, first
    (`owner_denies`), so a plate read below the confidence cannot get around
    the owner's deny on the way to a person or a QR."""
    if identity.presence is False or identity.unavailable is not None or not identity.plate:
        return None
    return identity.plate


def owner_denies(cache: DecisionCache, *plates: str | None, stale: bool) -> Rule | None:
    """THE OWNER'S DENY, CHECKED FIRST AND ALWAYS WINNING (the owner's rule of
    2026-10-30): the deny rule naming any of these plates -- what was read at
    any confidence (`plate_seen`), and the plate a QR was made for -- or None.
    Like every owner's rule, only while the cache is trusted (`stale` False):
    a stale rule is never applied (E3, the entry's second review)."""
    if stale:
        return None
    return next((rule for plate in plates if (rule := cache.denying(plate)) is not None), None)


def plate_read(identity: VehicleIdentity, confidence_threshold: float) -> str | None:
    """The plate this lane stands behind, or None.

    A plate read at the lane's confidence, from a read that happened, of a car
    that was there. Nothing else is looked up -- not a rule, not a register --
    and nothing else becomes a stay's plate: a read below the threshold is
    "unreadable", a value of the car's identity, never a plate that could match
    another car's stay or a pass by accident.
    """
    if (
        identity.presence is False
        or identity.unavailable is not None
        or identity.confidence < confidence_threshold
        or not identity.plate
    ):
        return None
    return identity.plate


def _seen(identity: VehicleIdentity, confidence_threshold: float, *, stale: bool) -> str:
    """What was seen of an admitted car, in words, for the decision's reason.
    Never the plate: the reason goes into `events.detail`, which no purge reaches."""
    if identity.unavailable is not None:
        seen = f"plate unreadable: no read was obtained ({identity.unavailable})"
    elif not identity.plate:
        seen = "plate unreadable: no plate was read"
    elif identity.confidence < confidence_threshold:
        seen = (
            f"plate unreadable: confidence {identity.confidence:.2f} below threshold "
            f"{confidence_threshold:.2f}"
        )
    else:
        seen = "plate read"
    return f"{seen}; cached rules are stale" if stale else seen


def spaces_from_payload(raw) -> int | None:
    """The garage's number of spaces as the payload (or the disk) holds it:
    a whole number above zero, or None. Anything else is None -- a board
    with no figure prints no fill-rule prices rather than a guessed one."""
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return None
    return raw


def transient_from_payload(raw) -> bool | None:
    """The garage's `transient_available` as the payload (or the disk) holds
    it: True or False, and None for anything else -- absent, null, or a value
    that is not a boolean. None is read as it always was: no change."""
    return raw if isinstance(raw, bool) else None


def banned_from_payload(raw) -> list[dict]:
    """The `banned` list as the platform sends it, each entry an id and a
    descriptor, both non-empty strings. An entry in any other shape is not a
    car this build can match, and is left out rather than guessed at."""
    if not isinstance(raw, list):
        return []
    return [
        {"id": entry["id"], "descriptor": entry["descriptor"]}
        for entry in raw
        if isinstance(entry, dict)
        and isinstance(entry.get("id"), str)
        and entry["id"]
        and isinstance(entry.get("descriptor"), str)
        and entry["descriptor"]
    ]


class DecisionCache:
    """Local allow and pricing rules, refreshed from the server when it can be.

    The storage is a dict, and with a `store` it is ALSO a file: every
    refresh that changes the cache is written whole to `durable.DurableStore`
    and read back at construction, so a lane that restarts with the platform
    unreachable decides from what it last held rather than from nothing. The
    dicts stay the source for every decision; the disk is a copy written on
    the refresh thread and read once. Without a store -- a test, the demo, a
    standalone lane that chose none -- the behaviour is exactly what it was:
    replaced on every refresh, gone on restart.

    THE BOUND ON WHAT IS HELD, at rest and in memory, is `max_age_seconds`:
    the age past which the lane stops trusting the cache is the age past
    which it stops holding it. `expire_if_past_bound` wipes both, and a store
    older than the bound is not restored. `durable.py` says why that is one
    number and not two.
    """

    def __init__(
        self,
        *,
        max_age_seconds: float = 86_400.0,
        store=None,
        pass_registers_offline_s: float = PASS_REGISTERS_OFFLINE_S,
    ) -> None:
        self._rules: dict[str, Rule] = {}
        self._refreshed_at: float | None = None
        self._max_age = max_age_seconds
        #: THE CEILING on a pass-only garage's registers kept past the bound:
        #: how long after the platform last confirmed them they may still be
        #: held while it cannot be reached (`[lane] pass_registers_offline_days`).
        self._registers_offline_max = pass_registers_offline_s
        #: `durable.DurableStore`, or None. Anything with `read() -> dict|None`,
        #: `write(dict)` and `wipe()`.
        self._store = store
        # What to do with a confidently-read plate that has no rule. None means
        # fall back -- the safe default, and the one that applies until the
        # platform has said otherwise. A transient garage syncs "allow"; a
        # permit-only garage will sync nothing and keep falling back.
        self.default_action: str | None = None
        # The owner's "pass holders only" (`transient_available`), as the
        # payload states it: False when this garage takes no other driver,
        # True when it does, None when the payload says nothing -- a platform
        # older than the field, or a garage that has not stated it -- and
        # None is the lane exactly as it was. Only `False` changes anything.
        self.transient_available: bool | None = None
        # WHAT THE EXIT WILL DECIDE FROM, held as the platform hands it and
        # consumed by nothing in this file yet: the decision that reads these
        # is its own round. This round only keeps them fresh.
        #
        # The garage's rate plans, whole, and its space class -- what
        # `rate_engine.quote` takes.
        self.plans: list = []
        self.space_class: str | None = None
        # The garage's number of spaces (platform 0034), or None: the price
        # board prices a car coming in now on how full the garage is now, the
        # figure the platform freezes onto the stay when it opens.
        self.spaces: int | None = None
        # The garage's tax sets, WHOLE, as `rate_engine.contract.run_tax`
        # takes them (platform 0023). Replaced on every refresh and never
        # merged: the count and the newest instant the exit reports with a
        # priced decision describe every set this copy holds, which is what
        # the platform judges a stale copy by. Empty is a stale cache for
        # pricing, never a stay taxed at zero.
        self.tax_sets: list = []
        # Each entitlement module's register, verbatim, keyed by module. A
        # module is REPLACED only when the payload carries its `register`: a
        # module the platform could not read (`unavailable`, no `register`)
        # keeps what this cache already held, because an outage is not an
        # empty register and a cache that emptied itself on one would send
        # every pass holder to pay at the next exit.
        self.entitlements: dict[str, dict] = {}
        self.entitlements_complete: bool | None = None
        # The garage's open stays, by session id, and the cursor the next
        # delta continues from. The full set arrives with the rules (the slow
        # cadence); the deltas arrive on their own (the fast cadence), closed
        # rows included so a stay that left is dropped here.
        self.stays: dict[str, dict] = {}
        self.stays_cursor: str | None = None
        self.stays_refreshed_at: float | None = None
        # The garage's clock and money, as the payload names them: the exit
        # compares the registers' days with the garage's local day, and prices
        # in the garage's currency.
        self.timezone: str | None = None
        self.currency: str | None = None
        # THIS LANE, OPEN OR CLOSED BY THE OWNER, and the owner's board for
        # it (platform 0026, 0030). Both ride the FAST read as well as the
        # slow one, so a closing reaches the barrier inside one stays cadence;
        # both are kept on disk with the rest of the cache, under the same
        # bound, so a lane that restarts offline still obeys the last closing
        # it was told about. Past the bound the cache holds nothing -- and an
        # empty cache is a lane falling back on every plate anyway.
        self.lane: dict = dict(OPEN_LANE)
        self.board: dict = {"prices": False, "messages": []}
        #: The cars this lane refuses (U7f): each ban that reaches this garage,
        #: as the platform's `banned` list -- `{"id", "descriptor"}`. Matched by
        #: descriptor at the entry; a match is refused with reason `banned`.
        self.banned: list[dict] = []
        #: Whether the registers held are ones kept PAST the cache's bound at a
        #: pass-only garage (`clear`): kept while the lane cannot reach the
        #: platform, and wiped by the first rules read that answers.
        self._registers_past_bound = False
        #: When the platform last confirmed the registers kept past the bound:
        #: the refresh they came with. The ceiling counts from here.
        self._registers_confirmed_at: float | None = None
        # THE TWO INDEXES THE EXIT READS ON THE BARRIER'S PATH, rebuilt whole
        # on every change so the read is one dict lookup and never a scan:
        # identity -> the register rows that name it, per module; identity ->
        # the open stays under it. Keys are `normalise_identity`'d, so the
        # plate the camera read and the identity a registrar typed meet.
        self._coverage_index: dict[str, list[tuple[str, dict, dict]]] = {}
        self._stay_index: dict[str, list[dict]] = {}
        # TWO THREADS CHANGE THE OPEN STAYS: the refresh thread applies what
        # the platform says, and the lane thread drops a stay whose close it
        # has just recorded (`drop_stay`). This lock is held for the dict and
        # index work only -- never across the disk or the network -- so the
        # lane thread waits on nothing but a few assignments.
        self._stays_lock = threading.Lock()
        if self._store is not None:
            self._restore()

    def load(
        self,
        rules: list[Rule],
        *,
        default_action: str | None = None,
        now: float | None = None,
    ) -> None:
        self._rules = {r.plate.upper(): r for r in rules}
        self.default_action = default_action
        self._refreshed_at = time.time() if now is None else now
        self._persist()

    # -- the disk -----------------------------------------------------------------

    def state(self) -> dict:
        """The whole cache as one JSON-able value: what the store holds."""
        return {
            "rules": [
                {"plate": r.plate, "allow": r.allow, "rate_plan": r.rate_plan}
                for r in self._rules.values()
            ],
            "default_action": self.default_action,
            "transient_available": self.transient_available,
            "refreshed_at": self._refreshed_at,
            "plans": self.plans,
            "space_class": self.space_class,
            "spaces": self.spaces,
            "tax_sets": self.tax_sets,
            "entitlements": self.entitlements,
            "entitlements_complete": self.entitlements_complete,
            "stays": self.stays,
            "stays_cursor": self.stays_cursor,
            "stays_refreshed_at": self.stays_refreshed_at,
            "timezone": self.timezone,
            "currency": self.currency,
            "lane": self.lane,
            "board": self.board,
            "banned": self.banned,
            "registers_past_bound": self._registers_past_bound,
            "registers_confirmed_at": self._registers_confirmed_at,
        }

    def _persist(self) -> None:
        if self._store is not None:
            self._store.write(self.state())

    def _take_registers(self, state: dict, *, confirmed_at) -> None:
        """A stored state's registers, the garage's day they are read on, and
        when the platform last confirmed them."""
        self.entitlements = dict(state.get("entitlements") or {})
        self.entitlements_complete = state.get("entitlements_complete")
        self.timezone = state.get("timezone")
        self._registers_past_bound = True
        self._registers_confirmed_at = float(confirmed_at) if confirmed_at is not None else None
        self._reindex_coverage()

    def wipe_registers(self) -> bool:
        """Hold no register: memory and disk. For a platform that has refused
        this lane outright, and for registers kept past the ceiling. True if
        any was held."""
        held = bool(self.entitlements)
        self.entitlements = {}
        self.entitlements_complete = None
        self._registers_past_bound = False
        self._registers_confirmed_at = None
        self._coverage_index = {}
        self._persist()
        return held

    def _registers_past_ceiling(self, current: float) -> bool:
        """Whether registers kept past the bound have outlived the ceiling."""
        return self._registers_past_bound and (
            self._registers_confirmed_at is None
            or current - self._registers_confirmed_at > self._registers_offline_max
        )

    def _take_settings(self, state: dict) -> None:
        """The garage's entry settings from a stored state, each through the
        validator its payload field goes through. See `clear`."""
        if state.get("default_action") in ("allow", "deny"):
            self.default_action = state["default_action"]
        self.transient_available = transient_from_payload(state.get("transient_available"))
        self.lane = lane_from_payload(state.get("lane")) or dict(OPEN_LANE)

    def _restore(self, *, now: float | None = None) -> bool:
        """What the store holds, into memory -- unless it is past the bound,
        in which case it is wiped rather than loaded. True if restored.

        The garage's entry settings are taken first and whatever the age
        (`clear` says why): they are not personal data, and a lane restarted
        two days into an outage still knows whether its garage takes
        transients."""
        state = self._store.read()
        if not state:
            return False
        self._take_settings(state)
        current = time.time() if now is None else now
        if state.get("registers_past_bound"):
            # A pass-only garage's registers, kept past the bound while the
            # platform could not be reached (`clear`): still held -- unless
            # they have outlived the ceiling while the process was down.
            self._take_registers(state, confirmed_at=state.get("registers_confirmed_at"))
            if self._registers_past_ceiling(current):
                self.wipe_registers()
        if state.get("refreshed_at") is None:
            return False
        if (current - float(state["refreshed_at"])) > self._max_age:
            # Personal data past its bound is not held, and not even read
            # back into memory on the way to being discarded -- except a
            # pass-only garage's registers, which `clear` keeps, inside the
            # ceiling.
            if garage_admits(self) == ADMITS_PASSES_ONLY:
                self._take_registers(state, confirmed_at=state["refreshed_at"])
            self.clear(now=current)
            return False
        self._rules = {
            r["plate"].upper(): Rule(
                plate=r["plate"], allow=bool(r["allow"]), rate_plan=r.get("rate_plan")
            )
            for r in state.get("rules") or []
        }
        self.default_action = state.get("default_action")
        self.transient_available = transient_from_payload(state.get("transient_available"))
        self._refreshed_at = float(state["refreshed_at"])
        self.plans = list(state.get("plans") or [])
        self.space_class = state.get("space_class")
        self.spaces = spaces_from_payload(state.get("spaces"))
        self.tax_sets = list(state.get("tax_sets") or [])
        self.entitlements = dict(state.get("entitlements") or {})
        self.entitlements_complete = state.get("entitlements_complete")
        self.stays = dict(state.get("stays") or {})
        self.stays_cursor = state.get("stays_cursor")
        self.stays_refreshed_at = state.get("stays_refreshed_at")
        self.timezone = state.get("timezone")
        self.currency = state.get("currency")
        self.lane = lane_from_payload(state.get("lane")) or dict(OPEN_LANE)
        self.board = board_from_payload(state.get("board")) or {"prices": False, "messages": []}
        self.banned = banned_from_payload(state.get("banned"))
        self._reindex_coverage()
        self._reindex_stays()
        return True

    def clear(self, *, now: float | None = None) -> None:
        """Hold no personal data: memory and disk. Stale, no rules, no
        registers, no stays, no banned list -- the empty cache this object
        started as, with two exceptions.

        A PASS-ONLY GARAGE'S REGISTERS ARE KEPT while the platform cannot be
        reached (and the garage's day they are read on), until the first rules
        read that answers wipes them (`load_payload`), a platform that refuses
        this lane outright wipes them (`sync.sync_rules`), or they reach the
        CEILING -- `pass_registers_offline_s` after the platform last confirmed
        them -- whichever comes first.

        THE GARAGE'S ENTRY SETTINGS ARE KEPT: whether it takes transients
        (`transient_available`), its default for a car with no rule, and this
        lane's closing. None of them names a car or a person, so the bound that
        exists for personal data does not reach them -- and the entry is
        decided on them. Wiped with the rest, a garage that takes transients
        would start sending every car to a person a day into an outage, which
        is the one thing its owner said it must not do, and a lane closed
        `full` would reopen itself. They are written back to the store alone,
        so a restart finds them too; the platform's next answer replaces them
        as it always did."""
        current = time.time() if now is None else now
        confirmed = (
            self._registers_confirmed_at if self._registers_past_bound else self._refreshed_at
        )
        self._rules = {}
        self._refreshed_at = None
        self.plans = []
        self.space_class = None
        self.spaces = None
        self.tax_sets = []
        keep_registers = (
            garage_admits(self) == ADMITS_PASSES_ONLY
            and bool(self.entitlements)
            and confirmed is not None
            and current - confirmed <= self._registers_offline_max
        )
        if keep_registers:
            # A PASS-ONLY GARAGE'S REGISTERS ARE KEPT while the platform cannot
            # be reached: at such a garage they are the only way a car gets in,
            # and wiped a day into an outage every monthly would wait for a
            # person. They are wiped by the first rules read that answers
            # (`load_payload`), which replaces them with the platform's.
            self._registers_past_bound = True
            self._registers_confirmed_at = confirmed
        else:
            self.entitlements = {}
            self.entitlements_complete = None
            self._registers_past_bound = False
            self._registers_confirmed_at = None
        with self._stays_lock:
            self.stays = {}
            self._stay_index = {}
        self.stays_cursor = None
        self.stays_refreshed_at = None
        if not keep_registers:
            self.timezone = None
        self.currency = None
        self.board = {"prices": False, "messages": []}
        self.banned = []
        if not keep_registers:
            self._coverage_index = {}
        if self._store is not None:
            self._store.wipe()
            # What is left -- the settings above and nothing else -- goes back.
            self._persist()

    def expire_if_past_bound(self, *, now: float | None = None) -> bool:
        """THE RETENTION BOUND. A cache older than `max_age_seconds` -- the age
        at which `is_stale` already stops the lane trusting it -- is wiped from
        memory and from disk. Called on every refresh tick and at start; a
        cache that is refreshed never reaches it. True if something was wiped.

        What goes is the personal data -- rules, registers, stays, the banned
        list -- and the garage's entry settings stay (`clear`), as do a
        pass-only garage's registers until the platform answers. At a garage
        that has not said what it takes, past the bound every plate already
        falls back `stale_rules`. A garage that takes transients still opens
        for every car; what the wipe costs it is the registers, so a car a
        register covered is turned away at a lane closed `full` until the
        platform answers again."""
        current = time.time() if now is None else now
        if self._registers_past_ceiling(current):
            # THE CEILING on a pass-only garage's kept registers: past it they
            # go too, and a car whose pass cannot be confirmed goes to a person.
            self.wipe_registers()
            return True
        if self._refreshed_at is None:
            return False
        if (current - self._refreshed_at) <= self._max_age:
            return False
        self.clear(now=current)
        return True

    def load_payload(self, payload: dict, *, now: float | None = None) -> None:
        """Everything `GET /lane/rules` carries, in one replacement.

        The plate rules and the default action as `load` takes them; the plans,
        the tax sets and the space class whole; the entitlements per module by the rule
        above (`register` present -> replaced, absent -> kept); and the open
        stays as a full set with their cursor, replacing whatever the deltas
        had built -- the slow cadence is the resync that bounds what a delta
        can miss (platform 0016 says why one can).
        """
        if self._registers_past_bound:
            # THE PLATFORM ANSWERS AGAIN: the registers kept past the bound
            # through the outage are wiped, and only what this answer carries
            # is held -- a module it could not read is not kept from before.
            self.entitlements = {}
            self.entitlements_complete = None
            self._registers_past_bound = False
            self._registers_confirmed_at = None
        rules = [
            Rule(plate=r["plate"], allow=bool(r.get("allow", False)), rate_plan=r.get("rate_plan"))
            for r in payload.get("plate_rules", [])
        ]
        self.load(rules, default_action=payload.get("default_action"), now=now)
        self.transient_available = transient_from_payload(payload.get("transient_available"))
        self.plans = list(payload.get("rate_plans") or [])
        self.space_class = payload.get("space_class")
        self.spaces = spaces_from_payload(payload.get("spaces"))
        self.tax_sets = list(payload.get("tax_sets") or [])
        self.timezone = payload.get("timezone")
        self.currency = payload.get("currency")
        facts = payload.get("entitlements") or {}
        for module, fact in facts.items():
            if isinstance(fact, dict) and "register" in fact:
                self.entitlements[module] = fact
        self.entitlements_complete = facts.get("complete")
        # Replaced whole, as the plans are: a platform older than the list
        # sends none, and none is an empty list.
        self.banned = banned_from_payload(payload.get("banned"))
        self._reindex_coverage()
        self._take_lane(payload)
        stays = payload.get("stays") or {}
        if "cursor" in stays:
            self.replace_stays(stays.get("open") or [], stays["cursor"], now=now)
        self._persist()

    def _take_lane(self, answer: dict) -> bool:
        """This lane's closing and board, from either read, when the answer
        carries them. A field the answer does not carry, or carries in a shape
        this build cannot read, leaves what the cache holds: a platform older
        than the closing says nothing, and saying nothing is not reopening.
        True if either changed."""
        changed = False
        lane = lane_from_payload(answer.get("lane"))
        if lane is not None and lane != self.lane:
            self.lane, changed = lane, True
        board = board_from_payload(answer.get("board"))
        if board is not None and board != self.board:
            self.board, changed = board, True
        return changed

    def apply_lane(self, answer: dict) -> None:
        """The fast read's copy of this lane's closing and board, applied and
        kept on disk. `sync_stays` calls it with every answer it is given."""
        if self._take_lane(answer):
            self._persist()

    def replace_stays(
        self, open_stays: list[dict], cursor: str, *, now: float | None = None
    ) -> None:
        """The full open set, and the cursor to continue from."""
        with self._stays_lock:
            self.stays = {s["session_id"]: s for s in open_stays}
            self._reindex_stays()
        self.stays_cursor = str(cursor)
        self.stays_refreshed_at = time.time() if now is None else now
        self._persist()

    def apply_stay_changes(
        self, changes: list[dict], cursor: str, *, now: float | None = None
    ) -> None:
        """One delta: an open row is added or updated, a closed row is dropped.

        Applied in the order the platform sent them, which is cursor order;
        the cursor moves only after every row landed, so a delta that raised
        halfway would be re-read from where it started.
        """
        with self._stays_lock:
            for stay in changes:
                if stay.get("open"):
                    self.stays[stay["session_id"]] = stay
                else:
                    self.stays.pop(stay["session_id"], None)
            self._reindex_stays()
        self.stays_cursor = str(cursor)
        self.stays_refreshed_at = time.time() if now is None else now
        self._persist()

    def drop_stay(self, session_id: str | None) -> bool:
        """The lane recorded this stay's close: it is not open here any more.

        Without it the stay stayed listed until the platform's next delta -- or
        for hours offline -- so a car that backed up and pulled forward again
        was priced a second time for the same stay, and the reader put the fee
        up again. Called on the lane thread. The stays are REPLACED, never
        changed in place, so a copy the refresh thread is writing to disk is
        never changed under it; and the disk copy itself follows at the next
        refresh that writes, because the barrier's thread does not wait on the
        disk. True if the stay was held."""
        if not session_id:
            return False
        with self._stays_lock:
            if session_id not in self.stays:
                return False
            self.stays = {sid: s for sid, s in self.stays.items() if sid != session_id}
            self._reindex_stays()
        return True

    # -- the indexes the exit reads ---------------------------------------------

    def _reindex_coverage(self) -> None:
        index: dict[str, list[tuple[str, dict, dict]]] = {}
        for module, fact in self.entitlements.items():
            register = fact.get("register") or {}
            heads = _register_heads(module, register)
            for row in register.get("registrations") or []:
                identity = row.get("vehicle_identity") or row.get("identity_normalised")
                if not identity:
                    continue
                head = heads.get(row.get("pass") or row.get("agreement"), {})
                index.setdefault(normalise_identity(identity), []).append((module, row, head))
        self._coverage_index = index

    def _reindex_stays(self) -> None:
        index: dict[str, list[dict]] = {}
        for stay in self.stays.values():
            for identity in (stay.get("plate"), stay.get("ticket_ref")):
                if identity:
                    index.setdefault(normalise_identity(identity), []).append(stay)
        self._stay_index = index

    def register_rows_for(self, identity: str) -> list[tuple[str, dict, dict]]:
        """Every register row naming this identity: (module, the row, the pass
        or agreement it names as the register lists it). One lookup."""
        return self._coverage_index.get(normalise_identity(identity), [])

    def open_stays_for(self, identity: str) -> list[dict]:
        """Every open stay under this plate or ticket. One lookup."""
        return self._stay_index.get(normalise_identity(identity), [])

    def occupancy_now_bp(self) -> int | None:
        """How full the garage is now, as the platform counts it at an open
        (platform 0034): every open stay this copy holds, against the garage's
        number of spaces, in basis points rounded down and never more than
        full. None without a number of spaces -- nothing is guessed."""
        if self.spaces is None:
            return None
        return min(FULL_BP, (FULL_BP * len(self.stays)) // self.spaces)

    def is_stale(self, *, now: float | None = None) -> bool:
        if self._refreshed_at is None:
            return True
        current = time.time() if now is None else now
        return (current - self._refreshed_at) > self._max_age

    def lookup(self, plate: str) -> Rule | None:
        return self._rules.get(plate.upper())

    def denying(self, plate: str | None) -> Rule | None:
        """The owner's DENY rule for this plate, or None: looked up as the
        rules are keyed, and then under `normalise_identity` -- so the plate a
        QR was made for, which comes in the pass module's normal form
        (`ABC123`), meets a rule the owner typed `abc-123`. Wider than
        `lookup`, and only ever used to keep a gate SHUT: an allow rule is
        never matched this way."""
        if not plate:
            return None
        exact = self.lookup(plate)
        if exact is not None:
            return None if exact.allow else exact
        key = normalise_identity(plate)
        return next(
            (r for r in self._rules.values() if not r.allow and normalise_identity(r.plate) == key),
            None,
        )

    def __len__(self) -> int:
        return len(self._rules)


def closed_to(cache: DecisionCache, direction: str | None) -> str | None:
    """How this lane is closed, as it ACTS on it: `full`, `everyone`, or None
    when it is open. `full` is an entry reason; anywhere else, and for a reason
    this build does not know, the lane acts as closed to everyone."""
    if cache.lane.get("state") != "closed":
        return None
    if cache.lane.get("reason") == CLOSED_FULL and direction != "exit":
        return CLOSED_FULL
    return CLOSED_EVERYONE


def decide(
    identity: VehicleIdentity,
    cache: DecisionCache,
    *,
    confidence_threshold: float,
    now: float | None = None,
    direction: str | None = None,
    day_at: float | None = None,
) -> Decision:
    """Turn an identification into a decision, or into an honest refusal.

    The ordering here is the safety property. Presence is checked first, then
    whether a read was obtained at all, then confidence, and confidence before
    the plate is ever used to look anything up -- so nothing that was not there
    can be identified, a failure to identify cannot be reported as an unsure
    identification, and a low-confidence read cannot match a rule by accident
    and open a barrier for the wrong vehicle.
    """
    if identity.presence is False:
        # Note `is False`, not `not identity.presence`. The latter reads
        # "nobody measured it" as "nothing is there", which would make every
        # lane without a reference view refuse every customer.
        return Decision(
            outcome=Outcome.NO_VEHICLE,
            reason="no vehicle present; refusing to transact",
            identity=identity,
        )

    closed = closed_to(cache, direction)
    if closed == CLOSED_EVERYONE:
        # CLOSED TO EVERYONE: nothing about the car can open it, so nothing
        # about the car is asked. Not a covered car, not a paid one, not a
        # read that failed -- a person's word through the intercom is the one
        # way through, and the driver is told the lane is closed rather than
        # that their plate was unclear.
        return Decision(
            outcome=Outcome.FALLBACK,
            reason="this lane is closed to everyone",
            identity=identity,
            fallback=Fallback.LANE_CLOSED,
        )

    if direction != "exit":
        admits = garage_admits(cache)
        if admits is not None:
            # THE OWNER'S ENTRY RULE, at a garage that has said what it takes.
            # Every path below is the old one, kept for a garage that has not.
            return _decide_entry(
                identity,
                cache,
                admits=admits,
                full=closed == CLOSED_FULL,
                confidence_threshold=confidence_threshold,
                stale=cache.is_stale(now=now),
                day_at=day_at if day_at is not None else (time.time() if now is None else now),
            )

    if identity.unavailable is not None:
        # BEFORE the confidence comparison, and that position is the whole
        # fix. Below this line `identity.confidence` is a number the engine
        # produced; above it, it is the 0.0 that is left when there is no
        # engine to have produced anything, and comparing it against a
        # threshold answers a question nobody measured.
        #
        # The outcome is unchanged -- FALLBACK, a human, a driver at the
        # barrier who still gets served. Only the REASON changes, and it
        # changes from one that is wrong to one that is true.
        return Decision(
            outcome=Outcome.FALLBACK,
            reason=f"no read was obtained: {identity.unavailable}",
            identity=identity,
            fallback=Fallback.ENGINE_UNREACHABLE,
        )

    if identity.confidence < confidence_threshold:
        return Decision(
            outcome=Outcome.FALLBACK,
            reason=(
                f"confidence {identity.confidence:.2f} below threshold "
                f"{confidence_threshold:.2f}"
            ),
            identity=identity,
            fallback=Fallback.LOW_CONFIDENCE,
        )

    if not identity.plate:
        return Decision(
            outcome=Outcome.FALLBACK,
            reason="no plate was read",
            identity=identity,
            fallback=Fallback.NO_PLATE_READ,
        )

    if cache.is_stale(now=now):
        # Confident read, usable plate, rules we no longer trust. Refusing to
        # decide is the only honest answer -- letting a car in on day-old
        # pricing and letting one in on no rules at all are different failures,
        # and neither should look like a normal open.
        return Decision(
            outcome=Outcome.FALLBACK,
            reason="cached rules are stale",
            identity=identity,
            fallback=Fallback.STALE_RULES,
        )

    if closed == CLOSED_FULL:
        # FULL: a car a pass or monthly register covers TODAY gets in as on any
        # day -- the same lookup, on the same local day, that the exit's
        # covered decision makes -- and every other car does not. Only a car
        # this lane has IDENTIFIED reaches here; one it could not identify fell
        # back above, to a person, as it does on any day.
        from .exit_pricing import local_day, register_cover

        # The LANE'S clock for the day, as the exit reads it (`price_exit` is
        # handed `controller.now()`); staleness above stays on `now`.
        current = day_at if day_at is not None else (time.time() if now is None else now)
        covered_by, _ = register_cover(identity.plate, cache, local_day(current, cache.timezone)[0])
        if covered_by:
            return Decision(
                outcome=Outcome.ALLOW,
                reason="this lane is closed full; a pass or monthly register covers this car",
                identity=identity,
            )
        return Decision(
            outcome=Outcome.FALLBACK,
            reason="this lane is closed full; no pass or monthly register covers this car",
            identity=identity,
            fallback=Fallback.LANE_CLOSED,
        )

    rule = cache.lookup(identity.plate)
    if rule is None:
        if cache.default_action == "deny" and direction == "exit":
            # THE GARAGE'S DEFAULT IS AN ENTRY RULE. "deny" says which strangers
            # this garage lets IN; a car at an exit is already inside, and
            # refusing it there refused every car -- pass holders and payers
            # alike -- with nothing closed. The exit lets it out, and its own
            # decision (`exit_pricing`) says covered, priced, or not priceable
            # here, as for any other car.
            return Decision(
                outcome=Outcome.ALLOW,
                reason="no rule for plate; an exit lets the car out (the garage default "
                "deny applies at entry)",
                identity=identity,
            )
        if cache.default_action == "allow":
            # "Pass holders only" is no longer decided here: a garage that has
            # said `transient_available: false` is decided by `_decide_entry`
            # above, whatever its default -- this branch only ever saw it where
            # the default was `allow`, and sent the uncovered car to a person.
            return Decision(
                outcome=Outcome.ALLOW,
                reason="no rule for plate; garage default is allow",
                identity=identity,
            )
        if cache.default_action == "deny":
            return Decision(
                outcome=Outcome.DENY,
                reason="no rule for plate; garage default is deny",
                identity=identity,
            )
        return Decision(
            outcome=Outcome.FALLBACK,
            reason="no rule for plate",
            identity=identity,
            fallback=Fallback.UNKNOWN_VEHICLE,
        )

    if not rule.allow:
        return Decision(
            outcome=Outcome.DENY,
            reason="plate is denied by rule",
            identity=identity,
            rate_plan=rule.rate_plan,
        )

    return Decision(
        outcome=Outcome.ALLOW,
        reason="plate allowed by rule",
        identity=identity,
        rate_plan=rule.rate_plan,
    )


def registers_held(cache: DecisionCache) -> bool:
    """Whether this lane holds any pass or monthly register to confirm a car
    against. None held -- never received, or a module the platform could not
    read -- is a garage at which no pass can be confirmed here."""
    return any(
        isinstance(fact, dict) and "register" in fact for fact in cache.entitlements.values()
    )


def _not_confirmed(identity: VehicleIdentity, confidence_threshold: float) -> Decision:
    """A pass-only garage's car whose pass this lane cannot confirm, because
    nobody could read its plate: to a person, unless the driver scans the
    car's registration QR (`qr_may_open`). The fallback names why it was not
    read."""
    if identity.unavailable is not None:
        fallback = Fallback.ENGINE_UNREACHABLE
    elif not identity.plate:
        fallback = Fallback.NO_PLATE_READ
    else:
        fallback = Fallback.LOW_CONFIDENCE
    return Decision(
        outcome=Outcome.FALLBACK,
        reason="pass or monthly only; the pass could not be confirmed: "
        + _seen(identity, confidence_threshold, stale=False),
        identity=identity,
        fallback=fallback,
        qr_may_open=True,
    )


def _decide_entry(
    identity: VehicleIdentity,
    cache: DecisionCache,
    *,
    admits: str,
    full: bool,
    confidence_threshold: float,
    stale: bool,
    day_at: float,
) -> Decision:
    """The entry at a garage that has said what it takes. Presence and a
    closing to everyone were decided before this, as for every lane.

    THE OWNER'S DENY COMES FIRST (2026-10-30): a plate the owner denied --
    read at the lane's confidence or below it (`plate_seen`), while the cache
    is trusted -- is DENIED before anything else is asked, at both kinds of
    garage, and is never a refusal a QR may open. A plate read below the
    confidence used to fall to "the pass could not be confirmed" before the
    rule was looked up, and a QR then let the denied car in.

    WHAT THE LANE STANDS BEHIND is `plate_read`: only a plate read at the
    lane's confidence is looked up to OPEN anything, in the rules or the
    registers, so a car nobody could read is never matched to someone else's
    pass. AN OWNER'S RULE for a plate, deny or allow, is consulted only while
    the cache is trusted -- a stale rule is never applied, as before.
    (The platform serves no rules today; a lane given one honours it.)

    A GARAGE THAT TAKES TRANSIENTS:
      1. full (`closed: full`): a car a register covers today gets in; every
         other car does not -- a person's word is the way past it;
      2. an owner's rule: deny keeps the gate shut, allow opens with its plan;
      3. every other car opens. What was seen goes in the reason, and the stay
         the car gets carries it (`LaneController._stay_identity`).

    A MONTHLY / PASS-ONLY GARAGE (1, 4 and 6 are the refusals a registration
    QR scanned at the lane may still open -- `qr_may_open`):
      1. a plate nobody could read, or read below the confidence: its pass
         cannot be confirmed -- a person, the fallback naming why;
      2. full: a car a register covers today opens; with no register held, a
         person; every other car is DENIED, the gate shut;
      3. an owner's rule, as above -- before the registers, as it always was;
      4. no register held at all: no pass can be confirmed here -- a person;
      5. a car a register covers today: open;
      6. every other car is DENIED (`REASON_NOT_REGISTERED`, the QR seam).
    """
    denied = owner_denies(cache, plate_seen(identity), stale=stale)
    if denied is not None:
        return _by_rule(denied, identity)
    read = plate_read(identity, confidence_threshold)
    covered = False
    if read:
        from .exit_pricing import local_day, register_cover

        covered_by, _ = register_cover(read, cache, local_day(day_at, cache.timezone)[0])
        covered = bool(covered_by)
    rule = cache.lookup(read) if read and not stale else None

    if admits == ADMITS_PASSES_ONLY:
        no_register = Decision(
            outcome=Outcome.FALLBACK,
            reason="pass or monthly only; the pass could not be confirmed: no register "
            "is held here",
            identity=identity,
            fallback=Fallback.STALE_RULES,
        )
        if read is None:
            return _not_confirmed(identity, confidence_threshold)
        if full:
            # Full first, as it always came: a covered car still gets in.
            if covered:
                return Decision(
                    outcome=Outcome.ALLOW,
                    reason="this lane is closed full; a pass or monthly register covers this "
                    "car",
                    identity=identity,
                )
            if not registers_held(cache):
                return no_register
            return Decision(
                outcome=Outcome.DENY,
                reason="this lane is closed full; no pass or monthly register covers this car",
                identity=identity,
            )
        # THE OWNER'S RULE FOR THIS PLATE BEFORE THE REGISTERS, in the order it
        # always had: a plate the owner denied stays out even with a pass.
        if rule is not None:
            return _by_rule(rule, identity)
        if not registers_held(cache):
            return replace(no_register, qr_may_open=True)
        if covered:
            return Decision(
                outcome=Outcome.ALLOW,
                reason="pass or monthly only; a pass or monthly register covers this car",
                identity=identity,
            )
        # THE QR SEAM: see `REASON_NOT_REGISTERED`.
        return Decision(
            outcome=Outcome.DENY, reason=REASON_NOT_REGISTERED, identity=identity, qr_may_open=True
        )

    if full:
        if covered:
            return Decision(
                outcome=Outcome.ALLOW,
                reason="this lane is closed full; a pass or monthly register covers this car",
                identity=identity,
            )
        return Decision(
            outcome=Outcome.FALLBACK,
            reason="this lane is closed full; no pass or monthly register covers this car",
            identity=identity,
            fallback=Fallback.LANE_CLOSED,
        )
    if rule is not None:
        return _by_rule(rule, identity)
    return Decision(
        outcome=Outcome.ALLOW,
        reason="garage takes transients; "
        + _seen(identity, confidence_threshold, stale=stale),
        identity=identity,
    )


def _by_rule(rule: Rule, identity: VehicleIdentity) -> Decision:
    if not rule.allow:
        return Decision(
            outcome=Outcome.DENY,
            reason="plate is denied by rule",
            identity=identity,
            rate_plan=rule.rate_plan,
        )
    return Decision(
        outcome=Outcome.ALLOW,
        reason="plate allowed by rule",
        identity=identity,
        rate_plan=rule.rate_plan,
    )
