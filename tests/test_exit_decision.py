"""The exit's local decision: covered from the cache, priced on the box, or
said to be unpriceable here -- and nothing on the barrier's path opens a
socket to the platform.

Each claim is paired with the case that falsifies it. The timing control
(brief 4.12) runs the REAL Vehicle ID service on loopback -- its HTTP
handler and its contract, with an injected engine standing in for the plate
recogniser, which needs torch this suite does not carry -- and a real
`PlatformClient` pointed at a dead port; every `connect()` the barrier's path
makes is counted by destination, and the only one allowed is loopback to
Vehicle ID. The whole of `run_once`, arrival to vend, is timed against his
one-second requirement, and the number is a FLOOR for a Jetson, said so.

`scripts/exit_decision_fail_control.py` breaks the decision in six ways and
requires this file to go red. The close and the reconciler (4.9, 4.9b) are
not here.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from fake_platform import FakePlatform
from lane_controller import CameraConfig, DecisionCache, GateConfig, LaneConfig
from lane_controller.controller import LaneController
from lane_controller.decision import normalise_identity
from lane_controller.events import EventQueue
from lane_controller.exit_pricing import (
    COVERED,
    ENGINE_REFUSED,
    NO_CACHED_ENTRY,
    PRICED,
    STALE_FACTS,
    local_day,
    price_exit,
    row_covers,
)
from lane_controller.interfaces import VehicleIdentity
from lane_controller.platform_client import PlatformClient
from lane_controller.simulated import (
    CannedCameraFeed,
    RecordingVendOutput,
    SimulatedLoopInput,
    StubVehicleIdentifier,
)
from lane_controller.sync import PlatformTransport
from lane_controller.vehicle_id_client import VehicleIdClient

PLAN = json.loads((Path(__file__).resolve().parent / "documents" / "flat_plan.json").read_text())
NEW_YORK = "America/New_York"
#: 2026-06-10 14:00 New York = 18:00 UTC. A day the fixtures' passes cover.
NOW = datetime(2026, 6, 10, 14, 0, tzinfo=ZoneInfo(NEW_YORK)).timestamp()
TODAY = date(2026, 6, 10)


def a_config(**overrides) -> LaneConfig:
    fields = dict(
        lane_id="lane-exit",
        site_id="site-exit",
        camera=CameraConfig(camera_id="sim-cam", rtsp_url="", frames_per_read=1),
        gate=GateConfig(),
        direction="exit",
    )
    fields.update(overrides)
    return LaneConfig(**fields)


def gp_register(*rows, passes=None):
    return {
        "garage": "gp-1",
        "unreadable_garage": None,
        "passes": passes or [
            {"pass": "p-1", "state": "active", "valid_from": "2026-01-01", "valid_to": "2026-12-31",
             "unreadable": None}
        ],
        "passes_not_naming_garage": [],
        "registrations": list(rows),
    }


def gp_row(identity, pass_id="p-1", effective="2026-01-01", end=None):
    return {"vehicle_identity": identity, "pass": pass_id, "effective_day": effective,
            "end_day": end, "ended_reason": "ended" if end else None}


def mb_register(*rows, agreements=None, not_found=()):
    return {
        "garage": "mb-1",
        "registrations": list(rows),
        "agreements": agreements or [
            {"agreement": "ag-1", "version": 1, "registrar": "this_module", "status": "active",
             "cancelled_effective_day": None}
        ],
        "agreements_not_covering_garage": [],
        "agreements_not_found": list(not_found),
    }


def a_platform(*, gp=None, mb=None, stays=()):
    platform = FakePlatform()
    platform.rate_plans = [PLAN]
    platform.timezone = NEW_YORK
    original = FakePlatform.get_rules

    def get_rules():
        payload = original(platform)
        payload["timezone"] = NEW_YORK
        return payload

    platform.get_rules = get_rules
    if gp is not None:
        platform.entitlements["garage_pass"] = {"consulted": True, "register": gp}
    if mb is not None:
        platform.entitlements["monthly_billing"] = {"consulted": True, "register": mb}
    for session_id, plate, entry_at in stays:
        stay = platform.stay_opened(session_id, plate=plate)
        stay["entry_at"] = entry_at
    return platform


def a_cache(platform, *, now=None) -> DecisionCache:
    """Refreshed NOW on the wall clock (the lane's `decide` judges staleness on
    it), unless a test says when; the decision's own clock is `NOW`, June
    2026, which is the day the fixtures' passes cover."""
    cache = DecisionCache(max_age_seconds=3600)
    cache.load_payload(platform.get_rules(), now=now)
    return cache


