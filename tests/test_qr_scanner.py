"""The QR scanner (Phase 1b): a registration QR, scanned for the car the lane
just refused, presented through the platform to the pass module's enrolment.

Each guarantee is paired with the case that falsifies it, and
`scripts/qr_fail_control.py` breaks the lane once per guarantee and requires
this file to go red:

  1  at a pass-only garage's entry, a car no register covers, or whose pass
     the lane could not confirm, is opened by a QR the module answers covered
     -- and stays shut on any other answer
  2  the scan is presented with the plate the lane stands behind, and with
     nothing when nothing usable was read -- no plate, or one below the
     confidence: the pass module's rule (similar enough gets a pass) decides,
     in one place; the lane's direction is the platform's to know, never sent
  3  a QR never overrules the owner: a banned car, a plate the owner denied
     (read at any confidence, or the plate the QR was made for), a full
     garage, a lane closed to everyone are not opened
  4  a QR is for the car at the barrier: with the arming loop clear, a car on
     the deactivate loop, the second arming loop clear, or the refusal
     already completed, it is not presented; and the gate opens only for the
     car that was there when it was scanned -- not for one that backed off
     while the platform was asked, nor for whoever rolled on; one refusal,
     one vend
  8  an opening by one difference or by nothing read is recorded: the rule
     and the QR's id on `vended`, the car's pictures against that event, and
     no plate on any event or log line
  5  offline the gate stays shut, and `qr_presented` with `answered: false`
     says why -- except where the platform recorded it itself
  6  the token is kept nowhere: not in an event, a log line or the outbox;
     and no plate rides on `qr_presented`
  7  an exit the lane did not let out is let out by a QR the module covers,
     and the close carries no local decision, so the platform asks the modules
"""

from __future__ import annotations

import io
import json
import logging
import time
import urllib.error

import pytest

from fake_platform import FakePlatform
from lane_controller import (
    CameraConfig,
    DecisionCache,
    GateConfig,
    LaneConfig,
    LaneController,
    LoopConfig,
    VehicleIdentity,
)
from lane_controller.cli import wire_lane
from lane_controller.decision import REASON_NOT_REGISTERED, Fallback, Outcome, Rule
from lane_controller.events import EventQueue
from lane_controller.interfaces import ClosingSequence
from lane_controller.platform_client import PlatformClient, PlatformRejected, PlatformUnreachable
from lane_controller.simulated import (
    CannedCameraFeed,
    ExactBannedMatcher,
    OccupancyLoopInput,
    RecordingVendOutput,
    ScriptedClosingLoops,
    SimulatedLoopInput,
    SimulatedQrScanner,
    StubVehicleIdentifier,
)
from lane_controller.sync import QR_PRESENTED, REASON_QR_OPENED, PlatformTransport
from test_exit_decision import NEW_YORK, NOW, gp_register, gp_row

OCC = ClosingSequence.OCCUPIED
THRESHOLD = 0.85
QR = "https://pass.example/e/QR-TOKEN-7f3a9c"

HOLDER = "PASS-777"
STRANGER = "CAR-123"
SEEN = VehicleIdentity(plate=STRANGER, confidence=0.97, presence=True,
                       descriptor="opvid-opa-id/2:a")
LOW = VehicleIdentity(plate=STRANGER, confidence=0.42, presence=True)
NO_PLATE = VehicleIdentity(plate=None, confidence=0.97, presence=True)
BANNED_DESCRIPTOR = "opvid-opa-id/2:banned"
FULL = {"state": "closed", "reason": "full", "message": "Full.", "closed_at": None}
EVERYONE = {"state": "closed", "reason": "everyone", "message": "Closed.", "closed_at": None}

COVERED = {"open": True, "outcome": "covered",
           "enrolment": {"enrolment": "qr-1", "recognised": True},
           "answer": {"outcome": "covered"}}
NOT_COVERED = {"open": False, "outcome": "not_covered",
               "enrolment": {"enrolment": None, "refused": "REFUSAL_CREDENTIAL_UNKNOWN"},
               "answer": {"outcome": "not_covered"}}


