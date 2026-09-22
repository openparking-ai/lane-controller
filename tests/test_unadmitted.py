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

import json
import logging
import threading
import time
import urllib.error
import urllib.request

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
from lane_controller.service import LaneService, make_server
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
    ENTRY_UNCONFIRMABLE,
    REASON_FORWARD,
    REASON_NO_PENDING_ENTRY,
    SESSION_OPEN,
    to_iso,
)
from lane_controller.vend import _Once
from serving import serving

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
    Once the settle is inside its read that read is OUTSTANDING, and the poll
    stands down on the count -- including after the lane's own deadline has
    moved the transit off PENDING with the driver still blocked, because the
    abandoned worker is still on the board. Either gate failing is one crossing
    recorded as unadmitted AND answered to the vend."""
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
    assert loops.polls == 0, "the idle poll read the loops while a settle's read was outstanding"
    assert ENTRY_UNADMITTED not in kinds(controller)

    loops.release.set()
    settle.join(timeout=5)
    assert not settle.is_alive()
    assert ENTRY_CONFIRMED not in kinds(controller), "the late return recorded an outcome"

    # And with nothing pending and nothing reading, the poll runs.
    assert controller.run_once() is None
    assert loops.polls == 1
    assert kinds(controller)[-1] == ENTRY_UNADMITTED


class PollThatStartsAVend(ClosingLoops):
    """A board whose idle read, while it is IN FLIGHT, sees a vend begin on
    another thread -- the moment the count alone could not cover. Records
    whether the vend's read entered the board before the poll's read left it."""

    def __init__(self, controller_ref: dict) -> None:
        self.ref = controller_ref
        self.poll_in_flight = False
        self.overlapped = False
        self.settle: threading.Thread | None = None

    def wait_for_sequence(self, window_seconds: float) -> ClosingSequence:
        self.overlapped = self.overlapped or self.poll_in_flight
        return ClosingSequence.FORWARD

    def poll_sequence(self) -> ClosingSequence:
        self.poll_in_flight = True
        controller = self.ref["controller"]
        at = to_iso(controller.now())

        def vend():
            controller.begin_transit(ALLOWED, at)
            controller.resolve_transit(ALLOWED, at)

        self.settle = threading.Thread(target=vend, daemon=True)
        self.settle.start()
        self.settle.join(timeout=0.2)  # a vend's read that does not wait gets in here
        self.poll_in_flight = False
        return ClosingSequence.NONE


def test_a_vends_read_never_overlaps_a_polls_read_in_flight():
    """The count check and the read are ONE moment, and a vend's READ that
    begins inside a poll's read waits for that read to finish -- microseconds,
    by the seam's own promise -- rather than reading the same board at the same
    time. Two overlapping driver calls on one board give one crossing to
    whichever wins, and the first count-only design measured 3 of 200 lost that
    way. (A vend that BEGINS inside a poll's check is the straddle test below;
    this one is about the read.)"""
    ref: dict = {}
    board = PollThatStartsAVend(ref)
    controller, _, _ = build(arrivals=0, loops_impl=board)
    ref["controller"] = controller

    assert controller.run_once() is None  # the idle poll, and a vend inside it
    assert board.settle is not None
    board.settle.join(timeout=5)
    assert not board.settle.is_alive(), "the vend's read never ran: it must wait, not starve"
    assert board.overlapped is False, (
        "the vend's read entered the board while the poll's read was in flight"
    )
    assert controller.transit_state == TransitState.CONFIRMED.value
    assert ENTRY_UNADMITTED not in kinds(controller)


class BoardWithOneCar(ClosingLoops):
    """A board a test can put one crossing on, at the moment it chooses. The
    blocking read finds it or times out; the idle read pops it or finds none."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.crossings: list[ClosingSequence] = []
        self.polls = 0
        self.poll_hits = 0

    def cross(self) -> None:
        with self.lock:
            self.crossings.append(ClosingSequence.FORWARD)

    def wait_for_sequence(self, window_seconds: float) -> ClosingSequence:
        deadline = time.monotonic() + window_seconds
        while time.monotonic() < deadline:
            with self.lock:
                if self.crossings:
                    return self.crossings.pop(0)
            time.sleep(0.001)
        return ClosingSequence.NONE

    def poll_sequence(self) -> ClosingSequence:
        with self.lock:
            self.polls += 1
            if self.crossings:
                self.poll_hits += 1
                return self.crossings.pop(0)
            return ClosingSequence.NONE


