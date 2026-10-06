"""The decision stage: local rules, local cache, no network on the hot path.

A lane with no internet still has to work. Every decision below is made from
rules already on the box; the server is how the cache gets refreshed and where
events are reported afterwards, never something the barrier waits on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
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

    @property
    def should_vend(self) -> bool:
        return self.outcome is Outcome.ALLOW


@dataclass(frozen=True, slots=True)
class Rule:
    """One cached allow/pricing rule."""

    plate: str
    allow: bool
    rate_plan: str | None = None


def normalise_identity(text: str) -> str:
    """The one folding under which a plate the camera read and an identity a
    registrar typed are compared: upper-case, letters and digits only.
    `AB-123`, `ab 123` and `AB123` are one identity here.

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
    return "".join(ch for ch in text.upper() if ch.isalnum())


def _register_heads(module: str, register: dict) -> dict[str, dict]:
    """The pass or agreement entries of a register, by id, as the modules'
    `show-garage-register` verbs list them (garage-pass G27: `passes[]` keyed
    `pass`; monthly-billing G48: `agreements[]` keyed `agreement`)."""
    if module == "garage_pass":
        return {p.get("pass"): p for p in register.get("passes") or [] if p.get("pass")}
    return {a.get("agreement"): a for a in register.get("agreements") or [] if a.get("agreement")}


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

    def __init__(self, *, max_age_seconds: float = 86_400.0, store=None) -> None:
        self._rules: dict[str, Rule] = {}
        self._refreshed_at: float | None = None
        self._max_age = max_age_seconds
        #: `durable.DurableStore`, or None. Anything with `read() -> dict|None`,
        #: `write(dict)` and `wipe()`.
        self._store = store
        # What to do with a confidently-read plate that has no rule. None means
        # fall back -- the safe default, and the one that applies until the
        # platform has said otherwise. A transient garage syncs "allow"; a
        # permit-only garage will sync nothing and keep falling back.
        self.default_action: str | None = None
        # WHAT THE EXIT WILL DECIDE FROM, held as the platform hands it and
        # consumed by nothing in this file yet: the decision that reads these
        # is its own round. This round only keeps them fresh.
        #
        # The garage's rate plans, whole, and its space class -- what
        # `rate_engine.quote` takes.
        self.plans: list = []
        self.space_class: str | None = None
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
        # THE TWO INDEXES THE EXIT READS ON THE BARRIER'S PATH, rebuilt whole
        # on every change so the read is one dict lookup and never a scan:
        # identity -> the register rows that name it, per module; identity ->
        # the open stays under it. Keys are `normalise_identity`'d, so the
        # plate the camera read and the identity a registrar typed meet.
        self._coverage_index: dict[str, list[tuple[str, dict, dict]]] = {}
        self._stay_index: dict[str, list[dict]] = {}
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
            "refreshed_at": self._refreshed_at,
            "plans": self.plans,
            "space_class": self.space_class,
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
        }

    def _persist(self) -> None:
        if self._store is not None:
            self._store.write(self.state())

    def _restore(self, *, now: float | None = None) -> bool:
        """What the store holds, into memory -- unless it is past the bound,
        in which case it is wiped rather than loaded. True if restored."""
        state = self._store.read()
        if not state or state.get("refreshed_at") is None:
            return False
        current = time.time() if now is None else now
        if (current - float(state["refreshed_at"])) > self._max_age:
            # Personal data past its bound is not held, and not even read
            # back into memory on the way to being discarded.
            self._store.wipe()
            return False
        self._rules = {
            r["plate"].upper(): Rule(
                plate=r["plate"], allow=bool(r["allow"]), rate_plan=r.get("rate_plan")
            )
            for r in state.get("rules") or []
        }
        self.default_action = state.get("default_action")
        self._refreshed_at = float(state["refreshed_at"])
        self.plans = list(state.get("plans") or [])
        self.space_class = state.get("space_class")
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
        self._reindex_coverage()
        self._reindex_stays()
        return True

    def clear(self) -> None:
        """Hold nothing: memory and disk. The empty cache is the one this
        object started as -- stale, no rules, falling back on every plate."""
        self._rules = {}
        self._refreshed_at = None
        self.default_action = None
        self.plans = []
        self.space_class = None
        self.tax_sets = []
        self.entitlements = {}
        self.entitlements_complete = None
        self.stays = {}
        self.stays_cursor = None
        self.stays_refreshed_at = None
        self.timezone = None
        self.currency = None
        self.lane = dict(OPEN_LANE)
        self.board = {"prices": False, "messages": []}
        self._coverage_index = {}
        self._stay_index = {}
        if self._store is not None:
            self._store.wipe()

    def expire_if_past_bound(self, *, now: float | None = None) -> bool:
        """THE RETENTION BOUND. A cache older than `max_age_seconds` -- the age
        at which `is_stale` already stops the lane trusting it -- is wiped from
        memory and from disk. Called on every refresh tick and at start; a
        cache that is refreshed never reaches it. True if something was wiped.

        Wiping changes no decision: past the bound every plate already falls
        back `stale_rules`, so what goes is only the personal data the box was
        holding for no one."""
        if self._refreshed_at is None:
            return False
        current = time.time() if now is None else now
        if (current - self._refreshed_at) <= self._max_age:
            return False
        self.clear()
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
        rules = [
            Rule(plate=r["plate"], allow=bool(r.get("allow", False)), rate_plan=r.get("rate_plan"))
            for r in payload.get("plate_rules", [])
        ]
        self.load(rules, default_action=payload.get("default_action"), now=now)
        self.plans = list(payload.get("rate_plans") or [])
        self.space_class = payload.get("space_class")
        self.tax_sets = list(payload.get("tax_sets") or [])
        self.timezone = payload.get("timezone")
        self.currency = payload.get("currency")
        facts = payload.get("entitlements") or {}
        for module, fact in facts.items():
            if isinstance(fact, dict) and "register" in fact:
                self.entitlements[module] = fact
        self.entitlements_complete = facts.get("complete")
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
        self.stays = {s["session_id"]: s for s in open_stays}
        self.stays_cursor = str(cursor)
        self.stays_refreshed_at = time.time() if now is None else now
        self._reindex_stays()
        self._persist()

    def apply_stay_changes(
        self, changes: list[dict], cursor: str, *, now: float | None = None
    ) -> None:
        """One delta: an open row is added or updated, a closed row is dropped.

        Applied in the order the platform sent them, which is cursor order;
        the cursor moves only after every row landed, so a delta that raised
        halfway would be re-read from where it started.
        """
        for stay in changes:
            if stay.get("open"):
                self.stays[stay["session_id"]] = stay
            else:
                self.stays.pop(stay["session_id"], None)
        self.stays_cursor = str(cursor)
        self.stays_refreshed_at = time.time() if now is None else now
        self._reindex_stays()
        self._persist()

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

    def is_stale(self, *, now: float | None = None) -> bool:
        if self._refreshed_at is None:
            return True
        current = time.time() if now is None else now
        return (current - self._refreshed_at) > self._max_age

    def lookup(self, plate: str) -> Rule | None:
        return self._rules.get(plate.upper())

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
        if cache.default_action == "allow":
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