ENTRY = "2026-06-10T15:30:00+00:00"  # 2.5 h before NOW (18:00 UTC): three hourly periods at 300


# ---------------------------------------------------------------------------
# the folding and the day
# ---------------------------------------------------------------------------


def test_the_one_folding_under_which_a_read_plate_and_a_typed_identity_meet():
    assert normalise_identity("AB-123") == normalise_identity("ab 123") == "AB123"
    assert normalise_identity("ab123") == "AB123"
    assert normalise_identity("AB-124") != normalise_identity("AB-123"), (
        "the control: different plates differ"
    )


def test_the_day_is_the_garages_local_day_and_a_bad_zone_says_it_fell_to_utc():
    late_evening_ny = datetime(2026, 6, 10, 23, 30, tzinfo=ZoneInfo(NEW_YORK)).timestamp()
    assert local_day(late_evening_ny, NEW_YORK) == (date(2026, 6, 10), NEW_YORK)
    assert local_day(late_evening_ny, "UTC")[0] == date(2026, 6, 11), "the control: UTC is tomorrow"
    day, clock = local_day(late_evening_ny, "Mars/Olympus")
    assert day == date(2026, 6, 11) and clock.startswith("UTC (")
    assert local_day(late_evening_ny, None)[1].startswith("UTC (")


# ---------------------------------------------------------------------------
# what covers, by the modules' own semantics read with the lane's day
# ---------------------------------------------------------------------------


GP_HEAD = {
    "pass": "p-1", "state": "active", "valid_from": "2026-01-01", "valid_to": "2026-12-31",
    "unreadable": None,
}


@pytest.mark.parametrize(
    ("row", "head", "covers", "why"),
    [
        (gp_row("X"), GP_HEAD, True, "active, inside every day"),
        (gp_row("X", end="2026-06-10"), GP_HEAD, False, "ended today: end_day is exclusive"),
        (gp_row("X", end="2026-06-11"), GP_HEAD, True, "ends tomorrow"),
        (gp_row("X", effective="2026-06-11"), GP_HEAD, False, "starts tomorrow"),
        (gp_row("X"), {**GP_HEAD, "state": "suspended"}, False, "a hold"),
        (gp_row("X"), {**GP_HEAD, "state": "revoked"}, False, "revoked is revoked"),
        (gp_row("X"), {**GP_HEAD, "valid_to": "2026-06-09"}, False, "valid_to passed"),
        (gp_row("X"), {**GP_HEAD, "valid_to": "2026-06-10"}, True, "valid_to today: inclusive"),
        (gp_row("X"), {**GP_HEAD, "valid_from": "2026-06-11"}, False, "valid_from ahead"),
        (gp_row("X"), {**GP_HEAD, "valid_from": None, "valid_to": None}, True, "no range stated"),
        (
            gp_row("X"), {**GP_HEAD, "unreadable": {"code": "X"}}, False,
            "terms the module cannot read",
        ),
        (gp_row("X"), {}, False, "a row whose pass the register did not list"),
    ],
)
def test_a_garage_pass_row_covers_by_the_passs_state_and_the_four_days(row, head, covers, why):
    assert row_covers("garage_pass", row, head, TODAY) is covers, why


MB_HEAD = {
    "agreement": "ag-1", "version": 1, "registrar": "outside", "status": "active",
    "cancelled_effective_day": None,
}


@pytest.mark.parametrize(
    ("head", "covers", "why"),
    [
        (MB_HEAD, True, "active"),
        (
            {**MB_HEAD, "status": "cancelled", "cancelled_effective_day": "2026-06-11"}, True,
            "the paid period runs to its end",
        ),
        (
            {**MB_HEAD, "status": "cancelled", "cancelled_effective_day": "2026-06-10"}, False,
            "cancelled from today",
        ),
        (
            {**MB_HEAD, "status": "cancelled", "cancelled_effective_day": None}, False,
            "cancelled with no day: not covered",
        ),
        ({}, False, "an agreement the store holds no version of"),
    ],
)
def test_a_monthly_billing_row_covers_by_the_agreements_status_and_cancellation_day(
    head, covers, why
):
    row = {"identity_normalised": "x", "agreement": "ag-1"}
    assert row_covers("monthly_billing", row, head, TODAY) is covers, why


