"""The exit case: the gate opens only when the exit is settled.

Covered, or a fee of zero, the lane settles on its own copy and the barrier
opens with nothing on the network. Every other car at an exit is a case on
the platform (`exit_case`): opened with the car's ID fields and its pictures,
moved on at the lane's idle turns while the car is on the arming loop, and the
barrier opens only when the platform says the case is settled -- paid at the
lane's reader, priced on the driver's word, or let out by a person.
"""

from __future__ import annotations

import copy
import json

from fake_platform import FakePlatform
from lane_controller import EventQueue, LaneController
from lane_controller.decision import Fallback, Outcome
from lane_controller.exit_case import EXIT_POLL_SECONDS, fold, id_fields
from lane_controller.exit_pricing import COVERED, NO_CACHED_ENTRY, PRICED
from lane_controller.interfaces import PlateCharacters, VehicleIdentity
from lane_controller.platform_client import PlatformRejected
from lane_controller.simulated import (
    CannedCameraFeed,
    RecordingVendOutput,
    SimulatedExitDisplay,
    SimulatedLoopInput,
    StubVehicleIdentifier,
)
from lane_controller.sync import PlatformTransport
from test_exit_decision import (
    ENTRY,
    NOW,
    PLAN,
    a_cache,
    a_config,
    a_platform,
    gp_register,
    gp_row,
)

PAYMENT = {"settled": False, "needs": "payment", "fee": 900, "payment": None}
PAID = {"settled": True, "why": "paid"}

#: The flat plan with its first hour free: a car out inside it owes nothing.
FIRST_HOUR_FREE = copy.deepcopy(PLAN)
FIRST_HOUR_FREE["plan_version"] = "flat-lot-first-hour-free"
FIRST_HOUR_FREE["rules"][0]["first_period_minor"] = 0


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float = EXIT_POLL_SECONDS + 1) -> None:
        self.now += seconds


def an_exit(plate="TRNS-7", *, answer=None, confidence=0.99, display=None, stays=None,
            identity=None, register=None, plan=None):
    """An exit lane, its rules from one platform and its cases, pictures and
    closes to another (`exits`) answering `answer`."""
    rules = a_platform(
        gp=register if register is not None else gp_register(gp_row("PASS-1")),
        stays=stays if stays is not None else [("s-7", "TRNS-7", ENTRY)],
    )
    if plan is not None:
        rules.rate_plans = [plan]
    exits = FakePlatform()
    exits.reject_close_without_open = False
    if answer is not None:
        exits.exit_answer = dict(answer)
    order: list[tuple] = []
    for name in ("post_picture", "open_exit"):
        original = getattr(exits, name)

        def recorded(*args, _name=name, _original=original, **kwargs):
            order.append((_name, kwargs.get("event_id")))
            return _original(*args, **kwargs)

        setattr(exits, name, recorded)
    clock = Clock()
    vend = RecordingVendOutput()
    controller = LaneController(
        a_config(),
        loop=SimulatedLoopInput(arrivals=1),
        camera=CannedCameraFeed(),
        vend=vend,
        identifier=StubVehicleIdentifier([identity or VehicleIdentity(
            plate=plate, plate_region="FL", make=None, model=None, color=None,
            confidence=confidence)]),
        cache=a_cache(rules),
        events=EventQueue(PlatformTransport(exits)),
        clock=clock,
        exits=exits,
        exit_display=display,
    )
    return controller, vend, exits, clock, order


def logged(controller, kind):
    """The events of `kind` the lane delivered to its platform, in order."""
    controller.events.flush()
    return [e for e in controller.exits.events.values() if e["kind"] == kind]


def test_a_covered_car_leaves_with_no_call_to_the_platform():
    controller, vend, exits, _, _ = an_exit("PASS-1")
    decision = controller.run_once()
    assert decision.outcome is Outcome.ALLOW and vend.vend_count == 1
    assert decision.exit_pricing.status == COVERED
    assert exits.exit_requests == [], "a covered car's exit asked the platform"


