"""The decision stage in isolation, including every fallback path."""

import pytest

from lane_controller import DecisionCache, Fallback, Outcome, Rule, VehicleIdentity, decide


def identity(plate="SIM-0001", confidence=0.97):
    return VehicleIdentity(plate=plate, confidence=confidence)


@pytest.fixture
def cache():
    c = DecisionCache()
    c.load(
        [
            Rule(plate="SIM-0001", allow=True, rate_plan="monthly"),
            Rule(plate="BANNED-1", allow=False),
        ]
    )
    return c


def test_confident_known_plate_is_allowed(cache):
    decision = decide(identity(), cache, confidence_threshold=0.85)
    assert decision.outcome is Outcome.ALLOW
    assert decision.should_vend
    assert decision.rate_plan == "monthly"


def test_denied_plate_is_denied_not_fallback(cache):
    decision = decide(identity(plate="BANNED-1"), cache, confidence_threshold=0.85)
    assert decision.outcome is Outcome.DENY
    assert not decision.should_vend
    assert decision.fallback is None


def test_low_confidence_falls_back(cache):
    decision = decide(identity(confidence=0.42), cache, confidence_threshold=0.85)
    assert decision.outcome is Outcome.FALLBACK
    assert decision.fallback is Fallback.LOW_CONFIDENCE
    assert not decision.should_vend


def test_low_confidence_is_checked_before_the_plate_is_used(cache):
    """A low-confidence read of an allowed plate must NOT be allowed.

    This is the ordering guarantee. If confidence were checked after the rule
    lookup, an unsure read that happened to resolve to a known plate would open
    the barrier -- exactly the silent wrong answer the lane must not produce.
    """
    decision = decide(identity(plate="SIM-0001", confidence=0.10), cache, confidence_threshold=0.85)
    assert decision.outcome is Outcome.FALLBACK
    assert decision.fallback is Fallback.LOW_CONFIDENCE


def test_no_plate_falls_back(cache):
    decision = decide(identity(plate=None), cache, confidence_threshold=0.85)
    assert decision.fallback is Fallback.NO_PLATE_READ


def test_unknown_plate_falls_back_rather_than_denying(cache):
    decision = decide(identity(plate="NEVER-SEEN"), cache, confidence_threshold=0.85)
    assert decision.outcome is Outcome.FALLBACK
    assert decision.fallback is Fallback.UNKNOWN_VEHICLE


def test_stale_rules_fall_back():
    # Loaded at t=1000 against a one-day maximum age, asked a day and a half later.
    cache = DecisionCache(max_age_seconds=86_400.0)
    cache.load([Rule(plate="SIM-0001", allow=True)], now=1_000.0)

    fresh = decide(identity(), cache, confidence_threshold=0.85, now=1_000.0 + 3_600)
    assert fresh.outcome is Outcome.ALLOW, "control: the same call must succeed while fresh"

    stale = decide(identity(), cache, confidence_threshold=0.85, now=1_000.0 + 129_600)
    assert stale.outcome is Outcome.FALLBACK
    assert stale.fallback is Fallback.STALE_RULES


def test_an_empty_cache_is_stale():
    assert DecisionCache().is_stale()


def test_plate_matching_is_case_insensitive(cache):
    decision = decide(identity(plate="sim-0001"), cache, confidence_threshold=0.85)
    assert decision.outcome is Outcome.ALLOW
