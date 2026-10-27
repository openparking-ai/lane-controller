"""One closing loop: the barrier's own, read through its detector's second output.

The standard installation, and the one the installer drawings show. The loop
stays wired to the barrier, which closes on it by itself; the lane reads the
detector's second output on an input of its control panel and drives nothing.

What the code does, and it is the whole of what is claimed here:

  * a vend, then the closing loop occupied inside the confirmation window ->
    `confirmed`, at an entry and at an exit, through the same `_confirms` both
    ends and both reads share;
  * a vend, then nothing in the window -> `held`, exactly as a two-loop lane;
  * the closing loop occupied with no vend behind it -> an entry nothing
    admitted, never a confirmed one -- and a stay all the same (2026-10-26),
    `no_open_command: true`, its plate unreadable when nothing read it;
  * the published geometry says `closing_loops: 1` and no spacing;
  * a lane built from `config/lane.example.toml` -- the drawing's lane -- counts
    in the platform's `inside_count`.

What one loop does NOT tell: a car that reaches it and then backs out. That is
accepted; the stay then has no exit, as any open stay, and nothing here claims
otherwise.

Every test below is paired with a break in `scripts/closing_loop_fail_control.py`
that must turn it red.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fake_platform import FakePlatform
from lane_controller import (
    CameraConfig,
    DecisionCache,
    EventQueue,
    GateConfig,
    LaneConfig,
    LaneController,
    LoopConfig,
    Outcome,
    PlatformTransport,
    Rule,
    VehicleIdentity,
    sync_rules,
)
from lane_controller.contract import TransitState
from lane_controller.interfaces import ClosingSequence
from lane_controller.simulated import (
    CannedCameraFeed,
    OccupancyLoopInput,
    RecordingVendOutput,
    ScriptedClosingLoops,
    SimulatedLoopInput,
    StubVehicleIdentifier,
)
from lane_controller.sync import (
    CONFIRMED,
    ENTRY_CONFIRMED,
    ENTRY_HELD,
    ENTRY_PENDING,
    ENTRY_UNADMITTED,
    ENTRY_UNCONFIRMABLE,
    EXIT_CONFIRMED,
    EXIT_HELD,
    HELD,
    REASON_CLOSING_LOOP_OCCUPIED,
    REASON_FORWARD,
    REASON_NO_PENDING_ENTRY,
    SESSION_CLOSE,
    SESSION_OPEN,
)

WINDOW = 10.0
EXAMPLE = Path(__file__).parent.parent / "config" / "lane.example.toml"

OCC = ClosingSequence.OCCUPIED
ALLOWED = VehicleIdentity(plate="SIM-0001", confidence=0.97, presence=True)
BANNED = VehicleIdentity(plate="BANNED-1", confidence=0.97, presence=True)


#: What the idle poll's pictures read (U7f): nothing -- the tailgater nobody
#: identified. tests/test_tailgater.py has the one that is read.
UNREAD = VehicleIdentity(plate=None, confidence=0.0)


def build(*, direction="entry", crossings=(), identities=None, closing_loops=1):
    """A lane with the barrier's closing loop read, and nothing else implied."""
    config = LaneConfig(
        lane_id="lane-test",
        site_id="site-test",
        camera=CameraConfig(camera_id="sim-cam-1", rtsp_url="", frames_per_read=1),
        gate=GateConfig(),
        direction=direction,
        confidence_threshold=0.85,
        loops=LoopConfig(
            arming_loops=1,
            closing_loops=closing_loops,
            closing_spacing_m=0.0 if closing_loops == 1 else 1.5,
            confirmation_window_seconds=WINDOW,
        ),
    )
    cache = DecisionCache()
    cache.load([Rule(plate="BANNED-1", allow=False)], default_action="allow")
    vend = RecordingVendOutput()
    loops = ScriptedClosingLoops(list(crossings))
    controller = LaneController(
        config,
        loop=SimulatedLoopInput(arrivals=1),
        camera=CannedCameraFeed(),
        vend=vend,
        identifier=StubVehicleIdentifier([*(identities or [ALLOWED]), UNREAD]),
        closing_loops=loops,
        cache=cache,
    )
    return controller, vend, loops


