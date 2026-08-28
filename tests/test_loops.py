"""The loops: two before the barrier to arm, two after it to confirm.

What the code does, and it is the whole of what is claimed here: at a lane with
two closing loops, a forward crossing inside the confirmation window promotes
the pending entry the vend created. `decide()` is untouched and still treats
`presence=None` as "nobody measured it" rather than as a refusal, because a lane
with no reference view must not refuse every customer.

Every test below is paired with a break in `scripts/confirmation_fail_control.py`
that must turn it red. A guarantee nobody has watched fail is not known to be
one, and one that can quietly stop running is not one either.
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
    Outcome,
    VehicleIdentity,
)
from lane_controller.interfaces import ClosingLoops, ClosingSequence
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
    HELD,
    SESSION_CLOSE,
    SESSION_OPEN,
    UNCONFIRMABLE,
)

WINDOW = 10.0


def build(
    *,
    direction: str = "entry",
    arming_loops: int = 1,
    closing_loops: int = 2,
    crossings=(),
    identities=None,
    second_loop_occupied: bool = True,
    window: float = WINDOW,
    default_action: str = "allow",
    loops_impl: ClosingLoops | None = None,
    clock=None,
):
    """A whole lane with the geometry named, and nothing about it implied.

    Both counts are passed explicitly at every call site rather than defaulted
    in the fixture, because they are the axes every assertion below branches on
    -- a fixture that pinned one of them would read as coverage while measuring
    a single point.
    """
    config = LaneConfig(
        lane_id="lane-test",
        site_id="site-test",
        camera=CameraConfig(camera_id="sim-cam-1", rtsp_url="", frames_per_read=3),
        gate=GateConfig(),
        direction=direction,
        confidence_threshold=0.85,
        loops=LoopConfig(
            arming_loops=arming_loops,
            closing_loops=closing_loops,
            confirmation_window_seconds=window,
        ),
    )
    cache = DecisionCache()
    cache.load([], default_action=default_action)
    vend = RecordingVendOutput()
    if loops_impl is not None:
        loops = loops_impl
    else:
        loops = ScriptedClosingLoops(crossings) if closing_loops == 2 else None
    extra = {"clock": clock} if clock is not None else {}
    controller = LaneController(
        config,
        loop=SimulatedLoopInput(arrivals=1),
        camera=CannedCameraFeed(),
        vend=vend,
        identifier=StubVehicleIdentifier(
            identities or [VehicleIdentity(plate="SIM-0001", confidence=0.97, presence=True)]
        ),
        arming_loop_b=(
            OccupancyLoopInput(second_loop_occupied) if arming_loops == 2 else None
        ),
        closing_loops=loops,
        cache=cache,
        **extra,
    )
    return controller, vend, loops


def kinds(controller) -> list[str]:
    return [event.kind for event in list(controller.events._queue)]


def detail(controller, kind: str) -> dict:
    return next(e for e in list(controller.events._queue) if e.kind == kind).detail


# ---------------------------------------------------------------------------
# S1 — arming. Both loops, or nothing, and the nothing is recorded.
# ---------------------------------------------------------------------------


def test_two_arming_loops_both_occupied_arms_the_lane():
    controller, vend, _ = build(
        arming_loops=2,
        second_loop_occupied=True,
        crossings=[(ClosingSequence.FORWARD, 3.0)],
    )

    decision = controller.run_once()

    assert decision is not None and decision.outcome is Outcome.ALLOW
    assert vend.vend_count == 1
    assert "armed" in kinds(controller)
    assert "arming_incomplete" not in kinds(controller)


def test_one_arming_loop_alone_arms_nothing_and_is_recorded():
    """The pattern the second loop exists to expose: a person standing on one
    loop with a piece of metal. An object has to SPAN the gap to arm."""
    controller, vend, _ = build(arming_loops=2, second_loop_occupied=False)

    decision = controller.run_once()

    assert decision is None, "one arming loop must not arm the lane"
    assert vend.vend_count == 0, "the barrier was told to open on one loop"
    assert kinds(controller) == ["arming_incomplete"]
    assert "frames_captured" not in kinds(controller)
    assert detail(controller, "arming_incomplete")["reason"] == "only_one_arming_loop_occupied"


def test_a_site_with_one_arming_loop_still_runs_and_says_so():
    """A site that has not installed the second loop is NOT refused. What it
    does not get is the single-loop-fraud protection, and every record it
    writes says which lane it is."""
    controller, vend, _ = build(
        arming_loops=1, crossings=[(ClosingSequence.FORWARD, 3.0)]
    )

    assert controller.run_once().outcome is Outcome.ALLOW
    assert vend.vend_count == 1
    assert detail(controller, "armed")["geometry_assumed"]["arming_loops"] == 1


def test_the_geometry_travels_with_the_record_as_an_assumption():
    """Every spacing and window is a per-site SETTING, published and never
    measured here. The key says `assumed` so nobody reads it as otherwise."""
    controller, _, _ = build(
        arming_loops=2, crossings=[(ClosingSequence.FORWARD, 3.0)], window=7.5
    )
    controller.run_once()

    geometry = detail(controller, "entry_confirmed")["geometry_assumed"]
    assert geometry == {
        "arming_loops": 2,
        "arming_spacing_m": 1.5,
        "closing_loops": 2,
        "closing_spacing_m": 1.5,
        "confirmation_window_seconds": 7.5,
    }


def test_declared_geometry_and_wired_hardware_must_agree():
    """Two arming loops in the config and one on the lane would make a
    single-loop site look like a two-loop site. It is refused at construction."""
    config = LaneConfig(
        lane_id="lane-test",
        site_id="site-test",
        camera=CameraConfig(camera_id="c", rtsp_url=""),
        gate=GateConfig(),
        loops=LoopConfig(arming_loops=2, closing_loops=2),
    )
    common = dict(
        loop=SimulatedLoopInput(),
        camera=CannedCameraFeed(),
        vend=RecordingVendOutput(),
        identifier=StubVehicleIdentifier(),
    )
    with pytest.raises(ValueError, match="arming_loop_b"):
        LaneController(config, closing_loops=ScriptedClosingLoops(), **common)
    with pytest.raises(ValueError, match="closing_loops"):
        LaneController(config, arming_loop_b=OccupancyLoopInput(), **common)


def test_one_closing_loop_is_refused_because_it_confirms_nothing():
    with pytest.raises(ValueError, match="cannot separate"):
        LoopConfig(closing_loops=1)


# ---------------------------------------------------------------------------
# S2 — the closing loops. Three answers, three names, none folded into another.
# ---------------------------------------------------------------------------


def test_a_forward_crossing_confirms_the_entry_and_opens_the_session():
    controller, vend, loops = build(crossings=[(ClosingSequence.FORWARD, 3.0)])

    controller.run_once()

    assert vend.vend_count == 1
    assert kinds(controller) == [
        "armed",
        "frames_captured",
        "vehicle_identified",
        "decision",
        "vended",
        "entry_pending",
        "entry_confirmed",
        SESSION_OPEN,
    ]
    assert detail(controller, "entry_confirmed")["reason"] == "closing_sequence_forward"
    assert detail(controller, SESSION_OPEN)["entry_confirmation"] == CONFIRMED
    assert loops.windows_seen == [WINDOW], "the CONFIGURED window must be the one applied"


def test_a_reverse_crossing_is_a_vehicle_backing_out_and_opens_nothing():
    controller, vend, _ = build(crossings=[(ClosingSequence.REVERSE, 3.0)])

    controller.run_once()

    assert vend.vend_count == 1, "the barrier did open; that is not what is being undone"
    assert "entry_pending" in kinds(controller)
    assert "entry_backed_out" in kinds(controller)
    assert SESSION_OPEN not in kinds(controller), "a car that backed out opened a session"
    assert "entry_confirmed" not in kinds(controller)
    assert "entry_held" not in kinds(controller)
    assert detail(controller, "entry_backed_out")["reason"] == "closing_sequence_reverse"


def test_the_window_elapsing_holds_the_entry_and_never_voids_or_promotes_it():
    """The third answer, and it is neither of the other two. Voiding it
    silently re-creates the abandoned-ticket fraud; promoting it is the phantom
    occupant. It is held, flagged, and left for the intercom."""
    controller, vend, _ = build(crossings=[])

    controller.run_once()

    assert vend.vend_count == 1
    assert "entry_pending" in kinds(controller), "the pending entry must not vanish"
    assert "entry_held" in kinds(controller)
    assert SESSION_OPEN not in kinds(controller)
    assert "entry_confirmed" not in kinds(controller)
    assert "entry_backed_out" not in kinds(controller)
    assert detail(controller, "entry_held")["reason"] == "confirmation_window_elapsed"


def test_a_crossing_slower_than_the_window_does_not_confirm():
    """The window is what makes the sequence mean "a vehicle went through at a
    plausible speed" rather than "something happened here eventually"."""
    controller, _, _ = build(window=10.0, crossings=[(ClosingSequence.FORWARD, 30.0)])

    controller.run_once()

    assert "entry_held" in kinds(controller)
    assert SESSION_OPEN not in kinds(controller)


