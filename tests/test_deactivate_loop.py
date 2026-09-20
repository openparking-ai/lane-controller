"""The deactivate loop: a third loop, BEFORE the arming loops, that HOLDS arming.

While it reads occupied the lane does not arm. A second vehicle that close
behind the one at the barrier would follow it through an open boom, so the boom
stays down until it backs off. What this file pins:

  D1  the rule is a LEVEL, re-read every turn: the hold ends the moment the
      loop clears, on whichever turn reads it -- an arrival, or an idle turn on
      which a one-shot loop driver would never report the held car again.
  D2  the interval is RECORDED at both ends: `arming_suppressed` once at the
      start, `arming_suppression_ended` once at the end, saying which end --
      `armed` (loop cleared, car still there, lane armed for it) or
      `arming_loop_cleared` (car left without being armed for).
  D3  the assisted vend route refuses `vehicle_too_close` from the SAME check,
      so the intercom is not a door around the loop.
  D4  `deactivate_loop_stuck_occupied` is measured the way the arming loop's
      code is, against the same bound.
  D5  the geometry is declared, bounded below, and refused when it disagrees
      with the wiring -- and a lane that declares no such loop is never held,
      never refused, and writes `deactivate_loops: 0` on every vehicle.

Every hold test has a control beside it in which the deactivate loop reads
clear and the same lane arms. A hold that cannot be released is the deadlock
the level exists to prevent; a hold that is never recorded is the silent
non-event the record exists to prevent.
"""

from __future__ import annotations

import pytest

from lane_controller import (
    CameraConfig,
    DecisionCache,
    GateConfig,
    LaneConfig,
    LaneController,
    LoopConfig,
    VehicleIdentity,
)
from lane_controller.config import DEACTIVATE_SPACING_MIN_M
from lane_controller.contract import VEND_BLOCKING, HealthState, MalfunctionCode, VendRefusal
from lane_controller.events import EventQueue
from lane_controller.interfaces import ClosingSequence, LoopInput
from lane_controller.service import LaneService
from lane_controller.simulated import (
    CannedCameraFeed,
    OccupancyLoopInput,
    RecordingVendOutput,
    ScriptedClosingLoops,
    StubVehicleIdentifier,
)
from lane_controller.sync import (
    ARMED,
    ARMING_SUPPRESSED,
    ARMING_SUPPRESSION_ENDED,
    REASON_VEHICLE_TOO_CLOSE,
    SUPPRESSION_ENDED_ARMED,
    SUPPRESSION_ENDED_ARMING_LOOP_CLEARED,
)

READABLE = [VehicleIdentity(plate="SIM-0001", confidence=0.97, presence=True)]
UNREADABLE = [VehicleIdentity(plate=None, confidence=0.10, presence=True)]


class EdgeLoop(LoopInput):
    """An arming loop whose driver reports each arrival ONCE.

    `wait_for_vehicle` answers True exactly once per arrival and False after,
    while `is_occupied` keeps saying what is on the loop. This is the driver
    shape under which an edge-triggered hold would deadlock: the held car is
    never reported again, so only an idle turn can end its hold.
    """

    def __init__(self) -> None:
        self._pending = 0
        self.occupied = False

    def arrive(self) -> None:
        self._pending += 1
        self.occupied = True

    def leave(self) -> None:
        self.occupied = False

    def wait_for_vehicle(self, timeout: float | None = None) -> bool:
        if self._pending <= 0:
            return False
        self._pending -= 1
        return True

    def is_occupied(self) -> bool:
        return self.occupied