class ParkedLoop(SimulatedLoopInput):
    """An arming loop that reports one arrival and keeps reading the car,
    which is what a refused car waiting at the barrier does."""

    def __init__(self) -> None:
        super().__init__(arrivals=1)
        self.parked = True

    def wait_for_vehicle(self, timeout: float | None = None) -> bool:
        arrived = super().wait_for_vehicle(timeout)
        self._occupied = self.parked
        return arrived

    def is_occupied(self) -> bool:
        return self.parked


class Presenter:
    """The platform's `redeem-enrolment`, as the lane calls it."""

    def __init__(self, answer=COVERED, raises=None) -> None:
        self.answer = answer
        self.raises = raises
        self.calls: list[tuple] = []

    def __call__(self, token, vehicle, presented_at):
        self.calls.append((token, vehicle, presented_at))
        if self.raises is not None:
            raise self.raises
        return self.answer


def a_lane(identity, *, direction="entry", transient=False, register=True, rules=(), banned=(),
           closing=None, presenter=None, platform=None, crossings=((OCC, 3.0),), loop=None,
           deactivate=None, arming_b=None):
    """A lane wired to the simulated seams, a FakePlatform and a scanner. The
    closing loop holds one crossing for the car a QR lets in; a test of a gate
    that must stay shut passes none, so no idle turn finds a car to record.
    `deactivate` and `arming_b` are loops of their own (`OccupancyLoopInput`)
    for a lane that has them."""
    loops = {"arming_loops": 2 if arming_b is not None else 1, "closing_loops": 1,
             "closing_spacing_m": 0.0, "confirmation_window_seconds": 10.0}
    if deactivate is not None:
        loops.update(deactivate_loops=1, deactivate_spacing_m=6.0, longest_vehicle_tail_m=5.0)
    config = LaneConfig(
        lane_id=f"lane-{direction}",
        site_id="site-1",
        camera=CameraConfig(camera_id="sim-cam-1", rtsp_url="", frames_per_read=2),
        gate=GateConfig(),
        direction=direction,
        confidence_threshold=THRESHOLD,
        loops=LoopConfig(**loops),
    )
    cache = DecisionCache(max_age_seconds=3600)
    cache.load(list(rules), default_action="allow", now=time.time())
    cache.transient_available = transient
    cache.timezone = NEW_YORK
    if register:
        cache.entitlements["garage_pass"] = {"consulted": True,
                                             "register": gp_register(gp_row(HOLDER))}
        cache._reindex_coverage()
    if closing is not None:
        cache._take_lane({"lane": closing})
    cache.banned = list(banned)
    platform = platform if platform is not None else FakePlatform()
    platform.reject_close_without_open = False
    scanner = SimulatedQrScanner()
    presenter = presenter if presenter is not None else Presenter()
    vend = RecordingVendOutput()
    loop = loop if loop is not None else ParkedLoop()
    controller = LaneController(
        config,
        loop=loop,
        camera=CannedCameraFeed(),
        vend=vend,
        identifier=StubVehicleIdentifier([identity]),
        closing_loops=ScriptedClosingLoops(list(crossings)),
        cache=cache,
        events=EventQueue(PlatformTransport(platform)),
        banned_matcher=ExactBannedMatcher(),
        clock=lambda: NOW,
        qr_scanner=scanner,
        qr_presenter=presenter,
        deactivate_loop=deactivate,
        arming_loop_b=arming_b,
    )
    return controller, vend, platform, scanner, presenter, loop


def qr_events(platform):
    return [e for e in platform.events.values() if e["kind"] == QR_PRESENTED]


# ---------------------------------------------------------------------------
# 1 -- a pass-only entry: covered opens, anything else stays shut
# ---------------------------------------------------------------------------