def test_a_vend_that_begins_while_the_poll_is_inside_its_check_waits_for_the_read(monkeypatch):
    """THE STRADDLE. The poll has answered "nothing pending" and has not yet
    read the board; a vend begins now. Two moments here is a car admitted and
    then written down as one nothing admitted: the poll's read, still to come,
    takes the crossing the vend's own read is waiting for -- measured 23 to 42
    of 200 when the check sat outside the lock. One critical section is the
    fix, and this holds the poll inside it to prove the beginning vend waits
    for the CLAIM -- not for the read, which is outside the lock and is the
    route-never-waits test's business below.

    Two independent assertions, each red on its own when either half of the
    fix is reverted: `begin_transit` must still be waiting while the poll is
    held mid-check, and the crossing that follows the vend must be the vend's
    -- either never seen by the poll, or seen and handed over, whichever way
    the scheduler orders the poll's read and the car.
    """
    board = BoardWithOneCar()
    controller, _, _ = build(arrivals=0, loops_impl=board)
    at = to_iso(controller.now())
    parked = threading.Event()
    release = threading.Event()
    arm = {"on": True}
    checks_pending = LaneController._nothing_pending

    def check_and_park(self):
        answer = checks_pending(self)
        if answer and arm["on"]:
            arm["on"] = False
            parked.set()
            release.wait(timeout=5)  # a poll held between its check and its read
        return answer

    monkeypatch.setattr(LaneController, "_nothing_pending", check_and_park)

    poll = threading.Thread(target=controller.run_once, kwargs={"timeout": 0}, daemon=True)
    poll.start()
    assert parked.wait(timeout=5), "the idle poll never reached its check"

    vend = threading.Thread(target=controller.begin_transit, args=(ALLOWED, at), daemon=True)
    vend.start()
    vend.join(timeout=0.3)
    assert vend.is_alive(), (
        "a vend BEGAN while the poll was between its check and its read: "
        "the pending state was published outside the poll's critical section"
    )
    assert controller.transit_state != TransitState.PENDING.value

    # The car crosses once the vend has begun -- never before the barrier was
    # commanded -- and the poll is released. Whichever order the lock allows,
    # the crossing must be the vend's.
    def cross_after_the_vend_begins():
        vend.join(timeout=5)
        board.cross()

    car = threading.Thread(target=cross_after_the_vend_begins, daemon=True)
    car.start()
    release.set()
    poll.join(timeout=5)
    car.join(timeout=5)
    assert not poll.is_alive() and not vend.is_alive() and not car.is_alive()
    assert controller.transit_state == TransitState.PENDING.value

    controller.resolve_transit(ALLOWED, at)
    assert board.poll_hits == controller._crossings_handed, (
        "the idle poll took the crossing of a vend that had begun and kept it"
    )
    assert controller.transit_state == TransitState.CONFIRMED.value
    assert ENTRY_UNADMITTED not in kinds(controller), (
        "a car the lane admitted was recorded as one nothing admitted"
    )
    assert ENTRY_CONFIRMED in kinds(controller)


# ---------------------------------------------------------------------------
# One hung loop driver must cost what it cost before the idle read existed:
# one leaked thread, and nothing else.
# ---------------------------------------------------------------------------

UNREADABLE = VehicleIdentity(plate=None, confidence=0.10, presence=True)