def kinds(controller) -> list[str]:
    return [event.kind for event in list(controller.events._queue)]


def detail(controller, kind: str) -> dict:
    return next(e for e in list(controller.events._queue) if e.kind == kind).detail


# ---------------------------------------------------------------------------
# Check 1 -- a vend, then the closing loop.
# ---------------------------------------------------------------------------


def test_a_vend_then_the_closing_loop_occupied_confirms_the_entry():
    controller, vend, loops = build(crossings=[(OCC, 3.0)])

    controller.run_once()

    assert vend.vend_count == 1
    order = kinds(controller)
    assert order.index(ENTRY_PENDING) < order.index(ENTRY_CONFIRMED) < order.index(SESSION_OPEN)
    assert detail(controller, ENTRY_CONFIRMED)["reason"] == REASON_CLOSING_LOOP_OCCUPIED
    assert detail(controller, SESSION_OPEN)["entry_confirmation"] == CONFIRMED
    assert ENTRY_UNCONFIRMABLE not in order
    assert controller.transit_state == TransitState.CONFIRMED.value
    assert loops.unread == 0
    assert loops.windows_seen == [WINDOW], "the configured window is the one applied"


def test_a_vend_then_the_closing_loop_occupied_confirms_the_exit():
    controller, vend, _ = build(direction="exit", crossings=[(OCC, 3.0)])

    controller.run_once()

    assert vend.vend_count == 1
    assert EXIT_CONFIRMED in kinds(controller)
    assert detail(controller, EXIT_CONFIRMED)["reason"] == REASON_CLOSING_LOOP_OCCUPIED
    assert detail(controller, SESSION_CLOSE)["exit_confirmation"] == CONFIRMED


def test_a_vend_then_nothing_holds_the_entry_and_opens_no_session():
    controller, vend, _ = build(crossings=[])

    controller.run_once()

    assert vend.vend_count == 1
    assert ENTRY_HELD in kinds(controller)
    assert ENTRY_CONFIRMED not in kinds(controller)
    assert SESSION_OPEN not in kinds(controller)
    assert controller.transit_state == TransitState.HELD.value


def test_a_vend_then_nothing_at_an_exit_still_closes_held():
    """The exit's one asymmetry is unchanged by the geometry: the car is gone."""
    controller, _, _ = build(direction="exit", crossings=[])

    controller.run_once()

    assert EXIT_HELD in kinds(controller)
    assert detail(controller, SESSION_CLOSE)["exit_confirmation"] == HELD


def test_an_occupancy_slower_than_the_window_is_held():
    controller, _, _ = build(crossings=[(OCC, WINDOW + 5.0)])

    controller.run_once()

    assert ENTRY_HELD in kinds(controller)
    assert SESSION_OPEN not in kinds(controller)


def test_the_closing_loop_occupied_with_no_vend_is_an_entry_nothing_admitted():
    """The lane refused the car, the car drove in anyway and reached the
    closing loop. An entry -- a vehicle is inside -- and never a confirmed one.
    And a stay: the loops and the pictures make the entry, whatever opened the
    barrier, so the car that drove in is in the money record, flagged as a car
    no open command let in."""
    controller, vend, loops = build(identities=[BANNED], crossings=[(OCC, 3.0)])

    assert controller.run_once().outcome is Outcome.DENY
    assert vend.vend_count == 0
    assert loops.unread == 1

    assert controller.run_once() is None, "an idle turn"

    assert loops.unread == 0
    assert kinds(controller).count(ENTRY_UNADMITTED) == 1
    assert ENTRY_CONFIRMED not in kinds(controller), "confirmed with no vend behind it"
    opened = detail(controller, SESSION_OPEN)
    assert (opened["no_open_command"], opened["identity_kind"], opened["plate_unreadable"]) == (
        True, "unread", True)
    assert "tailgater" not in opened, "a car nobody could read is never a tailgater"
    recorded = detail(controller, ENTRY_UNADMITTED)
    assert recorded["reason"] == REASON_NO_PENDING_ENTRY
    assert recorded["geometry_assumed"]["closing_loops"] == 1