def test_a_car_no_register_covers_is_opened_by_a_qr_the_module_covers():
    controller, vend, platform, scanner, presenter, _ = a_lane(SEEN)
    refused = controller.run_once()
    assert (refused.outcome, refused.reason) == (Outcome.DENY, REASON_NOT_REGISTERED)
    assert vend.vend_count == 0
    scanner.scan(QR)
    opened = controller.run_once()
    assert opened is not None and opened.should_vend
    assert [reason for _, reason in vend.vends] == [REASON_QR_OPENED]
    [(token, vehicle, presented_at)] = presenter.calls
    assert (token, vehicle) == (QR, STRANGER)
    assert presented_at.startswith("2026-")
    [stay] = platform.opened
    assert stay["plate"] == STRANGER and stay["no_open_command"] is False
    assert controller.last_decision_completed_at is not None


def test_a_qr_the_module_does_not_cover_keeps_the_gate_shut():
    controller, vend, platform, scanner, presenter, _ = a_lane(
        SEEN, presenter=Presenter(NOT_COVERED), crossings=())
    controller.run_once()
    scanner.scan(QR)
    assert controller.run_once() is None
    assert vend.vend_count == 0
    assert len(presenter.calls) == 1
    assert controller.last_decision_completed_at is None, "the refusal still stands for a person"
    assert platform.opened == []


# ---------------------------------------------------------------------------
# 2 -- what is presented: the read, sure or not, or nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("identity", "fallback"), [
    (LOW, Fallback.LOW_CONFIDENCE),
    (NO_PLATE, Fallback.NO_PLATE_READ),
], ids=["below the confidence", "nothing read"])
def test_a_car_whose_plate_was_not_read_usably_is_presented_with_nothing(identity, fallback):
    """Below the lane's confidence is NOTHING USABLE, as no plate is: the pass
    module's rule passes a valid QR on it (rule c). The unsure text never
    leaves the lane for the module to compare -- the decision is the
    module's, on what the lane stands behind."""
    controller, vend, _, scanner, presenter, _ = a_lane(identity)
    refused = controller.run_once()
    assert (refused.outcome, refused.fallback) == (Outcome.FALLBACK, fallback)
    scanner.scan(QR)
    controller.run_once()
    assert presenter.calls[0][1] is None
    assert vend.vend_count == 1


def test_no_register_held_a_qr_is_still_presented():
    controller, vend, _, scanner, presenter, _ = a_lane(SEEN, register=False)
    assert controller.run_once().fallback is Fallback.STALE_RULES
    scanner.scan(QR)
    controller.run_once()
    assert len(presenter.calls) == 1 and vend.vend_count == 1


# ---------------------------------------------------------------------------
# 3 -- a QR never overrules the owner
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["denied by rule", "banned", "full", "closed to everyone"])
def test_a_qr_does_not_open_what_the_owner_shut(case):
    kwargs = {
        "denied by rule": {"rules": [Rule(plate=STRANGER, allow=False)]},
        "banned": {"banned": [{"id": "ban-1", "descriptor": "opvid-opa-id/2:a"}]},
        "full": {"closing": FULL},
        "closed to everyone": {"closing": EVERYONE},
    }[case]
    controller, vend, _, scanner, presenter, _ = a_lane(SEEN, **kwargs)
    assert not controller.run_once().should_vend
    scanner.scan(QR)
    controller.run_once()
    assert presenter.calls == [] and vend.vend_count == 0


def test_a_garage_that_takes_transients_has_nothing_for_a_qr_to_open():
    controller, vend, _, scanner, presenter, _ = a_lane(SEEN, transient=True)
    assert controller.run_once().should_vend
    scanner.scan(QR)
    controller.run_once()
    assert presenter.calls == [] and vend.vend_count == 1


# ---------------------------------------------------------------------------
# 4 -- the car at the barrier, once
# ---------------------------------------------------------------------------


def test_a_qr_with_the_car_gone_is_not_presented():
    controller, vend, _, scanner, presenter, loop = a_lane(SEEN)
    controller.run_once()
    loop.parked = False
    scanner.scan(QR)
    controller.run_once()
    assert presenter.calls == [] and vend.vend_count == 0


