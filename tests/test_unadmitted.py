"""A crossing with nothing pending: the entry nothing admitted.

§3h gave a pending entry three answers -- promoted, backed out, held -- and
every one of them is what becomes of a VEND. The loops after the gate were
read only after one, so a car the lane refused that drove in anyway, a car
following the one admitted, and every car through a barrier that is simply up
crossed loops nobody was reading and were in no record at all. This suite is
the record of that case, and `scripts/unadmitted_fail_control.py` is what
proves each guarantee here can go red.

The three answers are NOT re-tested here. `scripts/transit_matrix.py` holds
them still: one frozen matrix, run on `main` and on the branch, and every row
that moved is one this change names. `tests/test_transit_matrix.py` pins it.
"""

from __future__ import annotations

import logging
import threading

from lane_controller import (
    CameraConfig,
    DecisionCache,
    GateConfig,
    LaneConfig,
    LaneController,
    LoopConfig,
    Outcome,
    Rule,
    TransitState,
    VehicleIdentity,
)
from lane_controller.events import SESSION_KINDS, EventQueue
from lane_controller.interfaces import ClosingLoops, ClosingSequence
from lane_controller.service import LaneService
from lane_controller.simulated import (
    CannedCameraFeed,
    RecordingVendOutput,
    ScriptedClosingLoops,
    SimulatedLoopInput,
    StubVehicleIdentifier,
)
from lane_controller.sync import (
    ENTRY_CONFIRMED,
    ENTRY_UNADMITTED,
    REASON_FORWARD,
    REASON_NO_PENDING_ENTRY,
    SESSION_OPEN,
    to_iso,
)
from lane_controller.vend import _Once

WINDOW = 10.0

ALLOWED = VehicleIdentity(plate="SIM-0001", confidence=0.97, presence=True)
BANNED = VehicleIdentity(plate="BANNED-1", confidence=0.97, presence=True)

F = ClosingSequence.FORWARD
R = ClosingSequence.REVERSE


class RecordingTransport:
    """What left the box, batch by batch."""

    def __init__(self) -> None:
        self.batches: list[list] = []

    def send(self, events) -> bool:
        self.batches.append(list(events))
        return True


def build(
    *,
    direction: str = "entry",
    closing_loops: int = 2,
    crossings=(),
    identities=None,
    arrivals: int = 1,
    loops_impl: ClosingLoops | None = None,
    transport=None,
):
    config = LaneConfig(
        lane_id="lane-test",
        site_id="site-test",
        camera=CameraConfig(camera_id="sim-cam-1", rtsp_url="", frames_per_read=1),
        gate=GateConfig(),
        direction=direction,
        confidence_threshold=0.85,
        loops=LoopConfig(
            arming_loops=1, closing_loops=closing_loops, confirmation_window_seconds=WINDOW
        ),
    )
    cache = DecisionCache()
    cache.load([Rule(plate="BANNED-1", allow=False)], default_action="allow")
    vend = RecordingVendOutput()
    if loops_impl is not None:
        loops = loops_impl
    else:
        loops = ScriptedClosingLoops(list(crossings)) if closing_loops == 2 else None
    controller = LaneController(
        config,
        loop=SimulatedLoopInput(arrivals=arrivals),
        camera=CannedCameraFeed(),
        vend=vend,
        identifier=StubVehicleIdentifier(identities or [ALLOWED]),
        closing_loops=loops,
        cache=cache,
        events=EventQueue(transport) if transport is not None else None,
    )
    return controller, vend, loops


def kinds(controller) -> list[str]:
    return [event.kind for event in list(controller.events._queue)]


def detail(controller, kind: str) -> dict:
    return next(e for e in list(controller.events._queue) if e.kind == kind).detail


# ---------------------------------------------------------------------------
# The case, and its positive control in the same file.
# ---------------------------------------------------------------------------


def test_a_crossing_with_nothing_pending_is_recorded_as_an_entry_nothing_admitted():
    """The lane refused the car. The car drove in anyway. Until this existed
    that car was in no record; now it is an entry with its own name."""
    controller, vend, loops = build(identities=[BANNED], crossings=[(F, 3.0)])

    decision = controller.run_once()
    assert decision.outcome is Outcome.DENY
    assert vend.vend_count == 0, "a refused car must not vend; that is not what is under test"
    assert loops.unread == 1, "the crossing is on the loops and nothing has read it"
    assert ENTRY_UNADMITTED not in kinds(controller)

    assert controller.run_once() is None, "an idle turn: no arrival"

    assert loops.unread == 0, "the idle turn read the loops"
    assert kinds(controller)[-1] == ENTRY_UNADMITTED
    assert kinds(controller).count(ENTRY_UNADMITTED) == 1
    recorded = detail(controller, ENTRY_UNADMITTED)
    assert recorded["reason"] == REASON_NO_PENDING_ENTRY
    assert recorded["last_decision"] == Outcome.DENY.value
    assert recorded["last_transit"] == TransitState.NONE.value
    assert recorded["last_decision_at"] == controller.last_decision_at
    assert recorded["geometry_assumed"]["closing_loops"] == 2
    assert "plate" not in recorded and "ticket_ref" not in recorded