def test_a_fee_of_zero_leaves_with_no_call_to_the_platform():
    controller, vend, exits, _, _ = an_exit(
        stays=[("s-0", "TRNS-7", "2026-06-10T17:30:00+00:00")], plan=FIRST_HOUR_FREE)
    decision = controller.run_once()
    assert decision.exit_pricing.status == PRICED and decision.exit_pricing.fee_minor == 0
    assert decision.outcome is Outcome.ALLOW and vend.vend_count == 1
    assert exits.exit_requests == []


def test_a_priced_car_waits_on_its_case_and_leaves_when_it_is_paid():
    display = SimulatedExitDisplay(receipt_phone="+15555550123")
    controller, vend, exits, clock, order = an_exit(answer=PAYMENT, display=display)

    decision = controller.run_once()

    # THE BARRIER STAYS DOWN, and the case is open on the platform naming the
    # stay the lane priced, with the car's ID and none of its plate text.
    assert decision.fallback is Fallback.EXIT_NOT_SETTLED and vend.vend_count == 0
    assert decision.exit_pricing.status == PRICED and decision.exit_pricing.fee_minor == 900
    opened = exits.exit_requests[0]
    assert opened[0] == "open" and opened[2] == "s-7"
    assert opened[3] == {"plate_characters": {"state": "read", "rule": "opvid-plate-chars/1",
                                              "pattern": fold("TRNS-7")}}
    assert logged(controller, "decision")[0]["detail"]["exit_id"] == "exit-1"
    # The pictures went before the case, under the case's own event.
    assert [name for name, _ in order] == ["post_picture", "open_exit"]
    assert order[0][1] == order[1][1] == opened[1]
    # The fee to the lane's reader, once.
    assert exits.exit_requests[1:] == [("pay", "exit-1")]
    assert display.shown[-1] == {"needs": "payment", "fee": 900}

    # Asked again no faster than the platform reads Stripe.
    clock.tick(EXIT_POLL_SECONDS / 2)
    controller.observe_exit_case()
    assert exits.exit_requests[-1] == ("pay", "exit-1")
    clock.tick()
    controller.observe_exit_case()
    assert exits.exit_requests[-1] == ("get", "exit-1"), "no decline: the fee is not sent again"

    # A DECLINE: the fee goes to the reader again, once for that decline.
    exits.exit_answer = {**PAYMENT, "payment": {"status": "open", "last_failure": "card_declined"}}
    clock.tick()
    controller.observe_exit_case()
    clock.tick()
    controller.observe_exit_case()
    assert [r for r in exits.exit_requests if r[0] == "pay"] == [("pay", "exit-1")] * 2
    assert vend.vend_count == 0

    # PAID: the barrier opens, the vend and the close name the case, and the
    # receipt goes to the phone the driver gave.
    exits.exit_answer = dict(PAID)
    clock.tick()
    controller.observe_exit_case()
    assert vend.vend_count == 1
    assert logged(controller, "vended")[-1]["detail"]["exit_id"] == "exit-1"
    assert ("receipt", "exit-1", "+15555550123") in exits.exit_requests
    controller.events.flush()
    assert exits.closed[-1]["exit_id"] == "exit-1"
    assert display.shown[-1] is None
    # and nothing more happens for it
    clock.tick()
    controller.observe_exit_case()
    assert vend.vend_count == 1


def test_a_car_with_no_stay_here_gives_its_word_and_leaves_when_it_is_priced_and_paid():
    display = SimulatedExitDisplay()
    controller, vend, exits, clock, _ = an_exit(
        "NEW-1", answer={"settled": False, "needs": "driver_word"}, display=display)

    decision = controller.run_once()
    assert decision.exit_pricing.status == NO_CACHED_ENTRY and vend.vend_count == 0
    assert exits.exit_requests[0][2] is None, "a stay the lane did not price is not named"
    assert display.asked_entry == 1, "the display was asked for the entry time"

    # A time the platform does not take: refused by name, and asked again.
    refusing = exits.exit_word
    exits.exit_word = lambda exit_id, entry_at: (_ for _ in ()).throw(
        PlatformRejected(400, "{}", "entry_in_future"))
    display.give_entry_time("2026-06-11T09:00:00-04:00")
    clock.tick()
    controller.observe_exit_case()
    assert display.shown[-1]["refused"] == "entry_in_future"
    assert vend.vend_count == 0

    exits.exit_word = refusing
    display.give_entry_time("2026-06-10T09:00:00-04:00")
    clock.tick()
    controller.observe_exit_case()
    assert ("word", "exit-1", "2026-06-10T09:00:00-04:00") in exits.exit_requests

    exits.exit_answer = dict(PAID)
    clock.tick()
    controller.observe_exit_case()
    assert vend.vend_count == 1