def test_one_refusal_one_vend_a_second_scan_is_not_presented():
    controller, vend, _, scanner, presenter, _ = a_lane(SEEN)
    controller.run_once()
    scanner.scan(QR)
    controller.run_once()
    scanner.scan(QR)
    controller.run_once()
    assert len(presenter.calls) == 1 and vend.vend_count == 1


def test_a_refusal_a_person_completed_is_not_presented():
    controller, vend, _, scanner, presenter, _ = a_lane(SEEN)
    controller.run_once()
    controller.last_decision_completed_at = "2026-09-21T14:00:00+00:00"
    scanner.scan(QR)
    controller.run_once()
    assert presenter.calls == [] and vend.vend_count == 0


def test_no_presenter_no_qr():
    controller, vend, _, scanner, _, _ = a_lane(SEEN)
    controller.qr_presenter = None
    controller.run_once()
    scanner.scan(QR)
    controller.run_once()
    assert vend.vend_count == 0


# ---------------------------------------------------------------------------
# 5 -- offline: shut, and said
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("raised", "reason"), [
    (PlatformUnreachable("timed out"), "platform unreachable"),
    (PlatformRejected(409, "{}", "qr_not_linked"), "garage not linked to the pass module"),
    (PlatformRejected(409, "{}", "qr_stale"), "presented too late"),
    (PlatformRejected(429, "{}"), "rate limited"),
], ids=["unreachable", "not linked", "stale", "rate limited"])
def test_a_qr_that_cannot_be_checked_keeps_the_gate_shut_and_says_why(raised, reason):
    controller, vend, platform, scanner, _, _ = a_lane(SEEN, presenter=Presenter(raises=raised))
    controller.run_once()
    scanner.scan(QR)
    assert controller.run_once() is None
    assert vend.vend_count == 0
    [event] = qr_events(platform)
    assert event["detail"] == {"answered": False, "reason": reason, "direction": "entry",
                               "read_identity": True}


def test_the_module_unable_to_answer_is_recorded_by_the_platform_not_twice():
    raised = PlatformUnreachable("HTTP 503", status=503, code="qr_unanswered")
    controller, vend, platform, scanner, _, _ = a_lane(SEEN, presenter=Presenter(raises=raised))
    controller.run_once()
    scanner.scan(QR)
    controller.run_once()
    assert vend.vend_count == 0
    assert qr_events(platform) == []


# ---------------------------------------------------------------------------
# 6 -- the token is kept nowhere
# ---------------------------------------------------------------------------


def test_the_token_is_in_no_event_and_no_log_line_and_no_plate_rides_on_qr_presented(caplog):
    caplog.set_level(logging.DEBUG)
    offline = Presenter(raises=PlatformUnreachable("x"))
    for presenter in (Presenter(), Presenter(NOT_COVERED), offline):
        controller, _, platform, scanner, _, _ = a_lane(SEEN, presenter=presenter)
        controller.run_once()
        scanner.scan(QR)
        controller.run_once()
        assert QR not in json.dumps(list(platform.events.values()), default=str)
        assert QR not in json.dumps(platform.opened, default=str)
        for event in qr_events(platform):
            assert STRANGER not in json.dumps(event, default=str)
    assert QR not in caplog.text
    # CONTROL: the log does carry the lane's words about the QR.
    assert "QR" in caplog.text


# ---------------------------------------------------------------------------
# 7 -- the exit
# ---------------------------------------------------------------------------


def test_an_exit_not_let_out_is_let_out_by_a_covered_qr_and_the_close_asks_the_modules():
    controller, vend, platform, scanner, presenter, _ = a_lane(
        LOW, direction="exit", transient=True)
    refused = controller.run_once()
    assert (refused.outcome, refused.fallback) == (Outcome.FALLBACK, Fallback.LOW_CONFIDENCE)
    scanner.scan(QR)
    controller.run_once()
    assert vend.vend_count == 1
    assert presenter.calls[0][1] is None, "below the confidence is nothing usable"
    [closed] = platform.closed
    assert closed.get("local_decision") is None, "the modules are asked at the close"