# ---------------------------------------------------------------------------
# the three answers, from the cache alone
# ---------------------------------------------------------------------------


def test_covered_from_a_pass_register_names_the_module_and_the_row_and_never_the_plate():
    cache = a_cache(a_platform(gp=gp_register(gp_row("PASS-1")), stays=[("s-1", "PASS-1", ENTRY)]))
    answer = price_exit("pass 1", cache, now=NOW)  # the camera's spelling, not the registrar's
    assert answer.status == COVERED and answer.covered_by == ("garage_pass",)
    assert answer.matched == ({"module": "garage_pass", "pass": "p-1", "agreement": None},)
    assert answer.fee_minor is None, "a covered car has no fee, even with an entry cached"
    detail = answer.to_detail()
    assert "PASS-1" not in json.dumps(detail) and "PASS1" not in json.dumps(detail)
    assert detail["computed_from"]["day"] == "2026-06-10"
    assert detail["computed_from"]["clock"] == NEW_YORK
    # CONTROL: the same register, a different plate: not covered, and priced from its stay
    assert price_exit("OTHER-9", cache, now=NOW).status == NO_CACHED_ENTRY


def test_a_pass_that_does_not_stand_today_covers_nothing_through_the_whole_decision():
    """The state and the days are read where the exit reads them -- through
    `price_exit`, not only in `row_covers`: a revoked pass, a suspended one
    and one whose valid_to has passed each leave the car a transient."""
    for head in (
        {**GP_HEAD, "state": "revoked"},
        {**GP_HEAD, "state": "suspended"},
        {**GP_HEAD, "valid_to": "2026-06-09"},
    ):
        cache = a_cache(a_platform(gp=gp_register(gp_row("PASS-1"), passes=[head])))
        answer = price_exit("PASS-1", cache, now=NOW)
        assert answer.status == NO_CACHED_ENTRY and answer.covered_by == (), head
    # the control: the same row on a pass that stands is covered
    cache = a_cache(a_platform(gp=gp_register(gp_row("PASS-1"), passes=[GP_HEAD])))
    assert price_exit("PASS-1", cache, now=NOW).status == COVERED


def test_covered_from_a_monthly_register_in_the_garages_folded_form():
    cache = a_cache(
        a_platform(mb=mb_register({"identity_normalised": "mnth7", "agreement": "ag-1"})),
    )
    answer = price_exit("MNTH-7", cache, now=NOW)
    assert answer.status == COVERED and answer.covered_by == ("monthly_billing",)
    assert answer.matched[0]["agreement"] == "ag-1"
    # both modules: both named, in register order
    cache = a_cache(a_platform(gp=gp_register(gp_row("BOTH-1")),
                               mb=mb_register(
                                   {"identity_normalised": "both1", "agreement": "ag-1"})),
                               )
    assert price_exit("BOTH-1", cache, now=NOW).covered_by == ("garage_pass", "monthly_billing")


def test_a_cached_transient_is_priced_on_the_box_by_the_engine_from_the_cached_plans():
    cache = a_cache(a_platform(stays=[("s-7", "TRNS-7", ENTRY)]))
    answer = price_exit("TRNS-7", cache, now=NOW)
    assert answer.status == PRICED, answer
    assert answer.fee_minor == 900 and answer.currency == "USD"  # 2.5 h, ceil to 3 periods at 300
    assert answer.plan_version == "flat-lot-2026-01"
    assert answer.session_id == "s-7" and answer.entry_at == ENTRY
    assert answer.exit_at == datetime.fromtimestamp(NOW, UTC).isoformat()
    assert answer.space_class == "standard"
    assert isinstance(answer.breakdown, list) and answer.breakdown, "the engine's ledger travels"
    detail = answer.to_detail()
    assert detail["fee_minor"] == 900 and "TRNS" not in json.dumps(detail)