def a_lane(
    *,
    deactivate_loops: int = 1,
    identities=None,
    crossings=None,
    clock=None,
    arming_loop_max_occupied_s: float | None = None,
):
    """The standard installation plus a deactivate loop 6 m back, or without one.

    No crossing is scripted after the gate unless a test asks for one: the idle
    turns the hold tests take read those loops, and a scripted crossing with
    nothing pending would land as `entry_unadmitted` in the middle of the record
    under test.
    """
    extra = (
        {"arming_loop_max_occupied_s": arming_loop_max_occupied_s}
        if arming_loop_max_occupied_s is not None
        else {}
    )
    config = LaneConfig(
        lane_id="lane-1",
        site_id="site-1",
        direction="entry",
        camera=CameraConfig(camera_id="sim-cam-1", rtsp_url="", frames_per_read=1),
        gate=GateConfig(),
        loops=LoopConfig(
            arming_loops=2,
            arming_spacing_m=1.5,
            closing_loops=2,
            closing_spacing_m=1.5,
            confirmation_window_seconds=10.0,
            deactivate_loops=deactivate_loops,
            deactivate_spacing_m=DEACTIVATE_SPACING_MIN_M if deactivate_loops else 0.0,
        ),
        **extra,
    )
    cache = DecisionCache()
    cache.load([])
    cache.default_action = "allow"
    loop = EdgeLoop()
    deactivate = OccupancyLoopInput(False) if deactivate_loops else None
    controller = LaneController(
        config,
        loop=loop,
        camera=CannedCameraFeed(),
        vend=RecordingVendOutput(),
        identifier=StubVehicleIdentifier(identities or READABLE),
        arming_loop_b=OccupancyLoopInput(True),
        closing_loops=ScriptedClosingLoops(crossings or []),
        deactivate_loop=deactivate,
        cache=cache,
        events=EventQueue(),
        **({"clock": clock} if clock is not None else {}),
    )
    return controller, loop, deactivate


def kinds(controller) -> list[str]:
    return [event.kind for event in list(controller.events._queue)]


def details(controller, kind: str) -> list[dict]:
    return [e.detail for e in list(controller.events._queue) if e.kind == kind]


# ---------------------------------------------------------------------------
# D1 / D2 -- the hold, its release on either end, and its record
# ---------------------------------------------------------------------------


def test_a_vehicle_on_the_deactivate_loop_holds_the_arming_and_the_hold_is_recorded_once():
    controller, loop, deactivate = a_lane()
    deactivate.set_occupied(True)
    loop.arrive()

    assert controller.run_once() is None
    assert controller.arming_suppressed
    assert kinds(controller) == [ARMING_SUPPRESSED]
    assert controller.vend.vend_count == 0

    # The turns that follow with both loops unchanged add NOTHING to the record:
    # the interval is one thing. Three idle turns, then three turns on which a
    # level-reporting driver reports the same held car again -- still one event.
    for _ in range(3):
        assert controller.run_once(timeout=0) is None
    for _ in range(3):
        loop.arrive()
        assert controller.run_once() is None
    assert kinds(controller) == [ARMING_SUPPRESSED]
    (start,) = details(controller, ARMING_SUPPRESSED)
    assert start["reason"] == REASON_VEHICLE_TOO_CLOSE
    assert start["geometry_assumed"]["deactivate_loops"] == 1
    assert start["geometry_assumed"]["deactivate_spacing_m"] == DEACTIVATE_SPACING_MIN_M


def test_the_control_the_same_lane_arms_when_the_deactivate_loop_is_clear():
    controller, loop, deactivate = a_lane()
    deactivate.set_occupied(False)
    loop.arrive()

    decision = controller.run_once()
    assert decision is not None
    assert kinds(controller)[0] == ARMED
    assert ARMING_SUPPRESSED not in kinds(controller)
    assert controller.vend.vend_count == 1


def test_the_hold_ends_the_first_way_when_the_loop_clears_with_the_car_still_there():
    """LEVEL, NOT EDGE. The driver reported the car once; it is still on the
    arming loop; the car behind backs off. The next IDLE turn -- the only turn
    there will ever be for this car -- must end the hold and arm for it."""
    controller, loop, deactivate = a_lane()
    deactivate.set_occupied(True)
    loop.arrive()
    assert controller.run_once() is None
    assert controller.arming_suppressed

    deactivate.set_occupied(False)  # the car behind backs off; ours is still there
    decision = controller.run_once(timeout=0)  # no arrival is reported: idle turn

    assert decision is not None, "an edge-triggered hold: the held car was never served"
    assert not controller.arming_suppressed
    assert kinds(controller)[:3] == [ARMING_SUPPRESSED, ARMING_SUPPRESSION_ENDED, ARMED]
    (end,) = details(controller, ARMING_SUPPRESSION_ENDED)
    assert end["ended_by"] == SUPPRESSION_ENDED_ARMED
    assert end["held_for_s"] >= 0.0
    assert controller.vend.vend_count == 1