def test_an_exit_closed_to_everyone_is_not_opened_by_a_qr():
    controller, vend, _, scanner, presenter, _ = a_lane(LOW, direction="exit", transient=True,
                                                        closing=EVERYONE)
    controller.run_once()
    scanner.scan(QR)
    controller.run_once()
    assert presenter.calls == [] and vend.vend_count == 0


# ---------------------------------------------------------------------------
# 3b -- the owner's deny, first and always (2026-10-30)
# ---------------------------------------------------------------------------


#: The plate the QR is made for, in the pass module's normal form.
HOLDER_KEY = HOLDER.replace("-", "")


def passed(rule, qr_plate=HOLDER_KEY):
    """A covered answer whose enrolment half says how the module's rule passed."""
    return {"open": True, "outcome": "covered",
            "enrolment": {"enrolment": "qr-1", "recognised": True, "passed_by": rule,
                          "qr_plate": qr_plate},
            "answer": {"outcome": "covered"}}


@pytest.mark.parametrize("transient", [False, True], ids=["pass only", "takes transients"])
def test_a_plate_read_below_the_confidence_that_the_owner_denied_is_denied_first(transient):
    """Fix 3: a read below the confidence used to fall to "the pass could not
    be confirmed" before the rule was looked up, and a QR then let the denied
    car in. The deny is asked first, of whatever was read."""
    controller, vend, _, scanner, presenter, _ = a_lane(
        LOW, transient=transient, rules=[Rule(plate=STRANGER, allow=False)], crossings=())
    refused = controller.run_once()
    assert refused.outcome is Outcome.DENY and refused.reason == "plate is denied by rule"
    assert not refused.qr_may_open
    scanner.scan(QR)
    controller.run_once()
    assert presenter.calls == [] and vend.vend_count == 0


def test_a_deny_the_lane_learned_after_the_refusal_still_keeps_the_qr_back():
    """`observe_qr` asks the deny itself, of the read at any confidence: the
    cache may have taken the rule since the car was refused."""
    controller, vend, _, scanner, presenter, _ = a_lane(LOW, crossings=())
    assert controller.run_once().fallback is Fallback.LOW_CONFIDENCE
    controller.cache.load([Rule(plate=STRANGER, allow=False)], default_action="allow",
                          now=time.time())
    scanner.scan(QR)
    controller.run_once()
    assert presenter.calls == [] and vend.vend_count == 0


@pytest.mark.parametrize("rule", ["nothing_read", "one_difference", "look_alike", "same_plate"])
def test_the_owner_denying_the_qrs_own_car_keeps_the_gate_shut(rule):
    """Deny wins ALSO WHEN NOTHING WAS READ: the car a QR lets in is the car
    it was made for, and the module says which (`qr_plate`). The owner typed
    the rule with a dash; the module's normal form has none."""
    controller, vend, platform, scanner, presenter, _ = a_lane(
        NO_PLATE, rules=[Rule(plate=HOLDER, allow=False)], presenter=Presenter(passed(rule)),
        crossings=())
    controller.run_once()
    scanner.scan(QR)
    assert controller.run_once() is None
    assert len(presenter.calls) == 1 and vend.vend_count == 0
    assert controller.last_decision_completed_at is None, "the refusal stands for a person"
    [said] = [e for e in platform.events.values()
              if e["kind"] == "decision" and e["detail"]["outcome"] == "deny"
              and "QR" in e["detail"]["reason"]]
    assert HOLDER not in json.dumps(said) and HOLDER_KEY not in json.dumps(said)


def test_the_control_without_the_owners_deny_the_same_answer_opens():
    controller, vend, _, scanner, _, _ = a_lane(NO_PLATE, presenter=Presenter(
        passed("nothing_read")))
    controller.run_once()
    scanner.scan(QR)
    assert controller.run_once() is not None and vend.vend_count == 1