def a_lane_that_completes(
    *identities, window: float = 0.1, grace: float = 0.05, loops_impl: ClosingLoops | None = None
):
    """The assisted vend's lane: a fallback case held, `vend.py` reachable
    through `LaneService`, a settle bounded by a short window and grace."""
    config = LaneConfig(
        lane_id="lane-test",
        site_id="site-test",
        camera=CameraConfig(camera_id="sim-cam-1", rtsp_url="", frames_per_read=1),
        gate=GateConfig(),
        direction="entry",
        confidence_threshold=0.85,
        loops=LoopConfig(arming_loops=1, closing_loops=2, confirmation_window_seconds=window),
        settle_grace_s=grace,
    )
    cache = DecisionCache()
    cache.load([], default_action="allow")
    controller = LaneController(
        config,
        loop=SimulatedLoopInput(arrivals=1),
        camera=CannedCameraFeed(),
        vend=RecordingVendOutput(),
        identifier=StubVehicleIdentifier(list(identities) or [UNREADABLE]),
        closing_loops=loops_impl if loops_impl is not None else ScriptedClosingLoops([]),
        cache=cache,
    )
    controller.run_once()
    assert controller.last_decision.outcome is Outcome.FALLBACK
    return controller


def complete(service, controller, key):
    from lane_controller.vend import parse

    body = {
        "authorised_by": "human_open_now",
        "identity": {"kind": "ticket", "ticket_ref": "TKT-4RS9WQ2M"},
        "decision_at": controller.last_decision_at,
    }
    status, _ = service.assisted.complete(parse(body, key))
    assert status == 202
    service.assisted.settling.join(timeout=5)
    assert not service.assisted.settling.is_alive()


def hang_the_driver(controller):
    """A `wait_for_sequence` that does not return until told to. The thread
    inside it is the one `vend.py` abandons at the deadline."""
    released = threading.Event()
    controller.closing_loops.wait_for_sequence = lambda window: (
        released.wait(timeout=10) or ClosingSequence.NONE
    )
    return released


def resolve_workers() -> set[threading.Thread]:
    """Every `assisted-vend-resolve` thread alive right now, BY IDENTITY.

    The instrument used to be a COUNT of threads by name, compared against a
    count taken at the start of the test. A count is a population figure, and
    the population is not this test's: workers abandoned by EARLIER tests are
    still alive when this one starts (each test releases its hung driver only
    at its end, and the thread takes a moment to die), so one of them dying
    between the baseline and the assertion moves the baseline under the test
    -- `1 == 1 + 1`, twice on push CI (after PR #28 and after PR #29), green
    on every rerun. The instrument was the defect, not the code it guards.

    So: a SET of the threads themselves. `leaked_since(before)` is exactly the
    workers this test started that are still alive, whatever any other test's
    workers do in the meantime.
    """
    return {t for t in threading.enumerate() if t.name == "assisted-vend-resolve"}


def leaked_since(before: set[threading.Thread]) -> set[threading.Thread]:
    return {t for t in resolve_workers() - before if t.is_alive()}


def wait_until_none_leaked_since(before: set[threading.Thread], seconds: float = 2.0) -> None:
    deadline = time.monotonic() + seconds
    while leaked_since(before) and time.monotonic() < deadline:
        time.sleep(0.005)


def test_after_one_hung_driver_the_next_vends_still_settle_and_bill():
    """THE DEFECT THE GATE FOUND. The first idle-read design held a mutex
    across the settle's read; `vend.py` abandons a worker whose driver never
    returns, and that worker was still inside the read, holding the mutex --
    so every later vend's settle waited behind it until its own deadline:
    `unconfirmable`, no session, one more leaked thread each, while every one
    of them still answered 202. `main` bills them. This lane must too."""
    controller = a_lane_that_completes()
    service = LaneService(controller)
    before = resolve_workers()
    released = hang_the_driver(controller)

    complete(service, controller, "KEY-001")
    assert controller.transit_state == TransitState.UNCONFIRMABLE.value
    assert service.assisted._in_progress is False
    assert len(leaked_since(before)) == 1, "the hung worker is the one leaked thread"

    # The driver recovers: a NEW call answers at once. The hung one is still
    # hung. Two more cars, two more completions.
    controller.closing_loops.wait_for_sequence = lambda window: ClosingSequence.FORWARD
    for n, key in ((2, "KEY-002"), (3, "KEY-003")):
        controller.loop._remaining = 1
        controller.run_once()
        complete(service, controller, key)
        assert controller.transit_state == TransitState.CONFIRMED.value, (
            f"vend {n} settled to {controller.transit_state}: it waited behind the hung read"
        )
        assert controller.events.pending_sessions == n - 1, f"vend {n} billed nobody"

    assert len(leaked_since(before)) == 1, "a later vend leaked a thread of its own"
    released.set()


