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
  7  at an exit a QR opens nothing itself: a QR the module covers asks the
     car's case again AS THE SAME STAY -- the old case superseded on the
     platform, its payment's cancel queued, never left on the reader -- and
     the gate opens only when that case is settled, through the exit case's
     one opener; a case that names no stay is not asked again (a person is
     told); never while a card is being taken; once per QR; the rolled-on
     car rule holds across the swap
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
           deactivate=None, arming_b=None, exits=False):
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
        exits=platform if exits else None,
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


#: An exit case the platform has priced: the fee waits for the reader.
CASE_PAYMENT = {"settled": False, "needs": "payment", "payment": None,
                "fee": {"outcome": "transient", "covered_by": [], "fee_minor": 900,
                        "subtotal_minor": 900, "currency": "USD", "breakdown": [],
                        "refused": False}}


#: The stay the platform found for the car at the exit.
STAY = "stay-77"
#: A read below the confidence, with a colour code: the case's one ID field.
LOW_EXIT = VehicleIdentity(plate=STRANGER, confidence=0.42, presence=True, color_code="c-07")


def an_exit_waiting(answer, *, stay=STAY, payment=None):
    """An exit lane with the platform's exit cases wired, the car's case on
    payment and its fee handed to the reader: the gate shut until the case is
    settled. `stay`: the stay the platform found for the case (None: none)."""
    controller, vend, platform, scanner, presenter, loop = a_lane(
        LOW_EXIT, direction="exit", transient=True, exits=True, presenter=answer)
    platform.exit_match_session = stay
    platform.exit_answer = {**CASE_PAYMENT, **({"payment": payment} if payment else {})}
    refused = controller.run_once()
    assert refused.fallback is Fallback.EXIT_NOT_SETTLED and vend.vend_count == 0
    assert controller._exit_case.exit_id == "exit-1"
    return controller, vend, platform, scanner, presenter


def opened_cases(platform):
    return [r for r in platform.exit_requests if r[0] == "open"]


def requests(platform, kind):
    return [r for r in platform.exit_requests if r[0] == kind]


def requote_covered(holder):
    """The platform, asked again after the QR bound the car, covers it."""
    return lambda: holder["platform"].exit_answer.update({"settled": True, "why": "covered"})


def test_a_covered_qr_at_the_exit_asks_the_same_stay_and_the_one_opener_opens_once():
    """The rewiring, as the same stay: the new case names the stay the platform
    found for the old one -- so the platform supersedes the old case -- with
    the old case's ID fields, pictures under its own event, the old case's
    payment cancel queued; the barrier opens through `_let_out` once."""
    holder = {}
    presenter = SlowPresenter(0.0, during=requote_covered(holder), answer=passed("nothing_read"))
    controller, vend, platform, scanner, _ = an_exit_waiting(presenter)
    holder["platform"] = platform
    assert ("pay", "exit-1") in platform.exit_requests, "the old fee is on the reader"
    scanner.scan(QR)
    controller.run_once()
    first, second = opened_cases(platform)
    assert second[2] == STAY, "the new case names the stay the platform found"
    assert second[3] == first[3] == {"color_code": "c-07"}, "with the old case's ID fields"
    assert requests(platform, "supersede") == [("supersede", "exit-1", "exit-2")]
    assert ("pay_cancel", "exit-1") in platform.exit_requests, "the old payment's cancel"
    assert "exit-1" not in controller._cancels
    kept = [p for p in platform.pictures.values() if p["event_id"] == second[1]]
    assert kept, "the car's pictures under the new case's own event"
    assert controller._reader_case != "exit-1", "the reader is held for no stale case"
    assert vend.vend_count == 1
    [vended] = [e for e in platform.events.values() if e["kind"] == "vended"]
    assert (vended["detail"]["exit_id"], vended["detail"]["reason"]) == (
        "exit-2", "the exit is settled: covered")
    [closed] = platform.closed
    assert closed["exit_id"] == "exit-2"
    assert closed["qr"] == {"enrolment": "qr-1", "passed_by": "nothing_read",
                            "qr_plate": HOLDER_KEY}
    for _ in range(3):
        controller.run_once()
    assert vend.vend_count == 1, "one settled case, one opening"


def test_a_qr_the_module_covers_but_the_platform_still_prices_stays_on_payment():
    controller, vend, platform, scanner, _ = an_exit_waiting(Presenter(passed("one_difference")))
    scanner.scan(QR)
    controller.run_once()
    assert vend.vend_count == 0, "the gate opened on the QR with the case unsettled"
    assert controller._exit_case is not None and controller._exit_case.exit_id == "exit-2"
    assert requests(platform, "supersede") == [("supersede", "exit-1", "exit-2")]
    assert ("pay", "exit-2") in platform.exit_requests, "the fee goes to the reader"
    assert ("pay_cancel", "exit-1") in platform.exit_requests