# ---------------------------------------------------------------------------
# 4b -- the car that was there when the QR was scanned, and no other
# ---------------------------------------------------------------------------


class SlowPresenter(Presenter):
    """A platform call that takes `seconds`, during which `during` runs once
    (a car backing off, another rolling on, a person completing)."""

    def __init__(self, seconds, during=None, after=None, answer=COVERED):
        super().__init__(answer)
        self.seconds = seconds
        self.during = during
        self.after = after

    def __call__(self, token, vehicle, presented_at):
        self.calls.append((token, vehicle, presented_at))
        if self.during is not None:
            self.during()
        time.sleep(self.seconds)
        if self.after is not None:
            self.after()
        return self.answer


def left_events(platform):
    return [e for e in platform.events.values()
            if e["kind"] == "fallback_needs_human" and e["detail"].get("needs") == "left"]


def test_a_car_that_backs_off_while_its_qr_is_checked_is_not_let_in():
    """Fix 1, the reviewer's probe: the refused car reverses during the call
    and the loop stays clear -- nothing opens, a person is told."""
    loop = ParkedLoop()
    presenter = SlowPresenter(0.7, during=lambda: setattr(loop, "parked", False))
    controller, vend, platform, scanner, _, _ = a_lane(SEEN, presenter=presenter, loop=loop,
                                                       crossings=())
    controller.run_once()
    scanner.scan(QR)
    assert controller.run_once() is None
    assert vend.vend_count == 0 and platform.opened == []
    assert controller.last_decision_completed_at is None
    assert len(left_events(platform)) == 1


def test_a_car_that_rolls_on_after_the_scanned_car_backed_off_is_not_let_in():
    """Fix 1, the swap: the scanned car backs off during the call, the next car
    pulls up before the answer -- the loop reads occupied at the end, and only
    the watch saw the clear. Its stay would have carried the first car's read."""
    loop = ParkedLoop()
    presenter = SlowPresenter(0.8, during=lambda: setattr(loop, "parked", False),
                              after=lambda: setattr(loop, "parked", True))
    controller, vend, platform, scanner, _, _ = a_lane(SEEN, presenter=presenter, loop=loop,
                                                       crossings=())
    controller.run_once()
    scanner.scan(QR)
    assert controller.run_once() is None
    assert vend.vend_count == 0 and platform.opened == []
    assert len(left_events(platform)) == 1


def test_the_control_a_car_that_stays_put_through_a_slow_check_is_let_in():
    controller, vend, platform, scanner, _, _ = a_lane(SEEN, presenter=SlowPresenter(0.7))
    controller.run_once()
    scanner.scan(QR)
    assert controller.run_once() is not None
    assert vend.vend_count == 1 and left_events(platform) == []


class ScriptedLoop(ParkedLoop):
    """The arming loop's readings in order after the arrival, the last one
    repeated: deterministic where the watch thread takes no sample."""

    def __init__(self, readings):
        super().__init__()
        self.readings = list(readings)

    def is_occupied(self) -> bool:
        return self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]


def test_the_loop_is_read_again_as_the_lane_decides():
    """The watch's own readings all say occupied -- before the call, at its
    start, as it stops -- and the car is gone when the lane decides: the final
    reading is the one that keeps the gate shut."""
    # run_once's idle reads, then: the pre-check, the watch's first and last
    # samples, and the decision's read (clear, read again after a sample: gone)
    controller, vend, platform, scanner, _, loop = a_lane(SEEN, crossings=())
    controller.run_once()
    loop_reads = ScriptedLoop([True, True, True, False, False])
    controller.loop = loop_reads
    scanner.scan(QR)
    controller.observe_qr()
    assert vend.vend_count == 0 and len(left_events(platform)) == 1


class FailingLoop(ParkedLoop):
    """Reads fine on the lane's thread, and raises on the watch's."""

    def is_occupied(self) -> bool:
        import threading

        if threading.current_thread().name == "qr-loop-watch":
            raise OSError("the loop driver stopped answering")
        return True


