"""The QR scanner (Phase 1b): a registration QR, scanned for the car the lane
just refused, presented through the platform to the pass module's enrolment.

Each guarantee is paired with the case that falsifies it, and
`scripts/qr_fail_control.py` breaks the lane once per guarantee and requires
this file to go red:

  1  at a pass-only garage's entry, a car no register covers, or whose pass
     the lane could not confirm, is opened by a QR the module answers covered
     -- and stays shut on any other answer
  2  the scan is presented with what the camera read, sure or not, and with
     nothing when it read nothing; the lane's direction is the platform's
     to know, never sent
  3  a QR never overrules the owner: a banned car, a plate the owner denied,
     a full garage, a lane closed to everyone are not presented
  4  a QR is for the car at the barrier: with the arming loop clear, or the
     refusal already completed, it is not presented; one refusal, one vend
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
           closing=None, presenter=None, platform=None, crossings=((OCC, 3.0),)):
    """A lane wired to the simulated seams, a FakePlatform and a scanner. The
    closing loop holds one crossing for the car a QR lets in; a test of a gate
    that must stay shut passes none, so no idle turn finds a car to record."""
    config = LaneConfig(
        lane_id=f"lane-{direction}",
        site_id="site-1",
        camera=CameraConfig(camera_id="sim-cam-1", rtsp_url="", frames_per_read=2),
        gate=GateConfig(),
        direction=direction,
        confidence_threshold=THRESHOLD,
        loops=LoopConfig(arming_loops=1, closing_loops=1, closing_spacing_m=0.0,
                         confirmation_window_seconds=10.0),
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
    loop = ParkedLoop()
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


@pytest.mark.parametrize(("identity", "fallback", "sent"), [
    (LOW, Fallback.LOW_CONFIDENCE, STRANGER),
    (NO_PLATE, Fallback.NO_PLATE_READ, None),
], ids=["below the confidence", "nothing read"])
def test_a_pass_only_car_whose_pass_could_not_be_confirmed_is_presented_with_what_was_read(
    identity, fallback, sent
):
    controller, vend, _, scanner, presenter, _ = a_lane(identity)
    refused = controller.run_once()
    assert (refused.outcome, refused.fallback) == (Outcome.FALLBACK, fallback)
    scanner.scan(QR)
    controller.run_once()
    assert presenter.calls[0][1] == sent
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
    assert presenter.calls[0][1] == STRANGER
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