def test_the_lanes_number_and_the_platforms_are_one_computation():
    """The same request through the engine's HTTP service -- what the
    platform's close calls -- and through `price_exit`: the same fee, the same
    plan, the same ledger, because both are `run_quote`."""
    from http.client import HTTPConnection

    from rate_engine.service import make_server

    server = make_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cache = a_cache(a_platform(stays=[("s-7", "TRNS-7", ENTRY)]))
        local = price_exit("TRNS-7", cache, now=NOW)
        body = json.dumps({
            "plans": [PLAN], "currency": "USD", "space_class": "standard",
            "entry_at": ENTRY, "exit_at": local.exit_at,
        })
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        connection.request(
            "POST", "/v1/quote", body=body, headers={"content-type": "application/json"},
        )
        response = connection.getresponse()
        remote = json.loads(response.read())
        assert response.status == 200, remote
        assert (remote["fee_minor"], remote["plan_version"], remote["breakdown"]) == (
            local.fee_minor, local.plan_version, local.breakdown)
    finally:
        server.shutdown()
        server.server_close()


def test_a_transient_with_no_cached_entry_cannot_be_priced_here_and_says_so():
    cache = a_cache(a_platform(stays=[("s-1", "OTHER-1", ENTRY)]))
    answer = price_exit("NEW-CAR", cache, now=NOW)
    assert answer.status == NO_CACHED_ENTRY
    assert answer.fee_minor is None and answer.covered_by == ()
    assert answer.to_detail()["status"] == "no_cached_entry"
    # CONTROL: the moment its entry arrives on the fast cadence, it is priced
    platform = a_platform(stays=[("s-1", "OTHER-1", ENTRY)])
    cache = a_cache(platform)
    stay = platform.stay_opened("s-2", plate="NEW-CAR")
    stay["entry_at"] = ENTRY
    cache.apply_stay_changes([stay], "2", now=NOW)
    assert price_exit("NEW-CAR", cache, now=NOW).status == PRICED


def test_a_plan_that_cannot_price_the_stay_is_the_engines_refusal_verbatim():
    platform = a_platform(stays=[("s-7", "TRNS-7", ENTRY)])
    platform.rate_plans = [
        {**PLAN, "space_classes": ["compact"], "rules": [{**PLAN["rules"][0],
        "space_classes": ["compact"]}]},
    ]
    cache = a_cache(platform)
    answer = price_exit("TRNS-7", cache, now=NOW)
    assert answer.status == ENGINE_REFUSED, answer
    assert answer.refusal and answer.refusal.get("refused") is True
    assert answer.refusal["findings"], "the engine's findings travel"
    assert answer.fee_minor is None and answer.session_id == "s-7"


def test_stale_facts_are_said_and_not_decided_from():
    cache = a_cache(a_platform(gp=gp_register(gp_row("PASS-1"))), now=NOW - 7200)
    assert cache.is_stale(now=NOW)
    answer = price_exit("PASS-1", cache, now=NOW)
    assert answer.status == STALE_FACTS and answer.covered_by == ()


def test_more_than_one_open_stay_under_one_identity_prices_the_newest_and_says_so():
    cache = a_cache(
        a_platform(stays=[("s-old", "DUP-1", "2026-06-09T10:00:00+00:00"),
        ("s-new", "DUP-1", ENTRY)]),
    )
    answer = price_exit("DUP-1", cache, now=NOW)
    assert answer.status == PRICED and answer.session_id == "s-new" and answer.ambiguous_stays == 2
    assert answer.to_detail()["ambiguous_stays"] == 2


# ---------------------------------------------------------------------------
# through the lane: the decision carries it, the event records it
# ---------------------------------------------------------------------------


def lane(
    cache, plate, *, direction="exit", confidence=0.99, transport=None, identifier=None, clock=None
):
    identity = VehicleIdentity(plate=plate, plate_region="FL", make=None, model=None, color=None,
                               confidence=confidence)
    vend = RecordingVendOutput()
    controller = LaneController(
        a_config(direction=direction),
        loop=SimulatedLoopInput(arrivals=1),
        camera=CannedCameraFeed(),
        vend=vend,
        identifier=identifier or StubVehicleIdentifier([identity]),
        cache=cache,
        events=EventQueue(transport) if transport else EventQueue(),
        clock=clock or (lambda: NOW),
    )
    return controller, vend


def recorded(controller, kind):
    return [e for e in controller.events._log if e.kind == kind]