def test_a_loop_that_cannot_be_read_while_the_qr_is_checked_opens_nothing():
    loop = FailingLoop()
    controller, vend, platform, scanner, _, _ = a_lane(
        SEEN, presenter=SlowPresenter(0.4), loop=loop, crossings=())
    controller.run_once()
    scanner.scan(QR)
    assert controller.run_once() is None and vend.vend_count == 0


def test_a_person_who_completes_the_refusal_while_the_qr_is_checked_is_the_one_vend():
    """Fix 2: the recheck before the relay. A person's vend lands on its own
    thread while the platform is asked; the QR's answer then opens nothing."""
    holder = {}
    presenter = SlowPresenter(
        0.0, during=lambda: setattr(holder["c"], "last_decision_completed_at",
                                    "2026-09-21T14:00:00+00:00"))
    controller, vend, _, scanner, _, _ = a_lane(SEEN, presenter=presenter, crossings=())
    holder["c"] = controller
    controller.run_once()
    scanner.scan(QR)
    assert controller.run_once() is None
    assert len(presenter.calls) == 1 and vend.vend_count == 0


def test_a_car_on_the_deactivate_loop_keeps_the_qr_from_being_presented():
    """Fix 2: the deactivate-loop guard. A car too close behind is the
    tailgate a QR's opening would let through."""
    behind = OccupancyLoopInput(occupied=False)
    controller, vend, _, scanner, presenter, _ = a_lane(SEEN, deactivate=behind, crossings=())
    assert controller.run_once().reason == REASON_NOT_REGISTERED
    behind.set_occupied(True)
    scanner.scan(QR)
    controller.run_once()
    assert presenter.calls == [] and vend.vend_count == 0


def test_a_car_that_pulls_up_behind_while_the_qr_is_checked_is_not_followed_in():
    behind = OccupancyLoopInput(occupied=False)
    presenter = SlowPresenter(0.0, during=lambda: behind.set_occupied(True))
    controller, vend, platform, scanner, _, _ = a_lane(
        SEEN, deactivate=behind, presenter=presenter, crossings=())
    controller.run_once()
    scanner.scan(QR)
    assert controller.run_once() is None
    assert len(presenter.calls) == 1 and vend.vend_count == 0
    assert len(left_events(platform)) == 1


def test_the_second_arming_loop_clear_keeps_the_qr_from_being_presented():
    second = OccupancyLoopInput(occupied=True)
    controller, vend, _, scanner, presenter, _ = a_lane(SEEN, arming_b=second, crossings=())
    assert controller.run_once().reason == REASON_NOT_REGISTERED
    second.set_occupied(False)
    scanner.scan(QR)
    controller.run_once()
    assert presenter.calls == [] and vend.vend_count == 0


@pytest.mark.parametrize("answer", [
    {"open": False, "outcome": "refused_to_answer",
     "enrolment": {"enrolment": "qr-1", "refused": "REFUSAL_ENROLMENT_AT_WRONG_END"},
     "answer": {"outcome": "refused_to_answer"}},
    {"open": False, "outcome": "covered", "enrolment": {}, "answer": {"outcome": "covered"}},
    {"open": "true", "outcome": "covered", "enrolment": {}, "answer": {"outcome": "covered"}},
    {"outcome": "covered", "enrolment": {}, "answer": {"outcome": "covered"}},
], ids=["refused to answer", "covered but not open", "open as text", "no open"])
def test_the_gate_opens_on_open_true_and_on_nothing_else(answer):
    """Fix 2: a plant that opened on `outcome != not_covered` passed the
    suite. Refused to answer is not covered, and `open` is the platform's word,
    `True` exactly."""
    controller, vend, _, scanner, presenter, _ = a_lane(SEEN, presenter=Presenter(answer),
                                                        crossings=())
    controller.run_once()
    scanner.scan(QR)
    assert controller.run_once() is None
    assert len(presenter.calls) == 1 and vend.vend_count == 0