def test_an_idle_occupancy_at_an_exit_is_not_an_entry():
    """As two loops: a car that left with nothing pending is logged, not an entry."""
    controller, vend, loops = build(direction="exit", identities=[BANNED], crossings=[(OCC, 3.0)])
    controller.cache.load([Rule(plate="BANNED-1", allow=False)], default_action="deny")

    controller.run_once()
    assert vend.vend_count == 0
    controller.run_once()

    assert loops.unread == 0
    assert ENTRY_UNADMITTED not in kinds(controller)
    assert EXIT_CONFIRMED not in kinds(controller)


# ---------------------------------------------------------------------------
# Each geometry reports only what it can see.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("answer", [ClosingSequence.FORWARD, ClosingSequence.REVERSE])
def test_a_one_loop_lane_does_not_use_a_direction_it_cannot_see(answer):
    controller, _, _ = build(crossings=[(answer, 3.0)])

    controller.run_once()

    assert ENTRY_CONFIRMED not in kinds(controller)
    assert ENTRY_HELD in kinds(controller)
    assert SESSION_OPEN not in kinds(controller)


def test_a_two_loop_lane_is_never_confirmed_on_one_occupancy():
    """Two loops behave exactly as before: FORWARD and nothing else confirms."""
    controller, _, _ = build(closing_loops=2, crossings=[(OCC, 3.0)])

    controller.run_once()

    assert ENTRY_CONFIRMED not in kinds(controller)
    assert ENTRY_HELD in kinds(controller)

    forward, _, _ = build(closing_loops=2, crossings=[(ClosingSequence.FORWARD, 3.0)])
    forward.run_once()
    assert detail(forward, ENTRY_CONFIRMED)["reason"] == REASON_FORWARD


# ---------------------------------------------------------------------------
# Check 3 -- the record, and the drawing's lane counted inside.
# ---------------------------------------------------------------------------


def test_the_published_geometry_says_one_loop_and_no_spacing():
    controller, _, _ = build(crossings=[(OCC, 3.0)])
    controller.run_once()

    geometry = detail(controller, ENTRY_CONFIRMED)["geometry_assumed"]
    assert geometry["closing_loops"] == 1
    assert geometry["closing_spacing_m"] == 0.0
    assert controller.config.loops.confirms_entry is True


def test_one_loop_with_a_spacing_is_refused():
    with pytest.raises(ValueError, match="no second loop"):
        LoopConfig(closing_loops=1, closing_spacing_m=1.5)


def inside_count(platform: FakePlatform) -> int:
    """The platform's rule, `platform/src/app.js` GET .../sessions/open:
    `inside_count` counts open sessions whose `entry_confirmation` is
    `confirmed`, and nothing else."""
    return sum(1 for s in platform.open_sessions.values() if s["entry_confirmation"] == CONFIRMED)


def test_a_lane_built_to_the_drawing_counts_inside():
    """The example file is the drawing's lane. A car admitted there, reaching
    the closing loop, is a car the garage counts as inside."""
    config = LaneConfig.from_file(EXAMPLE)
    platform = FakePlatform()
    cache = DecisionCache()
    sync_rules(platform, cache)
    controller = LaneController(
        config,
        loop=SimulatedLoopInput(arrivals=1),
        camera=CannedCameraFeed(),
        vend=RecordingVendOutput(),
        identifier=StubVehicleIdentifier([VehicleIdentity(plate="DRAWN-1", confidence=0.97)]),
        arming_loop_b=OccupancyLoopInput(True) if config.loops.arming_loops == 2 else None,
        deactivate_loop=OccupancyLoopInput(False) if config.loops.has_deactivate_loop else None,
        closing_loops=ScriptedClosingLoops([(OCC, 3.0)]) if config.loops.confirms_entry else None,
        cache=cache,
        events=EventQueue(PlatformTransport(platform)),
    )

    controller.run_once()
    controller.deliver()

    assert len(platform.open_sessions) == 1
    assert inside_count(platform) == 1, "the car the drawing's lane admitted is not counted inside"
