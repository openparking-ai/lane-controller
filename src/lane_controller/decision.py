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


@dataclass(frozen=True, slots=True)
class Decision:
    outcome: Outcome
    reason: str
    identity: VehicleIdentity
    fallback: Fallback | None = None
    rate_plan: str | None = None

    @property
    def should_vend(self) -> bool:
        return self.outcome is Outcome.ALLOW


@dataclass(frozen=True, slots=True)
class Rule:
    """One cached allow/pricing rule."""

    plate: str
    allow: bool
    rate_plan: str | None = None


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
            "entitlements": self.entitlements,
            "entitlements_complete": self.entitlements_complete,
            "stays": self.stays,
            "stays_cursor": self.stays_cursor,
            "stays_refreshed_at": self.stays_refreshed_at,
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
        self.entitlements = dict(state.get("entitlements") or {})
        self.entitlements_complete = state.get("entitlements_complete")
        self.stays = dict(state.get("stays") or {})
        self.stays_cursor = state.get("stays_cursor")
        self.stays_refreshed_at = state.get("stays_refreshed_at")
        return True

    def clear(self) -> None:
        """Hold nothing: memory and disk. The empty cache is the one this
        object started as -- stale, no rules, falling back on every plate."""
        self._rules = {}
        self._refreshed_at = None
        self.default_action = None
        self.plans = []
        self.space_class = None
        self.entitlements = {}
        self.entitlements_complete = None
        self.stays = {}
        self.stays_cursor = None
        self.stays_refreshed_at = None
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

        The plate rules and the default action as `load` takes them; the plans
        and the space class whole; the entitlements per module by the rule
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
        facts = payload.get("entitlements") or {}
        for module, fact in facts.items():
            if isinstance(fact, dict) and "register" in fact:
                self.entitlements[module] = fact
        self.entitlements_complete = facts.get("complete")
        stays = payload.get("stays") or {}
        if "cursor" in stays:
            self.replace_stays(stays.get("open") or [], stays["cursor"], now=now)
        self._persist()

    def replace_stays(
        self, open_stays: list[dict], cursor: str, *, now: float | None = None
    ) -> None:
        """The full open set, and the cursor to continue from."""
        self.stays = {s["session_id"]: s for s in open_stays}
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
        for stay in changes:
            if stay.get("open"):
                self.stays[stay["session_id"]] = stay
            else:
                self.stays.pop(stay["session_id"], None)
        self.stays_cursor = str(cursor)
        self.stays_refreshed_at = time.time() if now is None else now
        self._persist()

    def is_stale(self, *, now: float | None = None) -> bool:
        if self._refreshed_at is None:
            return True
        current = time.time() if now is None else now
        return (current - self._refreshed_at) > self._max_age

    def lookup(self, plate: str) -> Rule | None:
        return self._rules.get(plate.upper())

    def __len__(self) -> int:
        return len(self._rules)


def decide(
    identity: VehicleIdentity,
    cache: DecisionCache,
    *,
    confidence_threshold: float,
    now: float | None = None,
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