def test_the_positive_control_a_vend_then_a_crossing_is_promoted_and_not_unadmitted():
    """The ordinary case, in the same file and the same run as the new one: a
    vend, a forward crossing, a session. Nothing about it says `unadmitted`,
    and its reason is its own."""
    controller, vend, loops = build(crossings=[(F, 3.0)])

    controller.run_once()

    assert vend.vend_count == 1
    assert ENTRY_CONFIRMED in kinds(controller)
    assert SESSION_OPEN in kinds(controller)
    assert ENTRY_UNADMITTED not in kinds(controller)
    assert detail(controller, ENTRY_CONFIRMED)["reason"] == REASON_FORWARD
    assert loops.unread == 0

    # And the idle turn after it finds nothing: the crossing was the vend's.
    assert controller.run_once() is None
    assert ENTRY_UNADMITTED not in kinds(controller)


def test_the_new_case_is_never_folded_into_the_ordinary_one():
    """Two names, two reasons, and neither is the other. A stream of the new
    one is what a broken boom looks like; folded into `entry_confirmed` it
    would look like business as usual."""
    assert ENTRY_UNADMITTED != ENTRY_CONFIRMED
    assert REASON_NO_PENDING_ENTRY != REASON_FORWARD

    controller, _, _ = build(identities=[BANNED], crossings=[(F, 3.0)])
    controller.run_once()
    controller.run_once()

    assert ENTRY_CONFIRMED not in kinds(controller), "a crossing nothing admitted was confirmed"
    assert ENTRY_UNADMITTED in kinds(controller)
    assert detail(controller, ENTRY_UNADMITTED)["reason"] != REASON_FORWARD


def test_it_is_the_literal_the_platform_accepts():
    """`platform/src/app.js` `LANE_EVENT_KINDS` carries this string, added
    there first so a lane emitting it is not refused 400. Nothing in either CI
    compares the two lists; this pins the lane's half to the letter."""
    assert ENTRY_UNADMITTED == "entry_unadmitted"


# ---------------------------------------------------------------------------
# What it must not do.
# ---------------------------------------------------------------------------


def test_it_opens_no_session_and_moves_no_transit_state():
    """An entry nothing admitted has no identity to open a session on, and a
    session with none would be the phantom occupant in a different hat. And
    `TransitState` says what became of the last VEND; this had none."""
    controller, _, _ = build(
        crossings=[(ClosingSequence.NONE, 0.0), (F, 3.0)],
    )
    controller.run_once()  # allow, vend, nothing crosses in the window: HELD
    assert controller.transit_state == TransitState.HELD.value
    sessions_before = controller.events.pending_sessions

    controller.run_once()  # idle: the car goes through late

    assert ENTRY_UNADMITTED in kinds(controller)
    assert detail(controller, ENTRY_UNADMITTED)["last_transit"] == TransitState.HELD.value
    assert controller.events.pending_sessions == sessions_before, "the idle poll opened a session"
    assert not any(k in SESSION_KINDS for k in kinds(controller))
    assert controller.transit_state == TransitState.HELD.value, "the idle poll moved the transit"


def test_a_reverse_with_nothing_pending_is_read_and_is_not_an_entry(caplog):
    """B then A with no vend behind it is a vehicle leaving through an entry
    the wrong way. Not an entry of any kind; read, logged, not recorded."""
    controller, _, loops = build(identities=[BANNED], crossings=[(R, 3.0)])
    controller.run_once()

    with caplog.at_level(logging.WARNING, logger="lane_controller.controller"):
        controller.run_once()

    assert loops.unread == 0
    assert ENTRY_UNADMITTED not in kinds(controller)
    assert "reverse" in caplog.text and "not recorded" in caplog.text


def test_an_exit_lane_reads_the_crossing_and_records_nothing(caplog):
    """A car that left without paying is a different fact about different
    money, and no kind on the platform names it yet. Logged, not recorded, and
    not recorded as an ENTRY of anything."""
    controller, _, loops = build(direction="exit", identities=[BANNED], crossings=[(F, 3.0)])
    controller.run_once()

    with caplog.at_level(logging.WARNING, logger="lane_controller.controller"):
        controller.run_once()

    assert loops.unread == 0
    assert ENTRY_UNADMITTED not in kinds(controller)
    assert "EXIT" in caplog.text and "unbilled" in caplog.text


def test_a_lane_with_no_closing_loops_has_nothing_to_poll():
    controller, _, loops = build(identities=[BANNED], closing_loops=0)
    assert loops is None
    controller.run_once()
    assert controller.run_once() is None
    assert controller.observe_closing_loops() is None
    assert ENTRY_UNADMITTED not in kinds(controller)


# ---------------------------------------------------------------------------
# The crossing belongs to the vend while one is pending.
# ---------------------------------------------------------------------------