class _FakeClock:
    """A clock a test moves by hand, so a slow crossing costs no wall time."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class LateForwardLoops(ClosingLoops):
    """Loops that report FORWARD after the window has already gone by.

    A real one: a loop board whose own clock drifted, or one that reports the
    crossing it eventually saw rather than the one that happened in time.
    `interfaces.ClosingLoops` ASKS an implementation not to do this, and an
    obligation on the other side of a seam is a comment, not a check -- so the
    thing measured here is that the controller applies the window itself.
    """

    def __init__(self, clock: _FakeClock, took_seconds: float) -> None:
        self._clock = clock
        self._took = took_seconds
        self.windows_seen: list[float] = []

    def wait_for_sequence(self, window_seconds: float) -> ClosingSequence:
        self.windows_seen.append(window_seconds)
        self._clock.advance(self._took)
        return ClosingSequence.FORWARD


def test_a_forward_reported_after_the_window_is_held_not_confirmed():
    """The window is applied by the CONTROLLER, not trusted to the loops.

    The event stamps `confirmation_window_seconds` under `geometry_assumed` on
    every vehicle. It may only do that because the comparison is made here: a
    published window that nothing applies is a number the record asserts and the
    lane never used.
    """
    clock = _FakeClock()
    controller, vend, loops = build(
        window=10.0, loops_impl=LateForwardLoops(clock, took_seconds=30.0), clock=clock
    )

    controller.run_once()

    assert vend.vend_count == 1, "the barrier did open; that is not what is being undone"
    assert loops.windows_seen == [10.0], "the configured window must reach the loops"
    assert SESSION_OPEN not in kinds(controller), "a crossing reported late opened a session"
    assert "entry_confirmed" not in kinds(controller)
    assert "entry_held" in kinds(controller)
    assert detail(controller, "entry_held")["reason"] == "confirmation_window_elapsed"


def test_a_forward_reported_inside_the_window_still_confirms():
    """The other side of the same comparison, on the same implementation. Without
    it the test above is satisfied by a lane that confirms nothing at all."""
    clock = _FakeClock()
    controller, _, _ = build(
        window=10.0, loops_impl=LateForwardLoops(clock, took_seconds=3.0), clock=clock
    )

    controller.run_once()

    assert "entry_confirmed" in kinds(controller)
    assert detail(controller, SESSION_OPEN)["entry_confirmation"] == CONFIRMED


def test_a_lane_with_no_closing_loops_opens_sessions_and_marks_every_one():
    """The backwards path. A site that has not installed the loops is not
    refused -- but `unconfirmable` is not the word `confirmed`, and it is on
    every single session that lane opens."""
    controller, vend, _ = build(closing_loops=0)

    controller.run_once()

    assert vend.vend_count == 1
    assert kinds(controller) == [
        "armed",
        "frames_captured",
        "vehicle_identified",
        "decision",
        "vended",
        "entry_pending",
        "entry_unconfirmable",
        SESSION_OPEN,
    ]
    assert detail(controller, "entry_unconfirmable")["reason"] == "no_closing_loops_configured"
    assert detail(controller, SESSION_OPEN)["entry_confirmation"] == UNCONFIRMABLE


def test_the_pending_entry_and_its_outcome_replay_in_the_order_they_happened():
    """They ride the SAME queue as everything else, so an offline lane replays
    what happened in the order it happened."""
    controller, _, _ = build(crossings=[(ClosingSequence.FORWARD, 3.0)])
    controller.run_once()

    order = kinds(controller)
    assert order.index("vended") < order.index("entry_pending")
    assert order.index("entry_pending") < order.index("entry_confirmed")
    assert order.index("entry_confirmed") < order.index(SESSION_OPEN)


# --- exit lanes: A→B means the car LEFT the garage -------------------------


def test_an_exit_confirmed_by_the_loops_closes_the_session():
    controller, vend, _ = build(direction="exit", crossings=[(ClosingSequence.FORWARD, 3.0)])

    controller.run_once()

    assert vend.vend_count == 1
    assert "exit_pending" in kinds(controller)
    assert "exit_confirmed" in kinds(controller)
    assert detail(controller, SESSION_CLOSE)["exit_confirmation"] == CONFIRMED


def test_an_exit_that_reverses_back_inside_closes_nothing():
    """B→A at an exit is a vehicle that reversed back INTO the garage. It is
    not an entry backing out, so it does not borrow that name."""
    controller, _, _ = build(direction="exit", crossings=[(ClosingSequence.REVERSE, 3.0)])

    controller.run_once()

    assert "exit_backed_in" in kinds(controller)
    assert SESSION_CLOSE not in kinds(controller)
    assert "entry_backed_out" not in kinds(controller)


def test_an_unconfirmed_exit_still_closes_and_bills_and_is_flagged():
    """The one asymmetry between the two directions, and it is a decision.

    At an exit the vend IS the payment moment and the barrier opened -- the car
    is gone whatever the loops saw. Holding the session open would leave the
    stay unbilled and the vehicle inside for ever, so a site that installed the
    loops would be worse off than one that did not. It closes, it bills, and it
    carries `held` with the `exit_held` event beside it: a flag for a human, not
    a hole in the ledger. An ENTRY is the opposite and opens nothing."""
    controller, vend, _ = build(direction="exit", crossings=[])

    controller.run_once()

    assert vend.vend_count == 1
    assert "exit_held" in kinds(controller)
    assert SESSION_CLOSE in kinds(controller), "an exit that vended left the session open"
    assert detail(controller, SESSION_CLOSE)["exit_confirmation"] == HELD
    assert detail(controller, "exit_held")["reason"] == "confirmation_window_elapsed"


def test_a_held_ENTRY_still_opens_nothing():
    """The control for the asymmetry above: the same missing crossing, the other
    direction. Nothing confirmed the entry, so there is no session to bill."""
    controller, _, _ = build(direction="entry", crossings=[])

    controller.run_once()

    assert "entry_held" in kinds(controller)
    assert SESSION_OPEN not in kinds(controller)


# ---------------------------------------------------------------------------
# S3 — the null-vend property, written from the attacker's side.
# ---------------------------------------------------------------------------

NOBODY_MEASURED_IT = VehicleIdentity(
    # Exactly what a lane with no presence reference emits, which is every lane
    # in every deployment: a confident plate and a presence nobody measured.
    # It is also exactly what a confident read of pure noise emits -- the lane
    # sees only the tuple, and what produced the text is invisible to it.
    plate="7ABC123",
    confidence=0.999,
    presence=None,
)


def test_an_unmeasured_presence_with_no_car_behind_it_causes_no_transaction():
    """THE GUARANTEE. `presence=None`, a confident plate, the garage default is
    allow, the barrier opens -- and no closing loop ever sees a vehicle, because
    there is no vehicle. No session, no occupancy, one held entry.

    Note what is NOT asserted: that the lane refused, or that `decide()`
    treated `None` as an absence. It did neither, deliberately. The transaction
    does not happen because nothing confirmed it."""
    controller, vend, _ = build(crossings=[], identities=[NOBODY_MEASURED_IT])

    decision = controller.run_once()

    assert decision.outcome is Outcome.ALLOW, "decide() is unchanged; the vend still fires"
    assert vend.vend_count == 1
    assert SESSION_OPEN not in kinds(controller), "an unmeasured presence opened a session"
    assert kinds(controller).count("entry_held") == 1
    assert detail(controller, "entry_held")["reason"] == "confirmation_window_elapsed"


def test_the_same_unmeasured_presence_with_a_real_car_opens_exactly_one_session():
    """The sibling, and it is what stops the guarantee above being satisfied by
    a lane that transacts for nobody. Same identity, same settings, same
    `presence=None` -- the only difference is that a vehicle crossed the two
    loops in order."""
    controller, vend, _ = build(
        crossings=[(ClosingSequence.FORWARD, 3.0)], identities=[NOBODY_MEASURED_IT]
    )

    assert controller.run_once().outcome is Outcome.ALLOW
    assert vend.vend_count == 1
    assert kinds(controller).count(SESSION_OPEN) == 1
    assert detail(controller, SESSION_OPEN)["entry_confirmation"] == CONFIRMED