def test_the_hold_ends_the_second_way_when_the_car_leaves_without_being_armed_for():
    """The held car reverses out, or follows the car ahead through. Either way
    the interval CLOSES, on the idle turn that finds the arming loop clear, and
    the lane does not arm for a car that is not there."""
    controller, loop, deactivate = a_lane()
    deactivate.set_occupied(True)
    loop.arrive()
    assert controller.run_once() is None

    loop.leave()
    assert controller.run_once(timeout=0) is None

    assert not controller.arming_suppressed
    assert kinds(controller) == [ARMING_SUPPRESSED, ARMING_SUPPRESSION_ENDED]
    (end,) = details(controller, ARMING_SUPPRESSION_ENDED)
    assert end["ended_by"] == SUPPRESSION_ENDED_ARMING_LOOP_CLEARED
    assert controller.vend.vend_count == 0


def test_the_hold_also_ends_on_an_arrival_turn_when_the_driver_reports_the_car_again():
    """A driver that reports the level (True while occupied) reaches the
    release through the arrival branch. Same two events, same order."""
    controller, loop, deactivate = a_lane()
    deactivate.set_occupied(True)
    loop.arrive()
    assert controller.run_once() is None

    deactivate.set_occupied(False)
    loop.arrive()  # the driver reports it again
    assert controller.run_once() is not None
    assert kinds(controller)[:3] == [ARMING_SUPPRESSED, ARMING_SUPPRESSION_ENDED, ARMED]


def test_the_held_interval_is_timed_on_the_lanes_own_clock():
    now = [1_000.0]
    controller, loop, deactivate = a_lane(clock=lambda: now[0])
    deactivate.set_occupied(True)
    loop.arrive()
    controller.run_once()
    now[0] += 4.25
    loop.leave()
    controller.run_once(timeout=0)
    (end,) = details(controller, ARMING_SUPPRESSION_ENDED)
    assert end["held_for_s"] == 4.25


def test_a_second_hold_after_a_release_is_a_second_interval_with_its_own_record():
    controller, loop, deactivate = a_lane()
    for _ in range(2):
        deactivate.set_occupied(True)
        loop.arrive()
        controller.run_once()
        loop.leave()
        controller.run_once(timeout=0)
    assert kinds(controller) == [
        ARMING_SUPPRESSED,
        ARMING_SUPPRESSION_ENDED,
        ARMING_SUPPRESSED,
        ARMING_SUPPRESSION_ENDED,
    ]


# ---------------------------------------------------------------------------
# D3 -- the assisted vend applies the same check
# ---------------------------------------------------------------------------


def test_the_assisted_vend_refuses_vehicle_too_close_from_the_same_check():
    from lane_controller.vend import parse

    controller, loop, deactivate = a_lane(
        identities=UNREADABLE, crossings=[(ClosingSequence.FORWARD, 0.0)]
    )
    loop.arrive()
    controller.run_once()  # falls back: the case the intercom exists for
    service = LaneService(controller)

    def complete(key):
        return service.assisted.complete(
            parse(
                {
                    "authorised_by": "human_open_now",
                    "identity": {"kind": "ticket", "ticket_ref": "TICKET-000001"},
                    "decision_at": controller.last_decision_at,
                },
                key,
            )
        )

    deactivate.set_occupied(True)
    status, refused = complete("KEY-001")
    assert status == 409
    assert refused["code"] == VendRefusal.VEHICLE_TOO_CLOSE.value
    assert controller.vend.vend_count == 0

    # THE CONTROL: the car behind backs off and the same completion is accepted.
    deactivate.set_occupied(False)
    status, _ = complete("KEY-002")
    assert status == 202
    thread = service.assisted.settling
    if thread is not None:
        thread.join(timeout=5)
    assert controller.vend.vend_count == 1


def test_a_lane_with_no_deactivate_loop_is_never_refused_vehicle_too_close():
    from lane_controller.vend import parse

    controller, loop, _ = a_lane(
        deactivate_loops=0, identities=UNREADABLE, crossings=[(ClosingSequence.FORWARD, 0.0)]
    )
    loop.arrive()
    controller.run_once()
    service = LaneService(controller)
    status, _ = service.assisted.complete(
        parse(
            {
                "authorised_by": "human_open_now",
                "identity": {"kind": "ticket", "ticket_ref": "TICKET-000001"},
                "decision_at": controller.last_decision_at,
            },
            "KEY-001",
        )
    )
    assert status == 202
    thread = service.assisted.settling
    if thread is not None:
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# D4 -- the stuck loop is measured
# ---------------------------------------------------------------------------