def test_after_one_hung_driver_the_next_ordinary_arrival_is_served():
    """The same hang, then a car the lane admits on its own. `handle_arrival`
    settles on the loop thread with no deadline of its own: waiting on the
    abandoned read there is the barrier opening and `run_once` never
    returning -- no arrivals, no idle polls, and a `held` for a car that
    crossed forward once the driver finally came back."""
    controller = a_lane_that_completes(UNREADABLE, ALLOWED)
    service = LaneService(controller)
    released = hang_the_driver(controller)
    complete(service, controller, "KEY-001")
    assert controller.transit_state == TransitState.UNCONFIRMABLE.value

    controller.closing_loops.wait_for_sequence = lambda window: ClosingSequence.FORWARD
    controller.loop._remaining = 1
    served = threading.Event()
    outcome = {}

    def serve():
        outcome["decision"] = controller.run_once()
        served.set()

    threading.Thread(target=serve, daemon=True).start()
    returned = served.wait(timeout=3)
    released.set()
    assert returned, (
        "run_once did not return in 3s: the ordinary settle waited behind the abandoned read "
        f"(vends={controller.vend.vend_count}, transit={controller.transit_state})"
    )
    assert outcome["decision"].outcome is Outcome.ALLOW
    assert controller.transit_state == TransitState.CONFIRMED.value
    assert controller.events.pending_sessions == 1


def test_the_idle_read_stands_down_behind_an_abandoned_driver_and_only_that():
    """What the hang DOES cost, stated: while the abandoned worker is still on
    the board the idle read skips its turn -- a poll behind it would be the
    second reader the count exists to stop -- and it resumes the moment the
    driver returns. Nothing waits."""
    controller = a_lane_that_completes()
    service = LaneService(controller)
    polls = {"n": 0}

    def poll():
        polls["n"] += 1
        return ClosingSequence.NONE

    controller.closing_loops.poll_sequence = poll
    released = hang_the_driver(controller)
    complete(service, controller, "KEY-001")

    for _ in range(20):
        assert controller.run_once(timeout=0) is None
    assert polls["n"] == 0, "the idle read polled a board with a read outstanding"
    assert not controller._nothing_reading()

    released.set()
    service.assisted.settling.join(timeout=5)
    deadline = time.monotonic() + 2
    while not controller._nothing_reading() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert controller._nothing_reading(), "the count did not come down when the driver returned"
    assert controller.run_once(timeout=0) is None
    assert polls["n"] == 1


# ---------------------------------------------------------------------------
# The vend route waits on no loop driver, in either direction.
# ---------------------------------------------------------------------------

READ_TOKEN = "read-token-aaaa"
ACT_TOKEN = "act-token-bbbb"


