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
    assert kinds == ["frames_captured", "vehicle_identified", "decision", "vended"]


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