def test_a_deactivate_loop_stuck_occupied_reads_active_past_the_same_bound():
    now = [0.0]
    controller, _, deactivate = a_lane(clock=lambda: now[0], arming_loop_max_occupied_s=30.0)
    service = LaneService(controller)
    code = MalfunctionCode.DEACTIVATE_LOOP_STUCK_OCCUPIED

    assert service.derived_states()[code] is HealthState.UNKNOWN
    deactivate.set_occupied(True)
    assert service.derived_states()[code] is HealthState.UNKNOWN  # first observation
    now[0] = 31.0
    assert service.derived_states()[code] is HealthState.ACTIVE
    # And it defends the vend route through VEND_BLOCKING, named as itself.
    assert code in VEND_BLOCKING

    deactivate.set_occupied(False)  # clear: never `ok`, the run resets
    assert service.derived_states()[code] is HealthState.UNKNOWN
    deactivate.set_occupied(True)
    now[0] = 40.0
    assert service.derived_states()[code] is HealthState.UNKNOWN  # a NEW run, 0 s old


def test_a_lane_with_no_deactivate_loop_answers_unknown_for_its_stuck_code():
    controller, _, _ = a_lane(deactivate_loops=0)
    state = LaneService(controller).derived_states()[MalfunctionCode.DEACTIVATE_LOOP_STUCK_OCCUPIED]
    assert state is HealthState.UNKNOWN


# ---------------------------------------------------------------------------
# D5 -- declared, bounded, and wired to match
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("count", [2, -1, 3])
def test_deactivate_loops_is_zero_or_one(count):
    with pytest.raises(ValueError, match="deactivate_loops must be 0 or 1"):
        LoopConfig(deactivate_loops=count, deactivate_spacing_m=6.0)


@pytest.mark.parametrize("spacing", [0.0, 5.99, -6.0, float("nan"), float("inf")])
def test_a_deactivate_loop_closer_than_the_longest_vehicle_is_refused(spacing):
    """Under the tail of the very car whose nose is on the arming loop, it holds
    that car for ever. Refused at the bound, naming it."""
    with pytest.raises(ValueError, match=f"at least {DEACTIVATE_SPACING_MIN_M}"):
        LoopConfig(deactivate_loops=1, deactivate_spacing_m=spacing)


def test_the_bound_itself_is_accepted():
    loops = LoopConfig(deactivate_loops=1, deactivate_spacing_m=DEACTIVATE_SPACING_MIN_M)
    assert loops.has_deactivate_loop


def test_no_loop_means_no_spacing():
    with pytest.raises(ValueError, match="must be 0.0 when deactivate_loops is 0"):
        LoopConfig(deactivate_loops=0, deactivate_spacing_m=6.0)


def test_declared_and_wired_must_agree():
    base = dict(
        lane_id="lane-1",
        site_id="site-1",
        direction="entry",
        camera=CameraConfig(camera_id="c", rtsp_url=""),
        gate=GateConfig(),
    )
    seams = dict(
        loop=EdgeLoop(),
        camera=CannedCameraFeed(),
        vend=RecordingVendOutput(),
        identifier=StubVehicleIdentifier(READABLE),
    )
    declared = LaneConfig(loops=LoopConfig(deactivate_loops=1, deactivate_spacing_m=6.0), **base)
    with pytest.raises(ValueError, match="deactivate_loops = 1 but deactivate_loop was not"):
        LaneController(declared, **seams)
    undeclared = LaneConfig(loops=LoopConfig(), **base)
    with pytest.raises(ValueError, match="deactivate_loops = 0 but deactivate_loop was supplied"):
        LaneController(undeclared, deactivate_loop=OccupancyLoopInput(), **seams)


def test_a_lane_without_the_loop_publishes_zero_and_is_never_held():
    """THE PROPERTY THE ROUND IS HELD TO: a lane whose config declares no
    deactivate loop makes no different decision. It is never held, records
    neither kind, and says `deactivate_loops: 0` on every vehicle rather than
    saying nothing."""
    controller, loop, _ = a_lane(deactivate_loops=0)
    loop.arrive()
    assert controller.run_once() is not None
    recorded = kinds(controller)
    assert ARMING_SUPPRESSED not in recorded
    assert ARMING_SUPPRESSION_ENDED not in recorded
    (armed,) = details(controller, ARMED)
    assert armed["geometry_assumed"]["deactivate_loops"] == 0
    assert armed["geometry_assumed"]["deactivate_spacing_m"] == 0.0