# ---------------------------------------------------------------------------
# 8 -- the record of an opening the module's rule passed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rule", ["one_difference", "nothing_read"])
def test_an_opening_on_a_read_that_was_not_the_plate_is_kept_with_pictures(rule, caplog):
    caplog.set_level(logging.DEBUG)
    identity = NO_PLATE if rule == "nothing_read" else SEEN
    controller, vend, platform, scanner, _, _ = a_lane(identity, presenter=Presenter(
        passed(rule)))
    controller.run_once()
    scanner.scan(QR)
    assert controller.run_once() is not None and vend.vend_count == 1
    [vended] = [e for e in platform.events.values() if e["kind"] == "vended"]
    assert vended["detail"] == {"reason": REASON_QR_OPENED, "qr_passed_by": rule,
                                "enrolment": "qr-1"}
    kept = [p for p in platform.pictures.values() if p["event_id"] == vended["event_id"]]
    assert len(kept) == 2, "the car's pictures, against the opening itself"
    # THE READ IS ON THE STAY, as every stay keeps it; NO PLATE on any event
    # and in no log line
    [stay] = platform.opened
    assert stay.get("plate") == (STRANGER if identity is SEEN else None)
    events = json.dumps(list(platform.events.values()), default=str)
    for plate in (STRANGER, HOLDER, HOLDER_KEY):
        assert plate not in events and plate not in caplog.text


@pytest.mark.parametrize("rule", ["same_plate", "look_alike", None])
def test_an_opening_on_the_plate_itself_keeps_no_pictures_of_its_own(rule):
    answer = passed(rule) if rule else COVERED
    controller, vend, platform, scanner, _, _ = a_lane(SEEN, presenter=Presenter(answer))
    controller.run_once()
    scanner.scan(QR)
    assert controller.run_once() is not None and vend.vend_count == 1
    [vended] = [e for e in platform.events.values() if e["kind"] == "vended"]
    assert vended["detail"].get("qr_passed_by") == rule
    assert not [p for p in platform.pictures.values() if p["event_id"] == vended["event_id"]]


# ---------------------------------------------------------------------------
# The wire and the wiring
# ---------------------------------------------------------------------------


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_client_posts_the_scan_and_reads_the_platform_names():
    sent = []

    def opener(request, timeout):
        sent.append(request)
        return _Response(json.dumps(COVERED).encode())

    client = PlatformClient("https://platform.example", "device-token", opener=opener)
    assert client.redeem_enrolment(QR, None, "2026-09-21T14:00:00Z") == COVERED
    [request] = sent
    assert request.full_url == "https://platform.example/api/v1/lane/redeem-enrolment"
    assert json.loads(request.data) == {"token": QR, "vehicle": None,
                                        "presented_at": "2026-09-21T14:00:00Z"}

    def unanswered(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 503, "x", {},
                                     io.BytesIO(b'{"code":"qr_unanswered","error":"no"}'))

    with pytest.raises(PlatformUnreachable) as caught:
        PlatformClient("https://platform.example", "t", opener=unanswered).redeem_enrolment(
            QR, "A", "2026-09-21T14:00:00Z")
    assert (caught.value.status, caught.value.code) == (503, "qr_unanswered")


def test_a_lane_with_a_platform_wires_the_scanner_and_a_standalone_one_does_not():
    config = LaneConfig(lane_id="l", site_id="s", camera=CameraConfig(camera_id="c", rtsp_url=""),
                        gate=GateConfig(), direction="entry")
    standalone = wire_lane(config, platform=None)
    assert standalone.controller.qr_scanner is None
    assert standalone.seams["qr"].startswith("NONE")
    client = PlatformClient("https://platform.example", "t")
    wired = wire_lane(config, platform=client)
    assert wired.controller.qr_presenter == client.redeem_enrolment
    assert wired.seams["qr"].startswith("SIMULATED")