def test_a_qr_the_module_does_not_cover_leaves_the_case_as_it_was():
    controller, vend, platform, scanner, _ = an_exit_waiting(Presenter(NOT_COVERED))
    scanner.scan(QR)
    controller.run_once()
    assert vend.vend_count == 0 and len(opened_cases(platform)) == 1
    assert controller._exit_case.exit_id == "exit-1"


def test_a_covered_qr_with_the_cars_case_let_go_opens_nothing():
    """The case was let go (here, to a person): there is nothing to settle, and
    a QR the module covers opens nothing on its own."""
    controller, vend, platform, scanner, _ = an_exit_waiting(Presenter(passed("same_plate")))
    controller._leave_exit_case("let go for the test")
    assert controller._exit_case is None
    scanner.scan(QR)
    controller.run_once()
    assert vend.vend_count == 0 and len(opened_cases(platform)) == 1


def test_a_covered_qr_for_a_case_with_no_stay_asks_nothing_again_and_tells_a_person():
    """Finding 1. No stay was found for the car (an unmatched car, the driver's
    word): asking again would open a SECOND case on the platform, matched from
    scratch, with the first one's payment on the reader. The case stays the
    case -- its fee on the reader, payable -- and a person is told, once."""
    controller, vend, platform, scanner, _ = an_exit_waiting(
        Presenter(passed("nothing_read")), stay=None)
    scanner.scan(QR)
    controller.run_once()
    scanner.scan(QR)
    controller.run_once()
    assert len(opened_cases(platform)) == 1 and requests(platform, "supersede") == []
    assert requests(platform, "pay_cancel") == [], "the payable fee was taken off"
    assert controller._exit_case.exit_id == "exit-1" and controller._reader_case == "exit-1"
    [told] = [e for e in platform.events.values()
              if e["kind"] == "fallback_needs_human" and e["detail"].get("needs") == "qr_covered"]
    assert (told["detail"]["exit_id"], told["detail"]["enrolment"]) == ("exit-1", "qr-1")
    platform.exit_answer = {"settled": True, "why": "paid"}
    controller._exit_case.asked_at = None
    controller.run_once()
    assert vend.vend_count == 1, "the driver paid the case on the reader; it opens"


def test_the_old_payment_is_cancelled_even_when_the_platform_drops_before_the_new_case():
    """Finding 1, the probe: the fee on the reader, the QR covered, the
    platform gone before the new case opens, then a person opens. The old
    case's cancel was queued and is asked when the platform is back."""
    from lane_controller.contract import VendAuthority
    from lane_controller.service import LaneService
    from lane_controller.vend import parse as parse_vend

    holder = {}
    presenter = SlowPresenter(
        0.0, during=lambda: setattr(holder["platform"], "online", False),
        answer=passed("same_plate"))
    controller, vend, platform, scanner, _ = an_exit_waiting(presenter)
    holder["platform"] = platform
    service = LaneService(controller)
    scanner.scan(QR)
    controller.run_once()
    assert "exit-1" in controller._cancels, "the old case's cancel is queued"
    body = {"authorised_by": VendAuthority.HUMAN_OPEN_NOW.value,
            "identity": {"kind": "ticket", "ticket_ref": "TKT-4RS9WQ2M"},
            "decision_at": controller.last_decision_at}
    status, said = service.assisted.complete(parse_vend(body, "KEY-QR-EXIT"))
    if service.assisted.settling is not None:
        service.assisted.settling.join(timeout=5)
    assert status == 202 and vend.vend_count == 1, said
    platform.online = True
    controller._cancels._pending["exit-1"].due = 0
    controller.retry_cancels()
    assert ("pay_cancel", "exit-1") in platform.exit_requests
    assert "exit-1" not in controller._cancels


@pytest.mark.parametrize("status", ["processing", "requires_capture", "succeeded"])
def test_no_case_is_asked_again_while_its_card_is_being_taken(status):
    """Finding 2: the old case's payment is being processed or taken -- no
    supersede, which would cancel a card mid-way."""
    controller, vend, platform, scanner, _ = an_exit_waiting(
        Presenter(passed("same_plate")), payment={"status": status, "last_failure": None})
    scanner.scan(QR)
    controller.run_once()
    assert len(opened_cases(platform)) == 1 and requests(platform, "supersede") == []
    assert controller._exit_case.exit_id == "exit-1" and vend.vend_count == 0


