"""The whole lane sequence, end to end, against simulated hardware."""

from lane_controller import Fallback, Outcome, VehicleIdentity


def test_a_confident_known_vehicle_gets_the_gate(lane):
    controller, vend = lane([VehicleIdentity(plate="SIM-0001", confidence=0.97)])

    decision = controller.run_once()

    assert decision.outcome is Outcome.ALLOW
    assert vend.vend_count == 1, "the barrier should have been vended exactly once"


def test_a_low_confidence_read_does_not_get_the_gate(lane):
    controller, vend = lane([VehicleIdentity(plate="SIM-0001", confidence=0.40)])

    decision = controller.run_once()

    assert decision.outcome is Outcome.FALLBACK
    assert decision.fallback is Fallback.LOW_CONFIDENCE
    assert vend.vend_count == 0, "an unsure read must never open the barrier"


def test_a_denied_vehicle_does_not_get_the_gate(lane):
    controller, vend = lane([VehicleIdentity(plate="BANNED-1", confidence=0.99)])

    assert controller.run_once().outcome is Outcome.DENY
    assert vend.vend_count == 0


def test_the_controller_cannot_close_the_barrier(lane):
    """The absence of a close path is a design guarantee, so it is asserted.

    The barrier closes on its own closing loop. If someone ever adds a close
    method to a vend output, this fails and the reason is in CONTRIBUTING.md.
    """
    _, vend = lane([VehicleIdentity(plate="SIM-0001", confidence=0.97)])

    assert not hasattr(vend, "close")
    assert not hasattr(vend, "close_gate")
    assert not hasattr(vend, "lower")


def test_every_stage_is_reported_as_an_event(lane):
    controller, _ = lane([VehicleIdentity(plate="SIM-0001", confidence=0.97)])
    controller.run_once()

    kinds = [e.kind for e in list(controller.events._queue)]
    assert kinds == ["frames_captured", "vehicle_identified", "decision", "vended", "session_open"]


def test_a_fallback_is_recorded_not_swallowed(lane):
    controller, _ = lane([VehicleIdentity(plate=None, confidence=0.99)])
    controller.run_once()

    decisions = [e for e in list(controller.events._queue) if e.kind == "decision"]
    assert len(decisions) == 1
    assert decisions[0].detail["outcome"] == "fallback"
    assert decisions[0].detail["fallback"] == "no_plate_read"


def test_the_lane_decides_with_no_server_configured(lane):
    """No transport, no server URL, nothing to reach. The gate still works.

    This is the offline guarantee in its smallest form: the queue has nowhere
    to flush to and the decision is unaffected.
    """
    controller, vend = lane([VehicleIdentity(plate="SIM-0001", confidence=0.97)])

    assert controller.run_once().outcome is Outcome.ALLOW
    assert vend.vend_count == 1
    assert controller.events.pending > 0, "events are held, not lost, while offline"


def test_run_once_returns_none_when_no_vehicle_arrives(lane):
    controller, vend = lane([VehicleIdentity(plate="SIM-0001", confidence=0.97)], arrivals=0)

    assert controller.run_once(timeout=0.01) is None
    assert vend.vend_count == 0


def test_several_vehicles_in_a_row(lane):
    controller, vend = lane(
        [
            VehicleIdentity(plate="SIM-0001", confidence=0.97),
            VehicleIdentity(plate="BANNED-1", confidence=0.97),
            VehicleIdentity(plate="SIM-0001", confidence=0.30),
        ],
        arrivals=3,
    )

    outcomes = [controller.run_once().outcome for _ in range(3)]

    assert outcomes == [Outcome.ALLOW, Outcome.DENY, Outcome.FALLBACK]
    assert vend.vend_count == 1


def test_an_empty_but_synced_cache_is_not_thrown_away(config):
    """Regression: DecisionCache defines __len__, so an empty one is falsy.

    A garage with no per-plate rules -- which is every transient garage --
    syncs a cache of length zero. `cache or DecisionCache()` replaced it with a
    fresh, never-refreshed cache, which reports STALE, which sends every single
    vehicle to fallback. The gate would have stopped working for the most
    ordinary configuration there is.
    """
    from lane_controller import DecisionCache, LaneController
    from lane_controller.simulated import (
        CannedCameraFeed,
        RecordingVendOutput,
        SimulatedLoopInput,
        StubVehicleIdentifier,
    )

    empty_but_fresh = DecisionCache()
    empty_but_fresh.load([], default_action="allow")
    assert len(empty_but_fresh) == 0
    assert not empty_but_fresh.is_stale()

    controller = LaneController(
        config,
        loop=SimulatedLoopInput(),
        camera=CannedCameraFeed(),
        vend=RecordingVendOutput(),
        identifier=StubVehicleIdentifier(),
        cache=empty_but_fresh,
    )
    assert controller.cache is empty_but_fresh
    assert controller.run_once().outcome is Outcome.ALLOW


def test_no_vehicle_present_records_a_rejection_and_transacts_nothing(lane):
    """D3, at the controller. The lane must issue no ticket, open no session and
    not vend -- and the refusal must be RECORDED, so a lane being worked by
    someone tripping the loop shows up as a pattern instead of as silence.
    """
    controller, vend = lane([VehicleIdentity(plate=None, confidence=0.0, presence=False)])

    decision = controller.run_once()

    assert decision.outcome is Outcome.NO_VEHICLE
    assert vend.vend_count == 0, "the barrier was told to open with no car there"

    kinds = [event.kind for event in list(controller.events._queue)]
    assert "arming_rejected" in kinds
    assert "session_open" not in kinds, "a session was opened with no vehicle present"
    assert "vended" not in kinds

    rejection = next(e for e in list(controller.events._queue) if e.kind == "arming_rejected")
    # `events` is append-only by grant, so the retention purge cannot reach what
    # is written here. Nothing identifying goes in.
    assert "plate" not in rejection.detail
    assert rejection.detail["reason"]


def test_an_unreadable_plate_still_opens_the_fallback_path(lane):
    """D4, at the controller. A car with a filthy plate is a legitimate entry:
    a different event, a different outcome, and a human involved."""
    controller, vend = lane([VehicleIdentity(plate=None, confidence=0.0, presence=True)])

    decision = controller.run_once()

    assert decision.outcome is Outcome.FALLBACK
    kinds = [event.kind for event in list(controller.events._queue)]
    assert "fallback_needs_human" in kinds
    assert "arming_rejected" not in kinds
    assert vend.vend_count == 0