def test_a_plate_the_lane_cannot_stand_behind_sends_no_characters():
    identity = VehicleIdentity(plate="TRNS-7", confidence=0.40, color_code="WHT", size="large")
    controller, vend, exits, _, _ = an_exit(identity=identity, answer=PAYMENT)
    decision = controller.run_once()
    assert decision.fallback is Fallback.EXIT_NOT_SETTLED and vend.vend_count == 0
    assert exits.exit_requests[0][3] == {"color_code": "WHT", "size": "large"}


def test_the_identity_services_own_characters_go_as_it_stated_them():
    characters = PlateCharacters(state="partial", rule="opvid-plate-chars/1", pattern="TR?57")
    identity = VehicleIdentity(plate="TRNS-7", confidence=0.99, plate_characters=characters)
    assert id_fields(identity, plate_confident=True)["plate_characters"] == {
        "state": "partial", "rule": "opvid-plate-chars/1", "pattern": "TR?57"}


def test_no_request_and_no_event_carries_the_plate_text():
    controller, _, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.run_once()
    clock.tick()
    controller.observe_exit_case()
    assert "TRNS" not in json.dumps(exits.exit_requests)
    for kind in ("decision", "fallback_needs_human", "vended"):
        for event in logged(controller, kind):
            assert "TRNS" not in json.dumps(event["detail"], default=str), kind


def test_a_case_that_needs_a_person_says_so_once():
    controller, vend, _, clock, _ = an_exit(answer={"settled": False, "needs": "person"})
    controller.run_once()
    for _ in range(3):
        clock.tick()
        controller.observe_exit_case()
    asked = logged(controller, "fallback_needs_human")
    assert [e["detail"]["needs"] for e in asked] == ["person"]
    assert asked[0]["detail"]["exit_id"] == "exit-1" and vend.vend_count == 0


def test_a_platform_that_cannot_be_reached_keeps_the_barrier_down():
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    exits.online = False
    decision = controller.run_once()
    assert decision.fallback is Fallback.EXIT_NOT_SETTLED and vend.vend_count == 0
    held = [e for e in controller.events._queue if e.kind == "fallback_needs_human"]
    assert [e.detail["needs"] for e in held] == ["unanswered"]
    # THE CONTROL: back up and paid, the same case is opened and the car leaves.
    exits.online = True
    exits.exit_answer = dict(PAID)
    clock.tick()
    controller.observe_exit_case()
    assert vend.vend_count == 1


def test_a_car_that_leaves_the_loop_unsettled_leaves_its_case_to_a_person():
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.run_once()
    controller.loop.clear()
    clock.tick()
    controller.observe_exit_case()
    left = logged(controller, "fallback_needs_human")[-1]
    assert left["detail"]["needs"] == "left" and left["detail"]["exit_id"] == "exit-1"
    # settled afterwards: nothing opens here for a car that has gone
    exits.exit_answer = dict(PAID)
    clock.tick()
    controller.observe_exit_case()
    assert vend.vend_count == 0


def test_a_person_at_the_lane_takes_the_case_and_the_barrier_moves_once():
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    decision = controller.run_once()
    controller.complete_vend(decision.identity, authorised_by="attendant",
                             assisted={"authorised_by": "attendant"})
    exits.exit_answer = dict(PAID)
    clock.tick()
    controller.observe_exit_case()
    assert vend.vend_count == 1, "the case opened the barrier a person had already opened"


def test_with_no_platform_wired_an_unsettled_exit_stays_down():
    """The lane is never built to open for a car it could not settle because
    nobody wired the platform in."""
    controller, vend, _, _, _ = an_exit(answer=PAID)
    controller.exits = None
    decision = controller.run_once()
    assert decision.fallback is Fallback.EXIT_NOT_SETTLED and vend.vend_count == 0