def test_the_same_qr_scanned_again_asks_the_case_again_once():
    """Finding 2: four more scans of the same QR while the stay is still
    priced do nothing more -- one supersede, one new fee hand-off."""
    controller, vend, platform, scanner, _ = an_exit_waiting(Presenter(passed("one_difference")))
    for _ in range(5):
        scanner.scan(QR)
        controller.run_once()
    assert len(opened_cases(platform)) == 2
    assert requests(platform, "supersede") == [("supersede", "exit-1", "exit-2")]
    assert requests(platform, "pay") == [("pay", "exit-1"), ("pay", "exit-2")]
    assert vend.vend_count == 0


def test_a_clear_longer_than_a_flicker_before_the_qr_drops_the_case_and_asks_nothing():
    """Finding 3: the old case's loop read clear for longer than a flicker --
    the car on the loop now may have rolled on -- and the QR answered for it
    asks nothing again: the case goes to a person, as `observe_exit_case`
    drops it. A shorter clear is a flicker, and the QR goes on."""
    holder = {}
    presenter = SlowPresenter(0.0, during=requote_covered(holder), answer=passed("same_plate"))
    controller, vend, platform, scanner, _ = an_exit_waiting(presenter)
    holder["platform"] = platform
    controller._exit_case.clear_since = NOW - 1.0
    scanner.scan(QR)
    controller.observe_qr()  # the turn's own read set the clear
    assert len(opened_cases(platform)) == 1 and vend.vend_count == 0
    assert controller._exit_case is None
    assert ("pay_cancel", "exit-1") in platform.exit_requests, "its payment off the reader"


def test_a_flicker_before_the_qr_does_not_stop_it():
    holder = {}
    presenter = SlowPresenter(0.0, during=requote_covered(holder), answer=passed("same_plate"))
    controller, vend, platform, scanner, _ = an_exit_waiting(presenter)
    holder["platform"] = platform
    controller._exit_case.clear_since = NOW - 0.2
    scanner.scan(QR)
    controller.observe_qr()  # the turn's own read set the clear
    assert len(opened_cases(platform)) == 2 and vend.vend_count == 1


def test_a_car_that_backs_off_while_its_exit_qr_is_checked_asks_nothing_again():
    loop = ParkedLoop()
    presenter = SlowPresenter(0.7, during=lambda: setattr(loop, "parked", False),
                              answer=passed("same_plate"))
    controller, vend, platform, scanner, _, _ = a_lane(
        LOW_EXIT, direction="exit", transient=True, exits=True, presenter=presenter, loop=loop)
    platform.exit_match_session = STAY
    platform.exit_answer = dict(CASE_PAYMENT)
    controller.run_once()
    scanner.scan(QR)
    controller.run_once()
    assert len(opened_cases(platform)) == 1 and vend.vend_count == 0
    assert len(left_events(platform)) == 1


def test_the_owners_deny_on_the_qrs_car_holds_at_the_exit_too():
    controller, vend, platform, scanner, _ = an_exit_waiting(Presenter(passed("same_plate")))
    controller.cache.load([Rule(plate=HOLDER, allow=False)], default_action="allow",
                          now=time.time())
    scanner.scan(QR)
    controller.run_once()
    assert len(opened_cases(platform)) == 1 and vend.vend_count == 0
    assert [e for e in platform.events.values()
            if e["kind"] == "decision" and "QR" in e["detail"]["reason"]]


def test_a_refusal_a_person_completed_during_the_exit_qr_asks_nothing_again():
    holder = {}
    presenter = SlowPresenter(0.0, during=lambda: setattr(
        holder["c"], "last_decision_completed_at", "2026-09-21T14:00:00+00:00"),
        answer=passed("same_plate"))
    controller, vend, platform, scanner, _ = an_exit_waiting(presenter)
    holder["c"] = controller
    scanner.scan(QR)
    controller.run_once()
    assert len(opened_cases(platform)) == 1 and vend.vend_count == 0


def test_the_case_is_swapped_under_the_exit_lock(monkeypatch):
    """A person's vend takes the case under `_exit_lock`; the QR's swap takes
    it under the same lock, so the two never both act on one case."""
    seen = []
    original = LaneController.__setattr__

    def recording(self, name, value):
        if name == "_exit_case" and value is not None and hasattr(self, "_exit_lock"):
            seen.append(self._exit_lock.locked())
        original(self, name, value)

    holder = {}
    presenter = SlowPresenter(0.0, during=requote_covered(holder), answer=passed("same_plate"))
    controller, vend, platform, scanner, _ = an_exit_waiting(presenter)
    holder["platform"] = platform
    monkeypatch.setattr(LaneController, "__setattr__", recording)
    scanner.scan(QR)
    controller.run_once()
    assert seen and all(seen), "the case was swapped without the exit lock"


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