def test_an_exit_lane_attaches_the_local_decision_before_the_vend_and_records_it():
    cache = a_cache(a_platform(gp=gp_register(gp_row("PASS-1")), stays=[("s-7", "TRNS-7", ENTRY)]))
    controller, vend = lane(cache, "PASS-1")
    decision = controller.run_once()
    assert decision.should_vend and vend.vends
    assert decision.exit_pricing.status == COVERED
    event = recorded(controller, "decision")[0]
    assert event.detail["exit_pricing"]["status"] == "covered"
    assert event.detail["exit_pricing"]["covered_by"] == ["garage_pass"]
    assert "PASS" not in json.dumps(event.detail), "plate text reached an event detail"
    # a transient with a cached entry: priced, the fee on the record
    controller, vend = lane(cache, "TRNS-7")
    decision = controller.run_once()
    assert decision.should_vend and decision.exit_pricing.status == PRICED
    assert recorded(controller, "decision")[0].detail["exit_pricing"]["fee_minor"] == 900
    # and one the cache does not hold: the barrier still opens, the record says why
    controller, vend = lane(cache, "NEW-1")
    decision = controller.run_once()
    assert decision.should_vend and vend.vends, "an exit always works"
    assert decision.exit_pricing.status == NO_CACHED_ENTRY
    assert recorded(controller, "decision")[0].detail["exit_pricing"]["status"] == "no_cached_entry"


def test_an_entry_lane_and_a_fallback_carry_no_exit_pricing():
    cache = a_cache(a_platform(gp=gp_register(gp_row("PASS-1"))))
    controller, _ = lane(cache, "PASS-1", direction="entry")
    decision = controller.run_once()
    assert decision.should_vend and decision.exit_pricing is None
    assert "exit_pricing" not in recorded(controller, "decision")[0].detail
    controller, _ = lane(cache, "PASS-1", confidence=0.10)
    decision = controller.run_once()
    assert not decision.should_vend and decision.exit_pricing is None


def test_the_local_decision_travels_to_the_platform_on_the_decision_event():
    platform = a_platform(gp=gp_register(gp_row("PASS-1")))
    cache = a_cache(platform)
    controller, _ = lane(cache, "PASS-1", transport=PlatformTransport(platform))
    controller.run_once()
    delivered = [e for e in platform.events.values() if e["kind"] == "decision"]
    assert delivered and delivered[0]["detail"]["exit_pricing"]["status"] == "covered"


# ---------------------------------------------------------------------------
# 4.12: no platform call on the barrier's path, Vehicle ID live on loopback,
# timed against his one-second requirement
# ---------------------------------------------------------------------------


class StubReader:
    """An engine for the REAL Vehicle ID service: the contract's `Read`, from
    a plate this test chooses. The plate recogniser needs torch; the service
    around it does not, and the service is what the lane talks to."""

    def __init__(self, plate: str):
        from vehicle_id.contract import Engine

        self.plate = plate
        self.engine = Engine(name="stub-for-the-lane", version="0", weights_id=None)
        self.threshold = 0.5
        self.camera_faults: dict = {}

    def read(self, captures):
        from vehicle_id.contract import ANSWER, Identity, Read, new_read_id, utc_now

        return Read(
            read_id=new_read_id(),
            captured_at=captures[0].captured_at if captures else utc_now(),
            camera_id=captures[0].camera_id if captures else "unknown",
            identity=Identity(plate=self.plate),
            confidence=0.99,
            engine=self.engine,
            threshold_applied=self.threshold,
            outcome=ANSWER,
            captures_seen=len(captures),
        )


