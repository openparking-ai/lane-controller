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

    Stubbed in deliberately: the storage is a dict today and will become
    something durable on the Jetson (SQLite on the controller is the intended
    shape). The behaviour that matters is already fixed and already tested --
    what the cache does when it is empty, and what it does when it is stale.
    """

    def __init__(self, *, max_age_seconds: float = 86_400.0) -> None:
        self._rules: dict[str, Rule] = {}
        self._refreshed_at: float | None = None
        self._max_age = max_age_seconds
        # What to do with a confidently-read plate that has no rule. None means
        # fall back -- the safe default, and the one that applies until the
        # platform has said otherwise. A transient garage syncs "allow"; a
        # permit-only garage will sync nothing and keep falling back.
        self.default_action: str | None = None

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

    The ordering here is the safety property. Presence is checked before
    confidence, and confidence before the plate is ever used to look anything
    up -- so nothing that was not there can be identified, and a low-confidence
    read cannot match a rule by accident and open a barrier for the wrong
    vehicle.
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