def test_a_stale_deny_on_the_qrs_car_still_keeps_the_gate_shut_and_says_so():
    """The owner's decision of 2026-10-30: a deny always wins, also from rules
    past their age. Nothing read, the module passes the QR, and the owner
    denied the car it was made for: shut, and the record says it was stale."""
    controller, vend, platform, scanner, presenter, _ = a_lane(
        NO_PLATE, presenter=Presenter(passed("nothing_read")), crossings=())
    controller.cache.load([Rule(plate=HOLDER, allow=False)], default_action="allow",
                          now=time.time() - 7200)
    assert controller.cache.is_stale()
    controller.run_once()
    scanner.scan(QR)
    assert controller.run_once() is None
    assert len(presenter.calls) == 1 and vend.vend_count == 0
    [said] = [e for e in platform.events.values()
              if e["kind"] == "decision" and "QR" in e["detail"]["reason"]]
    assert said["detail"]["stale_deny"] is True


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


class Readings:
    """A loop whose readings are set by the test, one per sample."""

    def __init__(self):
        self.occupied = True

    def is_occupied(self) -> bool:
        return self.occupied


def test_a_clear_longer_than_a_flicker_seen_in_one_sample_is_a_car_gone():
    """The watch's time rule (loop_watch, half a second on the lane's clock):
    one clear sample only, because the samples came far apart, but the clear
    lasted longer than a flicker -- the car backed off and another rolled on."""
    from lane_controller.loop_watch import FLICKER_SECONDS, LoopWatch

    loop, now = Readings(), [100.0]
    watch = LoopWatch(loop, lambda: now[0])
    loop.occupied, now[0] = False, 100.1
    watch._sample()
    loop.occupied, now[0] = True, 100.1 + FLICKER_SECONDS + 0.1
    watch._sample()
    assert not watch.stayed(), "a clear longer than a flicker was taken for one"
    # THE CONTROL: the same single clear sample, shorter than a flicker
    loop, now = Readings(), [100.0]
    watch = LoopWatch(loop, lambda: now[0])
    loop.occupied, now[0] = False, 100.1
    watch._sample()
    loop.occupied, now[0] = True, 100.1 + FLICKER_SECONDS - 0.2
    watch._sample()
    assert watch.stayed()


# ---------------------------------------------------------------------------
# 9 -- the stay a QR opened names the QR, for the platform to tie them
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rule", ["one_difference", "nothing_read", "same_plate"])
def test_the_stay_a_qr_opened_carries_the_qr_and_how_it_passed(rule):
    identity = NO_PLATE if rule == "nothing_read" else SEEN
    controller, vend, platform, scanner, _, _ = a_lane(identity, presenter=Presenter(
        passed(rule)))
    controller.run_once()
    scanner.scan(QR)
    assert controller.run_once() is not None
    [stay] = platform.opened
    assert stay["qr"] == {"enrolment": "qr-1", "passed_by": rule, "qr_plate": HOLDER_KEY}
    assert stay.get("plate") == (STRANGER if identity is SEEN else None), "as read"


def test_an_exit_a_qr_let_out_carries_the_qr_on_its_close():
    holder = {}
    presenter = SlowPresenter(0.0, during=requote_covered(holder),
                              answer=passed("nothing_read"))
    controller, vend, platform, scanner, _ = an_exit_waiting(presenter)
    holder["platform"] = platform
    scanner.scan(QR)
    controller.run_once()
    [closed] = platform.closed
    assert closed["qr"] == {"enrolment": "qr-1", "passed_by": "nothing_read",
                            "qr_plate": HOLDER_KEY}


def test_a_stay_no_qr_opened_carries_no_qr():
    controller, vend, platform, _, _, _ = a_lane(SEEN, transient=True)
    assert controller.run_once().should_vend
    [stay] = platform.opened
    assert "qr" not in stay


def test_the_client_sends_the_qr_on_the_open_and_the_close():
    sent = []

    def opener(request, timeout):
        sent.append(json.loads(request.data))
        return _Response(json.dumps({"session": {"entry_confirmation": "confirmed",
                                                 "exit_confirmation": "confirmed"}}).encode())

    client = PlatformClient("https://platform.example", "t", opener=opener)
    qr = {"enrolment": "qr-1", "passed_by": "one_difference", "qr_plate": "PASS777"}
    client.open_session(event_id="e1", entry_at="2026-09-21T14:00:00Z",
                        entry_confirmation="confirmed", plate="PASS778", qr=qr)
    client.close_session(event_id="e2", exit_at="2026-09-21T15:00:00Z",
                         exit_confirmation="confirmed", plate="PASS778", qr=qr)
    client.open_session(event_id="e3", entry_at="2026-09-21T14:00:00Z",
                        entry_confirmation="confirmed", plate="PASS778")
    assert sent[0]["qr"] == qr and sent[1]["qr"] == qr and "qr" not in sent[2]


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