class HeldLoops(ClosingLoops):
    """Loops whose blocking read waits to be released, and whose idle read
    always has a crossing to offer -- so a poll that should not have happened
    shows up as a record that should not exist."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.polls = 0

    def wait_for_sequence(self, window_seconds: float) -> ClosingSequence:
        self.entered.set()
        self.release.wait(timeout=10)
        return ClosingSequence.FORWARD

    def poll_sequence(self) -> ClosingSequence:
        self.polls += 1
        return ClosingSequence.FORWARD


def test_a_crossing_while_a_vend_is_pending_is_the_vends_answer_not_a_new_entry():
    """Two gates, one crossing. Between `begin_transit` and the settle's
    blocking read the transit is PENDING and the poll stands down on that.
    Once the settle is inside its read it holds the loops, and the poll stands
    down on the lock -- including after the lane's own deadline has moved the
    transit off PENDING with the driver still blocked. Either gate failing is
    one crossing recorded as unadmitted AND answered to the vend."""
    loops = HeldLoops()
    controller, _, _ = build(arrivals=0, loops_impl=loops)
    at = to_iso(controller.now())

    # Gate one: pending, and nothing is reading yet.
    controller.begin_transit(ALLOWED, at)
    assert controller.transit_state == TransitState.PENDING.value
    assert controller.run_once() is None
    assert loops.polls == 0, "the idle poll read the loops while a vend was pending"
    assert ENTRY_UNADMITTED not in kinds(controller)

    # Gate two: the settle is inside its blocking read, and the lane's own
    # deadline has already answered the transit -- the shape an assisted vend
    # takes when its driver hangs. The claim is taken so the late return
    # records nothing, exactly as `vend.py` does it.
    claim = _Once()
    settle = threading.Thread(
        target=controller.resolve_transit, args=(ALLOWED, at), kwargs={"claim": claim}
    )
    settle.start()
    assert loops.entered.wait(timeout=5)
    assert claim.take(), "the deadline takes the claim first"
    controller.transit_timed_out(ALLOWED, at, deadline=1.0)
    assert controller.transit_state == TransitState.UNCONFIRMABLE.value

    assert controller.run_once() is None
    assert loops.polls == 0, "the idle poll read the loops while a settle was reading them"
    assert ENTRY_UNADMITTED not in kinds(controller)

    loops.release.set()
    settle.join(timeout=5)
    assert not settle.is_alive()
    assert ENTRY_CONFIRMED not in kinds(controller), "the late return recorded an outcome"

    # And with nothing pending and nothing reading, the poll runs.
    assert controller.run_once() is None
    assert loops.polls == 1
    assert kinds(controller)[-1] == ENTRY_UNADMITTED


# ---------------------------------------------------------------------------
# It leaves the box, and it reaches the read contract.
# ---------------------------------------------------------------------------


def test_the_record_leaves_the_box_on_the_idle_turn_that_made_it():
    """The next ordinary arrival may be hours away, and at a lane whose
    barrier is up it never vends. The idle turn that recorded it flushes."""
    transport = RecordingTransport()
    controller, _, _ = build(identities=[BANNED], crossings=[(F, 3.0)], transport=transport)
    controller.run_once()
    batches_before = len(transport.batches)

    controller.run_once()

    delivered = [e.kind for batch in transport.batches[batches_before:] for e in batch]
    assert ENTRY_UNADMITTED in delivered, "the record stayed on the box"
    assert controller.events.pending == 0


def test_it_is_on_the_read_contract_where_a_monitor_counts_it():
    """`GET /v1/lane/events?since=` is where the count of these is read. No
    field on `LaneHealth`, no contract version bump: a log event, in the
    window, opaque detail, no identity text."""
    controller, _, _ = build(identities=[BANNED], crossings=[(F, 3.0), (F, 3.0)])
    service = LaneService(controller)
    controller.run_once()
    controller.run_once()
    controller.run_once()

    page = service.events(0).to_dict()
    unadmitted = [e for e in page["events"] if e["kind"] == ENTRY_UNADMITTED]
    assert len(unadmitted) == 2
    for event in unadmitted:
        assert event["detail"]["reason"] == REASON_NO_PENDING_ENTRY
        assert "plate" not in event["detail"]
        assert event["occurred_at"].endswith("+00:00")


# ---------------------------------------------------------------------------
# Every implementation of the seam supplies the idle read.
# ---------------------------------------------------------------------------


def _every_closing_loops() -> list[type]:
    found: list[type] = []
    todo = list(ClosingLoops.__subclasses__())
    while todo:
        cls = todo.pop()
        found.append(cls)
        todo.extend(cls.__subclasses__())
    return found


def test_every_closing_loops_implementation_supplies_the_idle_read():
    """A `Protocol` method body is `...`, which returns None, so an
    implementation that leaves `poll_sequence` to the protocol would hand the
    controller None instead of a sequence and be silently never-crossing. Every
    class that names the seam supplies both reads itself."""
    implementations = _every_closing_loops()
    assert len(implementations) >= 2, "the walk found too few to be measuring anything"
    for cls in implementations:
        for method in ("wait_for_sequence", "poll_sequence"):
            owner = next(
                (base for base in cls.__mro__ if method in vars(base)),
                None,
            )
            assert owner is not None and owner is not ClosingLoops, (
                f"{cls.__module__}.{cls.__qualname__} leaves {method} to the protocol"
            )