class PollThatHangs(ClosingLoops):
    """A board whose idle read does not return until told to, and whose
    blocking read answers at once: the poll driver hung, the settle driver
    fine. The direction the outside review of 2026-09-19 measured."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.answer = ClosingSequence.NONE
        self.blocking_reads = 0

    def wait_for_sequence(self, window_seconds: float) -> ClosingSequence:
        self.blocking_reads += 1
        return ClosingSequence.FORWARD

    def poll_sequence(self) -> ClosingSequence:
        self.entered.set()
        self.release.wait(timeout=10)
        return self.answer


def a_hung_poll(controller):
    """One idle turn on its own thread -- the loop thread's shape -- parked
    inside the poll driver, with the car still on the arming loop."""
    controller.loop._remaining = 0
    idle = threading.Thread(target=controller.run_once, kwargs={"timeout": 0}, daemon=True)
    idle.start()
    assert controller.closing_loops.entered.wait(timeout=5), (
        "the idle poll never reached the driver"
    )
    # The simulated arming loop reads clear after an idle turn; the real car
    # is still sitting on it while the loop thread is inside the driver.
    controller.loop._occupied = True
    return idle


def post_vend(base, controller, key, *, timeout):
    """`POST /v1/lane/vend` over the socket, exactly as the intercom sends it."""
    payload = {
        "authorised_by": "human_open_now",
        "identity": {"kind": "ticket", "ticket_ref": "TKT-4RS9WQ2M"},
        "decision_at": controller.last_decision_at,
    }
    request = urllib.request.Request(
        f"{base}/v1/lane/vend",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ACT_TOKEN}",
            "Idempotency-Key": key,
        },
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}"), time.monotonic() - started
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}"), time.monotonic() - started
    except TimeoutError:
        return None, {}, time.monotonic() - started


def test_the_vend_route_never_waits_on_a_hung_poll_driver():
    """THE DEFECT THE OUTSIDE REVIEW FOUND. The poll's read sat under the
    board lock, `_transit` publishes PENDING under it, so a poll driver that
    hung held `POST /v1/lane/vend` inside `complete_vend` AFTER the relay had
    pulsed: no 202, a retry refused `already_completed`, the lane `busy` for as
    long as the driver hung -- measured no answer in 3 s at `0c91190` and 202
    in 20 ms at `df982e2`, where the poll did not exist. Now the route
    answers with the poll driver still hung, and the vend it opened settles
    once the driver comes back."""
    loops = PollThatHangs()
    controller = a_lane_that_completes(loops_impl=loops)
    service = LaneService(controller)
    idle = a_hung_poll(controller)
    try:
        with serving(make_server(service, port=0, token=READ_TOKEN, act_token=ACT_TOKEN)) as base:
            status, answered, took = post_vend(base, controller, "KEY-001", timeout=2.0)
            assert status == 202, (
                f"the vend route answered {status} after {took:.2f}s with the poll driver hung "
                f"(relay pulsed: {controller.vend.vend_count == 1}, "
                f"transit={controller.transit_state})"
            )
            assert answered["transit"] == TransitState.PENDING.value
            assert took < 1.0, f"202 took {took:.2f}s: the route waited on something"
            assert controller.vend.vend_count == 1
            assert idle.is_alive(), "the poll driver is still hung; that is the premise"
            assert controller.transit_state == TransitState.PENDING.value

            # The settle is waiting behind the poll's read -- on its own
            # thread, under its own deadline -- and not the route.
            assert loops.blocking_reads == 0, "the vend's read overlapped the poll's read in flight"
            loops.release.set()
            service.assisted.settling.join(timeout=5)
            assert not service.assisted.settling.is_alive()
    finally:
        loops.release.set()
    idle.join(timeout=5)
    assert not idle.is_alive()
    assert controller.transit_state == TransitState.CONFIRMED.value
    assert loops.blocking_reads == 1
    assert ENTRY_UNADMITTED not in kinds(controller)
    assert controller.events.pending_sessions == 1


def test_begin_transit_never_waits_on_a_hung_poll_driver():
    """The same property at the seam the route reaches, with no socket in
    the way, so a failure here names the lock and not the server."""
    loops = PollThatHangs()
    controller = a_lane_that_completes(loops_impl=loops)
    idle = a_hung_poll(controller)
    at = to_iso(controller.now())
    identity = VehicleIdentity(plate=None, presence=True, ticket_ref="TKT-4RS9WQ2M")
    vend = threading.Thread(target=controller.begin_transit, args=(identity, at), daemon=True)
    vend.start()
    vend.join(timeout=1.0)
    alive = vend.is_alive()
    loops.release.set()
    idle.join(timeout=5)
    assert not alive, (
        "begin_transit waited on the poll driver: "
        "PENDING is published under a lock a driver call holds"
    )
    assert controller.transit_state == TransitState.PENDING.value


def test_the_vend_route_never_waits_on_a_hung_settle_driver_either():
    """THE OTHER DIRECTION, kept measured: hang the blocking read instead and
    the route still answers in the same time. The deadline design from this
    round's first blocker; it must not regress while the poll's is fixed."""
    controller = a_lane_that_completes()
    service = LaneService(controller)
    released = hang_the_driver(controller)
    polls = {"n": 0}

    def poll():
        polls["n"] += 1
        return ClosingSequence.NONE

    controller.closing_loops.poll_sequence = poll
    controller.loop._remaining = 0
    assert controller.run_once(timeout=0) is None
    controller.loop._occupied = True
    with serving(make_server(service, port=0, token=READ_TOKEN, act_token=ACT_TOKEN)) as base:
        status, answered, took = post_vend(base, controller, "KEY-001", timeout=2.0)
        assert status == 202, f"{status} after {took:.2f}s"
        assert took < 1.0
        service.assisted.settling.join(timeout=5)
    assert controller.transit_state == TransitState.UNCONFIRMABLE.value
    released.set()


def test_the_leak_instrument_is_immune_to_another_tests_worker_dying_mid_measurement():
    """THE FLAKE, PLANTED, AND THE REPAIR MEASURED AGAINST IT. A worker with the
    real name, left over from "an earlier test", is alive at the baseline and
    dies between the completion and the assertion -- exactly the interleaving
    push CI produced twice. The old instrument (a count by name) reads
    `1 == 1 + 1` on it; the set-by-identity instrument reads one leaked worker,
    which is the truth. Both readings are taken here so the repair is not a
    claim: the old one is required to be WRONG on this run."""
    stray_release = threading.Event()
    stray = threading.Thread(
        target=stray_release.wait, kwargs={"timeout": 10},
        name="assisted-vend-resolve", daemon=True,
    )
    stray.start()
    controller = a_lane_that_completes()
    service = LaneService(controller)
    old_count_before = len(resolve_workers())
    before = resolve_workers()
    released = hang_the_driver(controller)
    try:
        complete(service, controller, "KEY-001")
        stray_release.set()  # the earlier test's worker dies now, mid-measurement
        stray.join(timeout=5)
        assert not stray.is_alive()
        old_count_after = len(resolve_workers())
        assert old_count_after != old_count_before + 1, (
            "the planted race did not move the baseline; the control measured nothing"
        )
        assert len(leaked_since(before)) == 1, "the repaired instrument mis-read the leak"
    finally:
        released.set()


def test_a_hung_poll_driver_costs_the_held_case_its_confirmation_and_nothing_on_the_route():
    """What `docs/CONTRACT.md` says a hung idle read costs, measured: the
    case the lane was holding is answered 202, its settle waits behind the
    hung read under the settle's own deadline and lands `unconfirmable` with
    `loop_driver_timeout`, the lane's route is free again, and the worker is
    the one leaked thread. The loop thread itself is inside the driver, and
    nothing here pretends otherwise: no further car is decided."""
    loops = PollThatHangs()
    loops.answer = ClosingSequence.FORWARD  # and when it comes back, it comes back with a car
    controller = a_lane_that_completes(loops_impl=loops)
    service = LaneService(controller)
    before = resolve_workers()
    idle = a_hung_poll(controller)
    try:
        complete(service, controller, "KEY-001")  # 202, and the settle joined at its deadline
        assert controller.transit_state == TransitState.UNCONFIRMABLE.value
        assert controller.loop_driver_timed_out
        assert service.assisted._in_progress is False, "the route is not free after the deadline"
        assert len(leaked_since(before)) == 1, (
            "the settle behind the hung read is the one leaked worker"
        )
        assert idle.is_alive(), "the loop thread is still inside the driver; that is the cost"
        assert loops.blocking_reads == 0
    finally:
        loops.release.set()
    idle.join(timeout=5)
    wait_until_none_leaked_since(before)
    # The driver came back with FORWARD. A vend began during that read, so
    # the crossing is that vend's and not a new `entry_unadmitted` -- the car
    # the lane admitted and then gave up on may be exactly what crossed. The
    # abandoned worker took it, found its claim gone, and recorded nothing.
    # Unconfirmable it stays, as a late driver return has always left it.
    assert controller._crossings_handed == 1
    assert controller._handed is None, "the handed crossing was not consumed"
    assert controller.transit_state == TransitState.UNCONFIRMABLE.value
    assert ENTRY_CONFIRMED not in kinds(controller)
    assert ENTRY_UNADMITTED not in kinds(controller), (
        "a car the lane admitted was recorded as one nothing admitted"
    )


def test_a_crossing_the_poll_holds_when_a_vend_begins_during_its_read_is_the_vends():
    """P3. The poll claimed the board and is inside its read; a vend begins
    -- and, the route no longer waiting, its PENDING goes up while the poll is
    out. The poll comes back holding FORWARD. That crossing is neither thrown
    away (a lost promotion) nor recorded as `entry_unadmitted` (an admitted
    car mislabelled): it is handed to the vend, whose read takes it without
    asking the driver, and the vend is confirmed on it."""
    loops = PollThatHangs()
    loops.answer = ClosingSequence.FORWARD
    controller, _, _ = build(arrivals=0, loops_impl=loops)
    idle = a_hung_poll(controller)
    at = to_iso(controller.now())

    controller.begin_transit(ALLOWED, at)  # returns at once: the read is outside the lock
    assert controller.transit_state == TransitState.PENDING.value
    settle = threading.Thread(target=controller.resolve_transit, args=(ALLOWED, at), daemon=True)
    settle.start()
    settle.join(timeout=0.3)
    assert settle.is_alive(), "the vend's read did not wait for the poll's read in flight"
    assert loops.blocking_reads == 0

    loops.release.set()
    settle.join(timeout=5)
    idle.join(timeout=5)
    assert not settle.is_alive() and not idle.is_alive()
    assert loops.blocking_reads == 0, (
        "the vend asked the driver instead of taking the handed crossing"
    )
    assert controller._crossings_handed == 1
    assert controller._handed is None, "the handed crossing was not consumed"
    assert controller.transit_state == TransitState.CONFIRMED.value
    assert ENTRY_CONFIRMED in kinds(controller)
    assert ENTRY_UNADMITTED not in kinds(controller), (
        "an admitted car was recorded as one nothing admitted"
    )
    assert SESSION_OPEN in kinds(controller)

    # And with nothing pending, the next idle read records what it finds.
    loops.entered.clear()
    loops.release.clear()
    loops.release.set()
    assert controller.run_once(timeout=0) is None
    assert kinds(controller)[-1] == ENTRY_UNADMITTED


def test_a_handed_crossing_is_taken_by_the_transit_it_was_handed_for_and_by_nothing_else():
    """P5. The poll is inside its read when V1 begins; the poll comes back
    holding FORWARD and hands it over WITH V1's `at`. Before V1's read has
    taken it, a second transit begins through the other door -- the loop
    thread's own next arrival, allowed by plate. Its read must leave the slot
    alone and ask the driver; V1's read, when it comes, takes its own crossing
    without asking. Unbound, the slot went to whichever read came first, and
    car 2 was confirmed on V1's crossing before it had crossed."""
    loops = PollThatHangs()
    loops.answer = ClosingSequence.FORWARD
    controller, _, _ = build(arrivals=0, loops_impl=loops)
    idle = a_hung_poll(controller)
    at_v1 = to_iso(controller.now())
    controller.begin_transit(ALLOWED, at_v1)  # V1: PENDING published during the poll's read
    loops.release.set()
    idle.join(timeout=5)
    assert controller._crossings_handed == 1
    assert controller._handed == (ClosingSequence.FORWARD, at_v1), "the slot does not carry V1's at"

    # Car 2, through the other door, before V1's read: an ordinary arrival
    # the loop thread serves, allowed, vended, and read -- with its own `at`.
    controller.loop._remaining = 1
    decision = controller.run_once()
    assert decision is not None and decision.outcome is Outcome.ALLOW
    assert loops.blocking_reads == 1, "car 2's read took the slot instead of asking the driver"
    assert controller._handed == (ClosingSequence.FORWARD, at_v1), "car 2's read took V1's crossing"
    assert controller.transit_state == TransitState.CONFIRMED.value  # on its OWN crossing

    # V1's read, late: it takes its own slot and asks nothing.
    controller.resolve_transit(ALLOWED, at_v1)
    assert loops.blocking_reads == 1, "V1's read asked the driver instead of taking its slot"
    assert controller._handed is None, "V1's handed crossing was not consumed"
    assert kinds(controller).count(ENTRY_CONFIRMED) == 2
    assert kinds(controller).count(SESSION_OPEN) == 2
    assert ENTRY_UNADMITTED not in kinds(controller)