@pytest.fixture
def vehicle_id_on_loopback():
    from vehicle_id.service import VehicleIdService, make_server

    reader = StubReader("PASS-1")
    server = make_server(VehicleIdService(reader), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield reader, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


class ConnectionCounter:
    """Every `connect()` the process makes while armed, by destination."""

    def __init__(self):
        self.destinations: list[tuple] = []
        self._original = socket.socket.connect

    def __enter__(self):
        counter = self

        def counting_connect(sock, address):
            counter.destinations.append(
                tuple(address) if isinstance(address, tuple) else (address,),
            )
            return counter._original(sock, address)

        socket.socket.connect = counting_connect
        return self

    def __exit__(self, *_):
        socket.socket.connect = self._original
        return False


#: His requirement, as the bound this test holds the Mac to. A MAC NUMBER IS A
#: FLOOR FOR A JETSON, NOT A MEASUREMENT OF ONE: no Jetson exists, none was
#: exercised, and what this proves is the shape of the path -- no platform on
#: it -- and that the shape fits inside the second on this machine.
ONE_SECOND = 1.0


class TimedVend(RecordingVendOutput):
    """The vend relay, marking THE MOMENT THE BARRIER WAS TOLD TO MOVE: the
    clock and how many connections the turn had made by then. Everything
    before this mark is the barrier's path; everything after it is the
    record leaving the box, which is best effort and is allowed to reach the
    platform (`handle_arrival`: "after the barrier has already been told
    what to do. Nothing above this line waits on the network")."""

    def __init__(self, counter: ConnectionCounter):
        super().__init__()
        self.counter = counter
        self.at: float | None = None
        self.connections_by_then: int | None = None

    def vend(self, reason: str) -> None:
        self.at = time.perf_counter()
        self.connections_by_then = len(self.counter.destinations)
        super().vend(reason)


def test_no_platform_call_on_the_barriers_path_with_vehicle_id_live_on_loopback(
    vehicle_id_on_loopback,
):
    reader, port = vehicle_id_on_loopback
    dead_platform = PlatformClient("http://127.0.0.1:9", "tok", timeout=0.5)
    cache = a_cache(a_platform(gp=gp_register(gp_row("PASS-1")), stays=[("s-7", "TRNS-7", ENTRY)]))
    allowed = ("127.0.0.1", port)
    measured = {}
    for plate, expected in (("PASS-1", COVERED), ("TRNS-7", PRICED), ("NEW-1", NO_CACHED_ENTRY)):
        reader.plate = plate
        with ConnectionCounter() as counter:
            vend = TimedVend(counter)
            identity = VehicleIdentity(plate=plate, plate_region="FL", make=None, model=None,
                                       color=None, confidence=0.99)
            controller = LaneController(
                a_config(), loop=SimulatedLoopInput(arrivals=1), camera=CannedCameraFeed(),
                vend=vend,
                identifier=VehicleIdClient(f"http://127.0.0.1:{port}"), cache=cache,
                events=EventQueue(PlatformTransport(dead_platform)), clock=lambda: NOW,
            )
            del identity  # the plate comes back from the REAL service, not from a stub
            started = time.perf_counter()
            decision = controller.run_once()
            finished = time.perf_counter()
        assert decision is not None and decision.should_vend and vend.at is not None, plate
        assert decision.exit_pricing.status == expected, (plate, decision.exit_pricing)
        # THE SENTENCE, MEASURED: every connection made BEFORE the barrier was
        # told to move went to Vehicle ID on loopback and nowhere else. The
        # platform client was in the lane and would have been counted at once
        # had anything on the path asked it.
        before_vend = counter.destinations[: vend.connections_by_then]
        assert before_vend, "the premise: the identifier really went over a socket"
        others = [d for d in before_vend if d[:2] != allowed]
        assert others == [], f"the barrier's path opened a socket to {others}"
        to_vend = vend.at - started
        measured[plate] = (to_vend, finished - vend.at)
        assert to_vend < ONE_SECOND, (
            f"{plate}: {to_vend * 1000:.0f} ms to the vend on this Mac, over the second"
        )
        # AND THE HALF THAT IS ALLOWED TO: after the vend the record leaves the
        # box, and with the platform dead that is the outbox trying the dead
        # port. Counted here so the number is on the record rather than in a
        # sentence: this is the time the LANE THREAD is held after a vend on
        # an outage, which is the next car's problem (reported, not this
        # round's).
        after_vend = counter.destinations[vend.connections_by_then :]
        assert any(d[:2] == ("127.0.0.1", 9) for d in after_vend), (
            "the control: the outbox did reach for the dead platform after the vend, and the "
            "counter saw it -- so it would have seen one before the vend too")
    print("\nbarrier path, arrival to vend, Vehicle ID over loopback (a Mac FLOOR, not a Jetson):")
    for plate, (to_vend, after) in measured.items():
        print(
            f"  {plate:8} {to_vend * 1000:7.1f} ms to the vend;  {after * 1000:7.1f} ms after it, "
            "the outbox on a dead platform"
        )