def test_a_timed_out_vends_handed_crossing_is_not_the_next_cars():
    """The door the gate of 2026-09-19 measured: V1 (assisted) begins during
    the poll's read and is answered `unconfirmable` by the settle's deadline
    before the poll's driver returns FORWARD. The crossing is handed with V1's
    `at`; the next car -- allowed on the loop thread, its read racing V1's
    abandoned worker -- must not take it: it asks the driver and is confirmed
    on its own crossing. V1's abandoned worker takes its own slot, finds its
    claim gone, and records nothing: no `entry_confirmed` for V1, no session,
    and NOT `entry_unadmitted` -- the lane admitted that car. The crossing is
    invisible in the record; `entry_unconfirmable` is what says why."""
    loops = PollThatHangs()
    loops.answer = ClosingSequence.FORWARD
    controller = a_lane_that_completes(loops_impl=loops)
    service = LaneService(controller)
    before = resolve_workers()
    # V1's worker is parked on its way to the board -- inside the window
    # read `resolve_transit` makes just before `_read_closing_loops` -- so the
    # race the gate measured (19 to 38 of 200) is decided the same way every
    # time: car 2's read reaches the board FIRST. Only the first caller parks;
    # car 2's own resolve goes straight through.
    parked = threading.Event()
    window = controller._confirmation_window
    calls = []

    def park_the_first_reader():
        calls.append(1)
        if len(calls) == 1:
            parked.wait(timeout=10)
        return window()

    controller._confirmation_window = park_the_first_reader
    idle = a_hung_poll(controller)
    complete(service, controller, "KEY-001")  # 202, and the settle joined at its deadline
    assert controller.transit_state == TransitState.UNCONFIRMABLE.value
    at_v1 = controller.transit_since
    loops.release.set()
    idle.join(timeout=5)
    assert controller._crossings_handed == 1
    assert controller._handed == (ClosingSequence.FORWARD, at_v1)

    # Car 2 at once, allowed by plate, on the loop thread, ahead of V1's read.
    controller.identifier = StubVehicleIdentifier([ALLOWED])
    controller.loop._remaining = 1
    decision = controller.run_once()
    assert decision is not None and decision.outcome is Outcome.ALLOW
    assert controller.transit_state == TransitState.CONFIRMED.value
    assert loops.blocking_reads == 1, "car 2's read took V1's crossing instead of asking the driver"
    assert controller._handed == (ClosingSequence.FORWARD, at_v1), "car 2's read took V1's slot"

    parked.set()  # V1's abandoned worker reaches the board now
    wait_until_none_leaked_since(before)
    assert not leaked_since(before), "V1's abandoned worker never took its slot"
    assert controller._handed is None, "V1's handed crossing was not consumed"
    assert controller.transit_since != at_v1  # car 2's transit, not V1's, is what is published
    recorded = kinds(controller)
    assert recorded.count(ENTRY_UNCONFIRMABLE) == 1
    assert recorded.count(ENTRY_CONFIRMED) == 1, "V1 was confirmed after its deadline"
    assert recorded.count(SESSION_OPEN) == 1, "the timed-out vend opened a session"
    assert ENTRY_UNADMITTED not in recorded, (
        "a car the lane admitted was recorded as one nothing admitted"
    )


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
