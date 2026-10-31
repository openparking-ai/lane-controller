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
import io
import json
import logging
import threading
import time
import urllib.error

import pytest

from fake_platform import FakePlatform
from lane_controller import EventQueue, LaneController
from lane_controller import exit_case as exit_case_module
from lane_controller.contract import VendAuthority, VendRefusal
from lane_controller.decision import Fallback, Outcome
from lane_controller.exit_case import (
    EXIT_FLICKER_SECONDS,
    EXIT_LEFT_SECONDS,
    EXIT_PAY_ATTEMPTS,
    EXIT_POLL_SECONDS,
    EXIT_READER_BUSY,
    EXIT_RESEND_SECONDS,
    EXIT_SAMPLE_SECONDS,
    fold,
    id_fields,
)
from lane_controller.exit_pricing import COVERED, NO_CACHED_ENTRY, PRICED
from lane_controller.interfaces import PlateCharacters, VehicleIdentity
from lane_controller.platform_client import PlatformClient, PlatformRejected
from lane_controller.reader import StripeCartScreen
from lane_controller.service import LaneService
from lane_controller.simulated import (
    CannedCameraFeed,
    RecordingVendOutput,
    SimulatedExitDisplay,
    SimulatedLoopInput,
    StubVehicleIdentifier,
)
from lane_controller.sync import PlatformTransport
from lane_controller.vend import AssistedVend
from lane_controller.vend import parse as parse_vend
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

#: The fee as the platform answers it on a case (`exits.presentFee`).
FEE = {"outcome": "transient", "covered_by": [], "fee_minor": 900, "subtotal_minor": 900,
       "currency": "USD", "breakdown": [{"kind": "base", "delta_minor": 900, "text": "3 h"}],
       "refused": False}
PAYMENT = {"settled": False, "needs": "payment", "fee": FEE, "payment": None}
PAID = {"settled": True, "why": "paid"}
DECLINED = "card_declined:insufficient_funds"

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
    assert display.shown[-1] == {"needs": "payment", "fee": FEE}

    # Asked again no faster than the platform reads Stripe.
    clock.tick(EXIT_POLL_SECONDS / 2)
    controller.observe_exit_case()
    assert exits.exit_requests[-1] == ("pay", "exit-1")
    clock.tick()
    controller.observe_exit_case()
    assert exits.exit_requests[-1] == ("get", "exit-1"), "no decline: the fee is not sent again"

    # A DECLINE: the fee goes to the reader again at once, and only once for
    # that decline until the resend interval has passed.
    exits.exit_answer = {**PAYMENT, "payment": {"status": "open", "last_failure": DECLINED}}
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
    clock.tick(EXIT_LEFT_SECONDS)
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


# ---------------------------------------------------------------------------
# The reader is the platform's once the case has handed its fee to it.
# ---------------------------------------------------------------------------


def a_reader(controller, exits):
    """The lane's own Stripe reader screen, and one list of what reached the
    reader in order: the lane's carts and clears, and the platform's /pay."""
    calls: list[str] = []
    controller.reader = StripeCartScreen(
        "tmr_lane", lambda path, form: calls.append(path.rsplit("/", 1)[-1]))
    pay, cancel = exits.exit_pay, exits.exit_pay_cancel

    def paying(exit_id):
        calls.append("platform:pay")
        return pay(exit_id)

    def cancelling(exit_id):
        calls.append("platform:pay_cancel")
        return cancel(exit_id)

    exits.exit_pay, exits.exit_pay_cancel = paying, cancelling
    return calls


def test_a_case_whose_fee_is_on_the_reader_never_has_the_reader_driven_after_it():
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    calls = a_reader(controller, exits)
    controller.run_once()
    # The platform's PaymentIntent went to the reader, and nothing of the
    # lane's followed it: no cart, no clear.
    assert calls == ["platform:pay"]
    exits.exit_answer = dict(PAID)
    clock.tick()
    controller.observe_exit_case()
    assert vend.vend_count == 1
    controller.events.flush()
    assert exits.closed[-1]["exit_id"] == "exit-1"
    assert calls == ["platform:pay"], "the lane drove the reader after the intent was on it"


def test_the_control_a_covered_car_still_has_the_reader_driven():
    controller, vend, exits, _, _ = an_exit("PASS-1")
    calls = a_reader(controller, exits)
    controller.run_once()
    assert vend.vend_count == 1 and calls and "platform:pay" not in calls


# ---------------------------------------------------------------------------
# A case the lane lets go of has its payment taken off the reader.
# ---------------------------------------------------------------------------


def test_a_car_that_leaves_has_its_payment_taken_off_the_reader():
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    calls = a_reader(controller, exits)
    controller.run_once()
    controller.loop.clear()
    controller.observe_exit_case()
    clock.tick(EXIT_LEFT_SECONDS)
    controller.observe_exit_case()
    assert calls == ["platform:pay", "platform:pay_cancel"]
    assert ("pay_cancel", "exit-1") in exits.exit_requests and vend.vend_count == 0


def test_a_new_car_takes_the_last_cars_payment_off_the_reader_before_its_own_goes_on():
    rules = a_platform(gp=gp_register(gp_row("PASS-1")),
                       stays=[("s-7", "TRNS-7", ENTRY), ("s-8", "TRNS-8", ENTRY)])
    exits = FakePlatform()
    exits.reject_close_without_open = False
    exits.exit_answer = dict(PAYMENT)
    controller = LaneController(
        a_config(), loop=SimulatedLoopInput(arrivals=2), camera=CannedCameraFeed(),
        vend=RecordingVendOutput(),
        identifier=StubVehicleIdentifier([
            VehicleIdentity(plate=p, plate_region="FL", make=None, model=None, color=None,
                            confidence=0.99)
            for p in ("TRNS-7", "TRNS-8")]),
        cache=a_cache(rules), events=EventQueue(PlatformTransport(exits)), clock=Clock(),
        exits=exits)
    controller.run_once()
    controller.run_once()
    asked = [r[:2] for r in exits.exit_requests if r[0] in ("pay", "pay_cancel")]
    assert asked == [("pay", "exit-1"), ("pay_cancel", "exit-1"), ("pay", "exit-2")]


def test_a_case_whose_fee_never_reached_the_reader_asks_nothing_to_be_taken_off():
    controller, _, exits, clock, _ = an_exit(answer={"settled": False, "needs": "person"})
    controller.run_once()
    controller.loop.clear()
    controller.observe_exit_case()
    clock.tick(EXIT_LEFT_SECONDS)
    controller.observe_exit_case()
    assert not [r for r in exits.exit_requests if r[0] == "pay_cancel"]


def person_opens(service, controller, key="KEY-EXIT-1"):
    """`POST /v1/lane/vend` as the route takes it, a person's word."""
    body = {"authorised_by": VendAuthority.HUMAN_OPEN_NOW.value,
            "identity": {"kind": "ticket", "ticket_ref": "TKT-4RS9WQ2M"},
            "decision_at": controller.last_decision_at}
    answer = service.assisted.complete(parse_vend(body, key))
    if service.assisted.settling is not None:
        service.assisted.settling.join(timeout=5)
    return answer


def test_a_person_who_opens_names_the_case_on_the_close_and_takes_its_payment_off_the_reader():
    controller, vend, exits, _, _ = an_exit(answer=PAYMENT)
    service = LaneService(controller)
    controller.run_once()
    status, answer = person_opens(service, controller)
    assert status == 202 and vend.vend_count == 1, answer
    assert ("pay_cancel", "exit-1") in exits.exit_requests
    controller.events.flush()
    assert exits.closed[-1]["exit_id"] == "exit-1", "the person's close did not name the case"


def test_a_case_that_settles_while_a_persons_vend_is_judged_moves_the_barrier_once():
    """The route checks `completed` without the lane's exit lock; the case
    settles and opens between that check and the vend. The vend is refused
    `already_completed` and the barrier moves once."""
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    service = LaneService(controller)
    controller.run_once()
    judged = AssistedVend._refuse

    def settles_in_between(request):
        refusal = judged(service.assisted, request)
        if refusal is None:
            exits.exit_answer = dict(PAID)
            clock.tick()
            controller.observe_exit_case()
        return refusal

    service.assisted._refuse = settles_in_between
    status, answer = person_opens(service, controller)
    assert status == 409 and answer["code"] == VendRefusal.ALREADY_COMPLETED.value
    assert vend.vend_count == 1


def test_a_person_who_opens_while_the_case_is_being_asked_is_the_only_one_who_opens():
    """The other order: a person's vend lands while the lane's turn is asking
    the platform, and the answer is settled. The turn opens nothing."""
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    decision = controller.run_once()
    step = type(controller)._step

    def person_first(case):
        controller.complete_vend(decision.identity, authorised_by="attendant",
                                 assisted={"authorised_by": "attendant"})
        exits.exit_answer = dict(PAID)
        return step(controller, case)

    controller._step = person_first
    clock.tick()
    controller.observe_exit_case()
    assert vend.vend_count == 1, "the case opened the barrier a person had already opened"


def test_a_person_who_opens_while_the_lane_reads_the_loop_is_the_only_one_who_opens(monkeypatch):
    """A person's vend lands while the lane is stalled in its last read of
    the loop, after the platform answered paid and before it decides. The
    turn opens nothing, and the person's car is not told "left"."""
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    decision = controller.run_once()
    exits.exit_answer = dict(PAID)
    reading, released = threading.Event(), threading.Event()
    deciding = {"now": False}
    read = controller.loop.is_occupied

    def stalls_as_the_lane_decides():
        if deciding["now"] and threading.current_thread() is threading.main_thread():
            deciding["now"] = False
            reading.set()
            assert released.wait(5), "the person's vend never finished"
        return read()

    controller.loop.is_occupied = stalls_as_the_lane_decides
    stop = exit_case_module.LoopWatch.stop

    def stop_then_decide(self):
        stop(self)
        deciding["now"] = True

    monkeypatch.setattr(exit_case_module.LoopWatch, "stop", stop_then_decide)

    def person():
        assert reading.wait(5), "the lane never read the loop to decide"
        controller.complete_vend(decision.identity, authorised_by="attendant",
                                 assisted={"authorised_by": "attendant"})
        released.set()

    attendant = threading.Thread(target=person)
    attendant.start()
    clock.tick()
    controller.observe_exit_case()
    attendant.join(5)
    assert released.is_set(), "the person's vend did not land during the lane's read"
    assert vend.vend_count == 1, "the case opened the barrier a person had already opened"
    assert not told(controller, "left")


# ---------------------------------------------------------------------------
# The fee is offered again while it waits for a card, and attempts are counted.
# ---------------------------------------------------------------------------


def pays(exits):
    return [r for r in exits.exit_requests if r[0] == "pay"]


def told(controller, need):
    return [e for e in logged(controller, "fallback_needs_human") if e["detail"]["needs"] == need]


def test_two_identical_declines_are_offered_again_and_a_person_is_told_after_the_attempts():
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.run_once()
    assert len(pays(exits)) == 1
    # The first card: declined. Offered again at once.
    exits.exit_answer = {**PAYMENT, "payment": {"status": "open", "last_failure": DECLINED}}
    clock.tick()
    controller.observe_exit_case()
    assert len(pays(exits)) == 2
    # The second card: declined the same way. The answer is the same as
    # before, and the fee is still offered again once the interval passes.
    clock.tick(EXIT_RESEND_SECONDS)
    controller.observe_exit_case()
    assert len(pays(exits)) == EXIT_PAY_ATTEMPTS == 3
    assert [e["detail"]["attempts"] for e in told(controller, "payment_attempts")] == [3]
    # Said once; the fee is still offered; the barrier stays down.
    clock.tick(EXIT_RESEND_SECONDS)
    controller.observe_exit_case()
    assert len(pays(exits)) == 4 and len(told(controller, "payment_attempts")) == 1
    assert vend.vend_count == 0


def test_a_driver_who_never_taps_is_offered_the_fee_again_when_the_reader_is_idle():
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    exits.exit_answer = {**PAYMENT, "payment": {"status": "open", "last_failure": None}}
    controller.run_once()
    assert len(pays(exits)) == 1
    # Still on the reader: the platform answers reader_busy. Not an attempt,
    # and not a refusal a person is told about.
    exits.exit_pay_refusal = "reader_busy"
    clock.tick(EXIT_RESEND_SECONDS)
    controller.observe_exit_case()
    assert len(pays(exits)) == 2
    # The reader timed out with no tap and went idle: no failure on the
    # intent, and the fee goes back on the reader.
    exits.exit_pay_refusal = None
    clock.tick(EXIT_RESEND_SECONDS)
    controller.observe_exit_case()
    assert len(pays(exits)) == 3
    assert controller._exit_case.attempts == 2
    assert not told(controller, "refused") and vend.vend_count == 0


# ---------------------------------------------------------------------------
# What only the platform's settled answer for THIS case can open.
# ---------------------------------------------------------------------------


def test_a_platform_refusal_keeps_the_barrier_down():
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.run_once()
    exits.get_exit = lambda exit_id: (_ for _ in ()).throw(
        PlatformRejected(409, "{}", "exit_on_another_lane"))
    clock.tick()
    controller.observe_exit_case()
    assert vend.vend_count == 0
    assert [e["detail"]["refused"] for e in told(controller, "refused")] == [
        "exit_on_another_lane"]


def test_a_platform_refusal_of_the_open_keeps_the_barrier_down():
    controller, vend, exits, _, _ = an_exit(answer=PAID)
    exits.open_exit = lambda **kwargs: (_ for _ in ()).throw(
        PlatformRejected(400, "{}", "bad_identity"))
    decision = controller.run_once()
    assert decision.fallback is Fallback.EXIT_NOT_SETTLED and vend.vend_count == 0


def test_settled_is_only_settled_when_it_is_true():
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.run_once()
    for almost in (1, "true", "yes", [1]):
        exits.exit_answer = {"settled": almost, "why": "paid"}
        clock.tick()
        controller.observe_exit_case()
    assert vend.vend_count == 0


def test_a_settled_answer_for_another_case_opens_nothing():
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.run_once()
    exits.get_exit = lambda exit_id: {"id": "exit-9", "settled": True, "why": "paid"}
    clock.tick()
    controller.observe_exit_case()
    assert vend.vend_count == 0
    assert [e["detail"]["refused"] for e in told(controller, "refused")] == ["another_exit_case"]


# ---------------------------------------------------------------------------
# The driver's entry time carries its offset.
# ---------------------------------------------------------------------------


def test_an_entry_time_without_an_offset_is_refused_at_the_lane_and_never_sent():
    display = SimulatedExitDisplay("2026-06-10T09:00:00")
    controller, vend, exits, clock, _ = an_exit(
        "NEW-1", answer={"settled": False, "needs": "driver_word"}, display=display)
    controller.run_once()
    assert not [r for r in exits.exit_requests if r[0] == "word"]
    assert display.shown[-1]["refused"] == "entry_time_without_offset"
    # The control: the same time with its offset goes, and so does UTC's Z.
    display.give_entry_time("2026-06-10T09:00:00Z")
    clock.tick()
    controller.observe_exit_case()
    assert ("word", "exit-1", "2026-06-10T09:00:00Z") in exits.exit_requests


# ---------------------------------------------------------------------------
# A paid car waits on nothing it does not have to.
# ---------------------------------------------------------------------------


def in_order(vend, exits):
    order: list[str] = []
    pulse, receipt = vend.vend, exits.exit_receipt

    def vending(reason):
        order.append("vend")
        return pulse(reason)

    def receipting(exit_id, phone):
        order.append("receipt")
        return receipt(exit_id, phone)

    vend.vend, exits.exit_receipt = vending, receipting
    return order


def test_the_barrier_opens_before_the_receipt_is_sent():
    display = SimulatedExitDisplay(receipt_phone="+15555550123")
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT, display=display)
    order = in_order(vend, exits)
    controller.run_once()
    exits.exit_answer = dict(PAID)
    clock.tick()
    controller.observe_exit_case()
    assert order == ["vend", "receipt"]


def test_the_barrier_opens_before_the_receipt_on_a_case_settled_at_once():
    display = SimulatedExitDisplay(receipt_phone="+15555550123")
    controller, vend, exits, _, _ = an_exit(answer=PAID, display=display)
    order = in_order(vend, exits)
    controller.run_once()
    assert order == ["vend", "receipt"]


def test_a_loop_that_flickers_clear_does_not_drop_a_paid_cars_case():
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.run_once()
    exits.exit_answer = dict(PAID)
    controller.loop.clear()
    clock.tick()
    controller.observe_exit_case()
    assert vend.vend_count == 0, "nothing opens for a car the loop does not see"
    # One clear sample, and the next one a turn later reads the car again.
    clock.tick(EXIT_SAMPLE_SECONDS)
    controller.loop._occupied = True
    controller.observe_exit_case()
    assert vend.vend_count == 1 and not told(controller, "left")


def test_a_car_that_rolls_on_unarmed_after_the_paid_car_backed_off_is_not_let_out():
    """Car A's case is paid; A backs off the loop and car B rolls onto it
    inside the two seconds without arming. A's case must not open for B."""
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.run_once()
    exits.exit_answer = dict(PAID)
    controller.loop.clear()
    controller.observe_exit_case()
    clock.tick(1.0)
    controller.observe_exit_case()
    controller.loop._occupied = True
    clock.tick(0.9)
    controller.observe_exit_case()
    assert vend.vend_count == 0, "a settled case opened for a car the lane did not read"
    assert [e["detail"]["exit_id"] for e in told(controller, "left")] == ["exit-1"]
    assert ("pay_cancel", "exit-1") in exits.exit_requests
    clock.tick()
    controller.observe_exit_case()
    assert vend.vend_count == 0


def test_a_clear_just_past_a_flicker_ends_the_cases_right_to_open():
    """Half a second, as the contract states it: not the constant, which a
    change could move with the test."""
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.run_once()
    exits.exit_answer = dict(PAID)
    controller.loop.clear()
    controller.observe_exit_case()
    clock.tick(0.5)
    controller.loop._occupied = True
    controller.observe_exit_case()
    assert vend.vend_count == 0 and told(controller, "left")


def slow_answer(controller, exits, clock, *, backs_off=False, swapped=False):
    """`get_exit` taking three seconds and answering paid; the car backs off
    the loop while it is asked, or is swapped for another in that time."""
    asked = exits.get_exit

    def slow(exit_id):
        if backs_off or swapped:
            controller.loop.clear()
        if swapped:
            time.sleep(0.1)
            controller.loop._occupied = True
        clock.tick(3.0)
        exits.exit_answer = dict(PAID)
        return asked(exit_id)

    exits.get_exit = slow


def test_a_car_that_backs_off_during_a_slow_platform_call_is_not_let_out():
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.run_once()
    slow_answer(controller, exits, clock, backs_off=True)
    clock.tick()
    controller.observe_exit_case()
    assert vend.vend_count == 0, "the barrier opened with the arming loop clear"
    assert [e["detail"]["exit_id"] for e in told(controller, "left")] == ["exit-1"]
    assert ("pay_cancel", "exit-1") in exits.exit_requests


def test_the_control_a_paid_car_that_stayed_put_through_a_slow_call_gets_out():
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.run_once()
    slow_answer(controller, exits, clock)
    clock.tick()
    controller.observe_exit_case()
    assert vend.vend_count == 1 and not told(controller, "left")


def test_a_car_swapped_on_the_loop_during_a_slow_call_is_not_let_out(monkeypatch):
    monkeypatch.setattr(exit_case_module, "EXIT_SAMPLE_SECONDS", 0.005)
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.run_once()
    slow_answer(controller, exits, clock, swapped=True)
    clock.tick()
    controller.observe_exit_case()
    assert vend.vend_count == 0, "a case opened for the car swapped on while it was asked"
    assert told(controller, "left")


def watchers():
    return [t for t in threading.enumerate() if t.name == "exit-loop-watch" and t.is_alive()]


def lane_reads(controller):
    """The loop as the lane's own thread reads it, with `flicker` clear
    readings to come on that thread only."""
    read = controller.loop.is_occupied
    state = {"flicker": 0}

    def reading():
        if state["flicker"] and threading.current_thread() is threading.main_thread():
            state["flicker"] -= 1
            return False
        return read()

    controller.loop.is_occupied = reading
    return state


def test_a_loop_read_that_fails_on_the_watcher_opens_nothing(monkeypatch):
    monkeypatch.setattr(exit_case_module, "EXIT_SAMPLE_SECONDS", 0.005)
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.run_once()
    read = controller.loop.is_occupied

    def fails_on_the_watcher():
        if threading.current_thread().name == "exit-loop-watch":
            raise OSError("loop driver")
        return read()

    controller.loop.is_occupied = fails_on_the_watcher
    asked = exits.get_exit

    def slow(exit_id):
        time.sleep(0.05)
        exits.exit_answer = dict(PAID)
        return asked(exit_id)

    exits.get_exit = slow
    clock.tick()
    controller.observe_exit_case()
    assert vend.vend_count == 0, "a watch that could not read the loop vouched for the car"
    assert told(controller, "left")


def test_the_loop_is_not_read_under_the_exit_lock_when_a_case_settles():
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.run_once()
    exits.exit_answer = dict(PAID)
    held: list[bool] = []
    read = controller.loop.is_occupied

    def reading():
        held.append(controller._exit_lock.locked())
        return read()

    controller.loop.is_occupied = reading
    clock.tick()
    controller.observe_exit_case()
    assert vend.vend_count == 1 and held
    assert not any(held), "the loop was read holding the lock a person's vend needs"


def test_a_flicker_as_the_answer_comes_back_does_not_drop_a_paid_car():
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.run_once()
    state = lane_reads(controller)
    asked = exits.get_exit

    def answers(exit_id):
        exits.exit_answer = dict(PAID)
        state["flicker"] = 1
        return asked(exit_id)

    exits.get_exit = answers
    clock.tick()
    controller.observe_exit_case()
    assert vend.vend_count == 1 and not told(controller, "left")


def test_a_flicker_as_the_lane_decides_does_not_drop_a_paid_car(monkeypatch):
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.run_once()
    state = lane_reads(controller)
    stop = exit_case_module.LoopWatch.stop

    def stop_then_flicker(self):
        stop(self)
        state["flicker"] = 1

    monkeypatch.setattr(exit_case_module.LoopWatch, "stop", stop_then_flicker)
    exits.exit_answer = dict(PAID)
    clock.tick()
    controller.observe_exit_case()
    assert vend.vend_count == 1 and not told(controller, "left")


class Readings:
    """A loop that reads as scripted, one reading a call."""

    def __init__(self, *readings: bool) -> None:
        self.readings = list(readings)

    def is_occupied(self) -> bool:
        return self.readings.pop(0)


def test_two_clear_samples_in_a_row_are_a_car_gone():
    watch = exit_case_module.LoopWatch(Readings(True, False, False, True), Clock())
    for _ in range(3):
        watch._sample()
    assert not watch.stayed()
    # The control: one clear sample is a flicker.
    watch = exit_case_module.LoopWatch(Readings(True, False, True), Clock())
    for _ in range(2):
        watch._sample()
    assert watch.stayed()


def test_no_watcher_is_left_running_after_a_platform_call_that_raised():
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.run_once()
    exits.get_exit = lambda exit_id: (_ for _ in ()).throw(RuntimeError("client"))
    clock.tick()
    with pytest.raises(RuntimeError):
        controller.observe_exit_case()
    assert not watchers(), "a watcher thread outlived the call that raised"
    assert vend.vend_count == 0


def test_a_car_that_backs_off_as_the_lane_decides_is_not_let_out(monkeypatch):
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.run_once()
    stop = exit_case_module.LoopWatch.stop

    def stop_then_backs_off(self):
        stop(self)
        controller.loop.clear()

    monkeypatch.setattr(exit_case_module.LoopWatch, "stop", stop_then_backs_off)
    exits.exit_answer = dict(PAID)
    clock.tick()
    controller.observe_exit_case()
    assert vend.vend_count == 0 and told(controller, "left")


def test_the_watch_reads_the_loop_as_the_call_returns(monkeypatch):
    """Clear from the moment the answer comes back until the watch stops, and
    a car on the loop again by the time the lane decides: only the watch's
    own reading at the stop saw the loop clear."""
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.run_once()
    asked = exits.get_exit

    def answers(exit_id):
        exits.exit_answer = dict(PAID)
        controller.loop.clear()
        return asked(exit_id)

    stop = exit_case_module.LoopWatch.stop

    def stop_then_back(self):
        stop(self)
        controller.loop._occupied = True

    exits.get_exit = answers
    monkeypatch.setattr(exit_case_module.LoopWatch, "stop", stop_then_back)
    clock.tick()
    controller.observe_exit_case()
    assert vend.vend_count == 0 and told(controller, "left")


def test_a_car_that_backs_off_while_its_first_answer_is_asked_is_not_let_out():
    controller, vend, exits, _, _ = an_exit(answer=PAID)
    opened = exits.open_exit

    def backs_off(**kwargs):
        controller.loop.clear()
        return opened(**kwargs)

    exits.open_exit = backs_off
    decision = controller.run_once()
    assert decision.fallback is Fallback.EXIT_NOT_SETTLED and vend.vend_count == 0
    assert [e["detail"]["exit_id"] for e in told(controller, "left")] == ["exit-1"]


def test_the_arming_loop_is_sampled_inside_a_flicker_while_a_car_waits():
    controller, _, _, _, _ = an_exit(answer=PAYMENT)
    controller.run_once()
    asked: list = []
    controller.loop.wait_for_vehicle = lambda timeout=None: asked.append(timeout) or False
    controller.run_once(timeout=1.0)
    controller.run_once(timeout=None)
    assert asked == [EXIT_SAMPLE_SECONDS, EXIT_SAMPLE_SECONDS]
    assert EXIT_SAMPLE_SECONDS < EXIT_FLICKER_SECONDS


def test_the_control_with_no_car_waiting_the_turn_is_the_callers():
    controller, _, _, _, _ = an_exit("PASS-1")
    controller.run_once()
    asked: list = []
    controller.loop.wait_for_vehicle = lambda timeout=None: asked.append(timeout) or False
    controller.run_once(timeout=1.0)
    assert asked == [1.0]


# ---------------------------------------------------------------------------
# A reader that never frees up, and answers that are not this car's.
# ---------------------------------------------------------------------------


def test_a_reader_that_stays_busy_tells_a_person_once_and_the_gate_stays_shut():
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    exits.exit_answer = {**PAYMENT, "payment": {"status": "open", "last_failure": None}}
    controller.run_once()
    exits.exit_pay_refusal = "reader_busy"
    for _ in range(EXIT_READER_BUSY - 1):
        clock.tick(EXIT_RESEND_SECONDS)
        controller.observe_exit_case()
    assert not told(controller, "reader_busy")
    for _ in range(3):
        clock.tick(EXIT_RESEND_SECONDS)
        controller.observe_exit_case()
    assert [e["detail"]["exit_id"] for e in told(controller, "reader_busy")] == ["exit-1"]
    assert controller._exit_case.attempts == 1 and vend.vend_count == 0


def test_busy_answers_count_again_once_the_reader_takes_the_fee():
    controller, _, exits, clock, _ = an_exit(answer=PAYMENT)
    exits.exit_answer = {**PAYMENT, "payment": {"status": "open", "last_failure": None}}
    controller.run_once()

    def turns(n):
        for _ in range(n):
            clock.tick(EXIT_RESEND_SECONDS)
            controller.observe_exit_case()

    exits.exit_pay_refusal = "reader_busy"
    turns(EXIT_READER_BUSY - 1)
    exits.exit_pay_refusal = None
    turns(1)
    exits.exit_pay_refusal = "reader_busy"
    turns(EXIT_READER_BUSY - 1)
    assert not told(controller, "reader_busy"), "busy answers before the take were counted"
    turns(1)
    assert len(told(controller, "reader_busy")) == 1


def test_a_settled_word_answer_for_another_event_opens_nothing():
    display = SimulatedExitDisplay("2026-06-10T09:00:00Z")
    controller, vend, exits, _, _ = an_exit(
        "NEW-1", answer={"settled": False, "needs": "driver_word"}, display=display)
    exits.exit_word = lambda exit_id, entry_at: {
        "id": exit_id, "event_id": "another-event", "settled": True, "why": "paid"}
    decision = controller.run_once()
    assert decision.fallback is Fallback.EXIT_NOT_SETTLED and vend.vend_count == 0
    assert controller._exit_case.refused == "another_exit_case"


def test_a_case_car_the_lane_could_not_price_never_has_the_reader_driven_after_its_fee():
    controller, vend, exits, clock, _ = an_exit("NEW-1", answer=PAYMENT)
    calls = a_reader(controller, exits)
    controller.run_once()
    assert controller._exit_case.session_id is None, "the lane priced this car"
    assert calls == ["platform:pay"]
    exits.exit_answer = dict(PAID)
    clock.tick()
    controller.observe_exit_case()
    assert vend.vend_count == 1
    assert calls == ["platform:pay"], "the lane drove the reader after the intent was on it"


def test_a_settled_pay_answer_for_another_case_opens_nothing():
    controller, vend, exits, _, _ = an_exit(answer=PAYMENT)
    exits.exit_pay = lambda exit_id: (
        {"id": "exit-9", "event_id": "another-event", "settled": True, "why": "paid"}, True)
    decision = controller.run_once()
    assert decision.fallback is Fallback.EXIT_NOT_SETTLED and vend.vend_count == 0
    assert [e["detail"]["refused"] for e in told(controller, "refused")] == ["another_exit_case"]


def test_a_first_answer_for_another_event_opens_nothing():
    controller, vend, exits, _, _ = an_exit(answer=PAYMENT)
    exits.open_exit = lambda **kwargs: {
        "id": "exit-9", "event_id": "another-event", "settled": True, "why": "paid"}
    decision = controller.run_once()
    assert decision.fallback is Fallback.EXIT_NOT_SETTLED and vend.vend_count == 0
    assert controller._exit_case.exit_id is None
    assert [e["detail"]["refused"] for e in told(controller, "refused")] == ["another_exit_case"]


def test_the_vend_route_takes_the_next_car_after_an_already_completed_refusal():
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    service = LaneService(controller)
    controller.run_once()
    judged = AssistedVend._refuse

    def settles_in_between(request):
        refusal = judged(service.assisted, request)
        if refusal is None:
            exits.exit_answer = dict(PAID)
            clock.tick()
            controller.observe_exit_case()
        return refusal

    service.assisted._refuse = settles_in_between
    status, answer = person_opens(service, controller)
    assert status == 409 and answer["code"] == VendRefusal.ALREADY_COMPLETED.value
    del service.assisted._refuse
    # The next car, and a person who opens for it: not refused `busy`.
    controller.loop = SimulatedLoopInput(arrivals=1)
    exits.exit_answer = dict(PAYMENT)
    controller.run_once()
    status, answer = person_opens(service, controller, key="KEY-EXIT-2")
    assert status == 202, answer
    assert vend.vend_count == 2


# ---------------------------------------------------------------------------
# The last car's cancel waits for the next car's frames.
# ---------------------------------------------------------------------------


def two_cars(first, second):
    """An exit lane two cars arrive at, and one list of what happened in order:
    frames taken, the barrier moved, and the platform's /pay and /pay/cancel."""
    rules = a_platform(gp=gp_register(gp_row("PASS-1")),
                       stays=[("s-7", "TRNS-7", ENTRY), ("s-8", "TRNS-8", ENTRY)])
    exits = FakePlatform()
    exits.reject_close_without_open = False
    exits.exit_answer = dict(PAYMENT)
    vend = RecordingVendOutput()
    controller = LaneController(
        a_config(), loop=SimulatedLoopInput(arrivals=2), camera=CannedCameraFeed(),
        vend=vend,
        identifier=StubVehicleIdentifier([
            VehicleIdentity(plate=p, plate_region="FL", make=None, model=None, color=None,
                            confidence=0.99)
            for p in (first, second)]),
        cache=a_cache(rules), events=EventQueue(PlatformTransport(exits)), clock=Clock(),
        exits=exits)
    order: list[str] = []
    grab, pulse = controller.camera.grab, vend.vend
    pay, cancel = exits.exit_pay, exits.exit_pay_cancel

    def grabbing(count=1):
        order.append("frames")
        return grab(count)

    def vending(reason):
        order.append("vend")
        return pulse(reason)

    def paying(exit_id):
        order.append(f"pay {exit_id}")
        return pay(exit_id)

    def cancelling(exit_id):
        order.append(f"pay_cancel {exit_id}")
        return cancel(exit_id)

    controller.camera.grab, vend.vend = grabbing, vending
    exits.exit_pay, exits.exit_pay_cancel = paying, cancelling
    return controller, order


def test_the_last_cars_cancel_comes_after_the_next_cars_frames_and_before_its_fee():
    controller, order = two_cars("TRNS-7", "TRNS-8")
    controller.run_once()
    controller.run_once()
    assert order == ["frames", "pay exit-1", "frames", "pay_cancel exit-1", "pay exit-2"]


def test_a_covered_car_after_a_case_car_is_let_out_before_the_last_cars_cancel():
    controller, order = two_cars("TRNS-7", "PASS-1")
    controller.run_once()
    controller.run_once()
    assert order == ["frames", "pay exit-1", "frames", "vend", "pay_cancel exit-1"]


def test_the_platform_client_takes_the_payment_off_the_reader_on_the_cases_own_route():
    seen = []

    class Answer:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout):
        seen.append((request.get_method(), request.full_url, request.data))
        return Answer(json.dumps({"exit": {"id": "e 1", "settled": False}}).encode())

    client = PlatformClient("http://platform.test", "tok", opener=opener)
    assert client.exit_pay_cancel("e 1")["id"] == "e 1"
    assert seen == [("POST", "http://platform.test/api/v1/lane/exits/e%201/pay/cancel", b"{}")]


# ---------------------------------------------------------------------------
# The cancel's answers, the close's retry, and who opened (platform 0042).
# ---------------------------------------------------------------------------


def a_case_on_the_reader():
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.run_once()
    case = controller._exit_case
    assert case is not None and case.sent_at is not None
    return case, exits


def test_the_cancel_reads_exit_paid_as_paid_and_payment_in_progress_as_neither():
    case, exits = a_case_on_the_reader()
    for refusal, outcome in [
        (None, exit_case_module.CANCELED),
        ("exit_paid", exit_case_module.CANCEL_FOUND_PAID),
        ("no_payment", exit_case_module.CANCEL_NOTHING),
        ("exit_closed", exit_case_module.CANCEL_CLOSED),
        ("payment_in_progress", exit_case_module.CANCEL_IN_PROGRESS),
        ("exit_on_another_lane", exit_case_module.CANCEL_REFUSED),
        ("payment_amount_mismatch", exit_case_module.CANCEL_REFUSED),
    ]:
        exits.exit_pay_cancel_refusal = refusal
        assert exit_case_module.cancel_on_reader(case, exits) == outcome, refusal
    exits.exit_pay_cancel_refusal = None
    exits.online = False
    assert exit_case_module.cancel_on_reader(case, exits) == exit_case_module.CANCEL_FAILED
    exits.online = True

    def rate_limited(exit_id):
        raise PlatformRejected(429, "{}")

    exits.exit_pay_cancel = rate_limited
    assert exit_case_module.cancel_on_reader(case, exits) == exit_case_module.CANCEL_FAILED


def cancels(exits):
    return [r for r in exits.exit_requests if r[0] == "pay_cancel"]


def test_a_cancel_answered_exit_paid_is_not_a_failure_and_opens_nothing_for_a_car_gone(caplog):
    """The answer came back paid as the car backed off the loop: the case is
    left to a person, and the cancel finds the card had paid. Not a failure,
    not asked again, and nothing opens for whoever is on the loop next."""
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.run_once()
    exits.exit_pay_cancel_refusal = "exit_paid"
    slow_answer(controller, exits, clock, backs_off=True)
    clock.tick()
    with caplog.at_level(logging.INFO, logger="lane_controller.exit_case"):
        controller.observe_exit_case()
        controller.loop._occupied = True
        for _ in range(3):
            clock.tick(EXIT_POLL_SECONDS)
            controller.observe_exit_case()
    assert vend.vend_count == 0, "a card found paid opened the barrier for a car the loop lost"
    assert len(cancels(exits)) == 1, "a cancel answered exit_paid was asked again"
    assert [e["detail"]["exit_id"] for e in told(controller, "left")] == ["exit-1"]
    cancel_lines = [r for r in caplog.records if r.name == "lane_controller.exit_case"
                    and "cancel" in r.getMessage()]
    assert cancel_lines and all(r.levelno < logging.WARNING for r in cancel_lines), (
        "exit_paid was logged as a failure")


def test_a_cancel_answered_payment_in_progress_keeps_the_gate_shut():
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.run_once()
    exits.exit_pay_cancel_refusal = "payment_in_progress"
    controller.loop.clear()
    controller.observe_exit_case()
    clock.tick(EXIT_LEFT_SECONDS)
    controller.observe_exit_case()
    controller.loop._occupied = True
    clock.tick(EXIT_POLL_SECONDS)
    controller.observe_exit_case()
    assert vend.vend_count == 0, "a card still being authorised let a car out"
    assert len(cancels(exits)) == 1


def test_a_persons_vend_whose_cancel_finds_the_card_paid_opens_once_and_still_closes_the_case():
    controller, vend, exits, _, _ = an_exit(answer=PAYMENT)
    service = LaneService(controller)
    controller.run_once()
    exits.exit_pay_cancel_refusal = "exit_paid"
    status, answer = person_opens(service, controller)
    assert status == 202 and vend.vend_count == 1, answer
    controller.events.flush()
    assert len(cancels(exits)) == 1
    assert exits.closed[-1]["exit_id"] == "exit-1"


def test_a_persons_close_names_the_authority_it_opened_on():
    controller, vend, exits, _, _ = an_exit(answer=PAYMENT)
    service = LaneService(controller)
    controller.run_once()
    person_opens(service, controller)
    controller.events.flush()
    assert exits.closed[-1]["exit_id"] == "exit-1"
    assert exits.closed[-1].get("let_out_by") == VendAuthority.HUMAN_OPEN_NOW.value


def test_a_persons_close_after_a_flagged_opening_names_that_authority():
    """`human_open_and_flag` rides the close as the other two do; the
    platform marks its change-log line flagged."""
    controller, vend, exits, _, _ = an_exit(answer=PAYMENT)
    service = LaneService(controller)
    controller.run_once()
    body = {"authorised_by": VendAuthority.HUMAN_OPEN_AND_FLAG.value,
            "identity": {"kind": "ticket", "ticket_ref": "TKT-4RS9WQ2M"},
            "decision_at": controller.last_decision_at}
    status, answer = service.assisted.complete(parse_vend(body, "KEY-EXIT-FLAG"))
    if service.assisted.settling is not None:
        service.assisted.settling.join(timeout=5)
    assert status == 202 and vend.vend_count == 1, answer
    controller.events.flush()
    assert exits.closed[-1]["exit_id"] == "exit-1"
    assert exits.closed[-1].get("let_out_by") == VendAuthority.HUMAN_OPEN_AND_FLAG.value


def test_a_close_the_lane_opened_on_its_own_names_no_person():
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.run_once()
    exits.exit_answer = dict(PAID)
    clock.tick()
    controller.observe_exit_case()
    controller.events.flush()
    assert vend.vend_count == 1 and exits.closed[-1]["exit_id"] == "exit-1"
    assert "let_out_by" not in exits.closed[-1]


class CloseOverHttp:
    """The fake platform, with the close sent through a real `PlatformClient`
    to a platform that answers it as `answers` say, one answer a request."""

    def __init__(self, fake: FakePlatform, answers: list[tuple[int, dict | None]]) -> None:
        self.fake = fake
        self.answers = answers
        self.bodies: list[dict] = []
        self.client = PlatformClient("http://platform.test", "tok", opener=self._open)

    def __getattr__(self, name):
        return getattr(self.fake, name)

    def close_session(self, **kwargs):
        return self.client.close_session(**kwargs)

    def _open(self, request, timeout):
        sent = json.loads(request.data)
        self.bodies.append(sent)
        status, body = self.answers.pop(0)
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "refused", {},
                                         io.BytesIO(json.dumps(body).encode()))
        echo = {"session": {"id": "s-7", "exit_confirmation": sent["exit_confirmation"]},
                "closed": True}

        class Answer:
            def read(self):
                return json.dumps(echo).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return Answer()


def test_a_persons_close_answered_503_payment_unsettled_stays_queued_and_is_sent_again():
    controller, vend, exits, _, _ = an_exit(answer=PAYMENT)
    unsettled = {"error": "the exit case's payment could not be settled with Stripe now",
                 "code": "payment_unsettled"}
    platform = CloseOverHttp(exits, [(503, unsettled), (200, None)])
    controller.events = EventQueue(PlatformTransport(platform))
    service = LaneService(controller)
    controller.run_once()
    status, answer = person_opens(service, controller)
    assert status == 202 and vend.vend_count == 1, answer
    assert len(platform.bodies) == 1, "the close was not sent"
    assert controller.events.pending_sessions == 1, "a 503 close was dropped from the outbox"
    controller.events.flush()
    assert controller.events.pending_sessions == 0
    first, again = platform.bodies
    assert again == first, "the close sent again is not the close that was refused"
    assert first["exit_id"] == "exit-1"
    assert first["let_out_by"] == VendAuthority.HUMAN_OPEN_NOW.value


# ---------------------------------------------------------------------------
# A failed cancel is asked again until the platform confirms (fix 4).
# ---------------------------------------------------------------------------


def cancel_fails(exits, times, *, how="unreachable"):
    """`/pay/cancel` fails `times` times -- the platform out of reach, or a
    card still being authorised -- and then confirms."""
    from lane_controller.platform_client import PlatformUnreachable

    real = exits.exit_pay_cancel
    left = {"n": times}

    def cancel(exit_id):
        exits.exit_requests.append(("pay_cancel", exit_id))
        if left["n"] > 0:
            left["n"] -= 1
            if how == "unreachable":
                raise PlatformUnreachable("timed out")
            raise PlatformRejected(409, "{}", "payment_in_progress")
        exits.exit_requests.pop()
        return real(exit_id)

    exits.exit_pay_cancel = cancel


def dropped_on_the_reader(exits_times=1, *, how="unreachable", drain=True, events=None):
    """A case whose fee is on the reader, dropped because its car left, with
    the first cancel failing. `drain`: a `LaneRunner`-shaped drain is
    installed, so `deliver()` is a signal and the lane's thread never asks:
    the first ask is the drain's (`retry_cancels`), made here."""
    controller, vend, exits, clock, order = an_exit(answer=PAYMENT)
    if events is not None:
        events._transport = PlatformTransport(exits)
        controller.events = events
        controller._cancels = exit_case_module.PendingCancels(clock, events)
    if drain:
        controller.set_drain(lambda: None)
    controller.run_once()
    cancel_fails(exits, exits_times, how=how)
    controller.loop.clear()
    controller.observe_exit_case()
    clock.tick(EXIT_LEFT_SECONDS)
    controller.observe_exit_case()
    if drain:
        assert cancels(exits) == [], "the lane's thread asked the cancel"
        assert controller.retry_cancels() == 1, "the drain's first ask"
    assert len(cancels(exits)) == 1, "the first ask"
    return controller, vend, exits, clock


@pytest.mark.parametrize("how", ["unreachable", "payment_in_progress"])
def test_a_failed_cancel_is_asked_again_with_a_bounded_backoff_until_confirmed(how):
    controller, vend, exits, clock = dropped_on_the_reader(7, how=how)
    asked_at = []
    start = clock.now
    for _ in range(int(200 / 0.5)):
        clock.tick(0.5)
        if controller.retry_cancels():
            asked_at.append(clock.now - start)
    # 1, 2, 4, 8, 16, 30, 30 seconds apart: bounded, and asked until confirmed
    gaps = [b - a for a, b in zip([0.0, *asked_at], asked_at, strict=False)]
    assert gaps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0], gaps
    assert len(cancels(exits)) == 8, "seven failures and the confirming ask"
    assert "exit-1" not in controller._cancels
    clock.tick(3600)
    assert controller.retry_cancels() == 0, "a confirmed cancel is never asked again"
    assert vend.vend_count == 0


def test_the_backoff_is_bounded():
    assert [exit_case_module.cancel_backoff(n) for n in range(1, 9)] == [
        1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]


def test_the_lane_thread_never_asks_a_cancel_again_under_the_runner():
    """Never blocking the lane loop: with the runner's drain installed the lane
    turns go on, past every backoff, and only the drain asks again."""
    controller, vend, exits, clock = dropped_on_the_reader(1)
    for _ in range(20):
        clock.tick(5)
        controller.observe_exit_case()
        controller.run_once(timeout=0)
    # the next car, covered, served on the lane's thread: every delivery it
    # makes is the runner's signal, and none of them asks the cancel again
    controller.identifier = StubVehicleIdentifier([VehicleIdentity(
        plate="PASS-1", plate_region="FL", make=None, model=None, color=None,
        confidence=0.99)])
    controller.loop = SimulatedLoopInput(arrivals=1)
    assert controller.run_once().should_vend and vend.vend_count == 1
    controller.deliver()
    assert len(cancels(exits)) == 1, "the lane's thread asked a cancel again"
    from lane_controller.runner import LaneRunner

    runner = LaneRunner(controller, client=None, rules_refresh_s=300.0, stays_refresh_s=5.0)
    runner.drain_turn()
    assert len(cancels(exits)) == 2 and "exit-1" not in controller._cancels


def test_a_slow_retry_never_holds_the_lane():
    controller, vend, exits, clock = dropped_on_the_reader(1)
    release, entered = threading.Event(), threading.Event()
    real = exits.exit_pay_cancel

    def hung(exit_id):
        entered.set()
        release.wait(5)
        return real(exit_id)

    exits.exit_pay_cancel = hung
    clock.tick(5)
    worker = threading.Thread(target=controller.retry_cancels)
    worker.start()
    assert entered.wait(2)
    began = time.monotonic()
    controller.observe_exit_case()
    controller.run_once(timeout=0)
    assert controller.retry_cancels() == 0, "a second retry pass while one is in flight"
    assert time.monotonic() - began < 1.0, "the lane waited on a cancel"
    release.set()
    worker.join(5)
    assert "exit-1" not in controller._cancels


def test_a_bare_controller_asks_again_when_it_delivers():
    controller, vend, exits, clock = dropped_on_the_reader(1, drain=False)
    clock.tick(1)
    controller.deliver()
    assert len(cancels(exits)) == 2 and "exit-1" not in controller._cancels


def test_a_person_is_told_once_when_a_cancel_keeps_failing_and_the_lane_goes_on_asking():
    controller, vend, exits, clock = dropped_on_the_reader(50)
    for _ in range(int(600 / 0.5)):
        clock.tick(0.5)
        controller.retry_cancels()
    pending = told(controller, "cancel_pending")
    assert [(e["detail"]["exit_id"], e["detail"]["attempts"]) for e in pending] == [
        ("exit-1", exit_case_module.EXIT_CANCEL_TELL_AFTER)]
    assert "exit-1" in controller._cancels, "still asked"
    assert vend.vend_count == 0


def test_a_cancel_the_platform_refuses_outright_is_not_asked_again_and_a_person_is_told():
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.set_drain(lambda: None)
    controller.run_once()
    exits.exit_pay_cancel_refusal = "exit_on_another_lane"
    controller.loop.clear()
    controller.observe_exit_case()
    clock.tick(EXIT_LEFT_SECONDS)
    controller.observe_exit_case()
    assert controller.retry_cancels() == 1, "the drain asks"
    assert "exit-1" not in controller._cancels
    assert [e["detail"]["exit_id"] for e in told(controller, "cancel_refused")] == ["exit-1"]
    clock.tick(60)
    assert controller.retry_cancels() == 0 and len(cancels(exits)) == 1


def test_a_cancel_not_confirmed_before_a_restart_is_asked_again_after_it(tmp_path):
    from lane_controller.durable import OutboxStore

    path = tmp_path / "lane" / "decision-cache.outbox.sqlite"
    first = EventQueue(store=OutboxStore(path))
    controller, vend, exits, clock = dropped_on_the_reader(1, events=first)
    assert "exit-1" in controller._cancels
    assert first.wait_written(5)
    assert OutboxStore(path).held_cancels() == ["exit-1"]
    # THE RESTART: a new process, a new queue on the same file
    again = EventQueue(PlatformTransport(exits), store=OutboxStore(path))
    restarted = LaneController(
        controller.config, loop=SimulatedLoopInput(arrivals=0), camera=CannedCameraFeed(),
        vend=RecordingVendOutput(), identifier=StubVehicleIdentifier([]), cache=controller.cache,
        events=again, clock=clock, exits=exits)
    restarted.set_drain(lambda: None)
    assert "exit-1" in restarted._cancels, "the restart forgot the cancel"
    assert restarted.retry_cancels() == 1, "due at once after a restart"
    assert len(cancels(exits)) == 2 and "exit-1" not in restarted._cancels
    assert again.wait_written(5)
    assert OutboxStore(path).held_cancels() == [], "a confirmed cancel left on disk"


# ---------------------------------------------------------------------------
# The payment-status guard, and no plate in a log line (fix 5).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["processing", "requires_capture", "succeeded", "canceled"])
def test_a_payment_that_is_not_open_is_never_handed_to_the_reader_again(status):
    """`_act`'s guard: only a payment that is open (or none yet) goes to the
    reader. A card being processed, captured or canceled is the platform's;
    handing the fee over again would put a second payment on the reader."""
    controller, vend, exits, clock, _ = an_exit(
        answer={**PAYMENT, "payment": {"status": status, "last_failure": None}})
    controller.run_once()
    for _ in range(3):
        clock.tick(EXIT_RESEND_SECONDS)
        controller.observe_exit_case()
    assert pays(exits) == [], status
    assert vend.vend_count == 0


def test_the_control_an_open_payment_is_handed_to_the_reader():
    controller, vend, exits, clock, _ = an_exit(
        answer={**PAYMENT, "payment": {"status": "open", "last_failure": None}})
    controller.run_once()
    assert len(pays(exits)) == 1


def test_no_log_line_carries_the_plate_text(caplog):
    """Every exit-case path that logs -- opened, paid, declined, busy, word,
    refused, unanswered, left, cancel failing and confirmed -- and no
    record names the plate, as typed or as folded."""
    caplog.set_level(logging.DEBUG)
    display = SimulatedExitDisplay(entry_time="2026-09-21T09:00:00-04:00")
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT, display=display)
    controller.run_once()
    exits.exit_answer = {**PAYMENT, "payment": {"status": "open", "last_failure": DECLINED}}
    clock.tick()
    controller.observe_exit_case()
    exits.exit_pay_refusal = "reader_busy"
    clock.tick(EXIT_RESEND_SECONDS)
    controller.observe_exit_case()
    exits.exit_pay_refusal = None
    exits.online = False
    clock.tick(EXIT_RESEND_SECONDS)
    controller.observe_exit_case()
    exits.online = True
    exits.exit_answer = {"settled": False, "needs": "driver_word"}
    clock.tick(EXIT_RESEND_SECONDS)
    controller.observe_exit_case()
    exits.exit_answer = dict(PAID)
    clock.tick()
    controller.observe_exit_case()
    # a second car, dropped with its cancel failing, then confirmed
    second, _, exits2, clock2 = dropped_on_the_reader(1, drain=False)
    clock2.tick(2)
    second.deliver()
    text = caplog.text
    assert "exit case" in text, "the control: the exit case does log"
    for plate in ("TRNS-7", "TRNS7", "TRN57", fold("TRNS-7")):
        assert plate not in text, plate


# ---------------------------------------------------------------------------
# At night, a reader that cannot take a card (Gokhan, 2026-10-30).
# ---------------------------------------------------------------------------


def a_reader_that_cannot_take_a_card(reader):
    """An exit case needing payment whose `/pay` always fails: the reader is
    offline (503 `reader_offline`) or none is bound to the lane."""
    from lane_controller.platform_client import PlatformUnreachable

    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    if reader == "offline":
        def offline(exit_id):
            exits.exit_requests.append(("pay", exit_id))
            raise PlatformUnreachable("HTTP 503: reader_offline")

        exits.exit_pay = offline
        return controller, vend, exits, clock, "unanswered"
    exits.exit_pay_refusal = "no_reader_bound"
    return controller, vend, exits, clock, "refused"


@pytest.mark.parametrize("reader", ["offline", "none bound"])
def test_at_night_a_reader_that_cannot_take_a_card_keeps_the_car_at_a_shut_gate_for_a_person(
    reader,
):
    """The decision of 2026-10-30, as the lane does it already: the card reader
    cannot take a card and nobody is on duty -- the case waits at a shut gate,
    a person is told once, and the lane never opens on its own however long
    the car waits (six hours here). A person deciding the case on the
    platform is the way out, and the barrier then moves once."""
    controller, vend, exits, clock, need = a_reader_that_cannot_take_a_card(reader)
    decision = controller.run_once()
    assert decision.fallback is Fallback.EXIT_NOT_SETTLED and vend.vend_count == 0
    for _ in range(6 * 60):  # six hours, a turn a minute
        clock.tick(60)
        controller.observe_exit_case()
    assert vend.vend_count == 0, "the gate opened with no card taken and no person"
    assert controller._exit_case is not None, "the case was let go"
    assert len(told(controller, need)) == 1, "a person is told once"
    assert len(pays(exits)) > 1, "the fee is still offered to the reader"
    exits.exit_answer = {"settled": True, "why": "person"}
    clock.tick(60)
    controller.observe_exit_case()
    assert vend.vend_count == 1


@pytest.mark.parametrize("reader", ["offline", "none bound"])
def test_a_person_at_the_lane_lets_out_the_car_a_dead_reader_kept_waiting(reader):
    controller, vend, exits, clock, need = a_reader_that_cannot_take_a_card(reader)
    service = LaneService(controller)
    controller.run_once()
    for _ in range(3):  # a person at the lane within the completion's age bound
        clock.tick(30)
        controller.observe_exit_case()
    assert vend.vend_count == 0 and len(told(controller, need)) == 1
    status, said = person_opens(service, controller)
    assert status == 202 and vend.vend_count == 1, said


# ---------------------------------------------------------------------------
# `no_payment` is confirmed (review 2, item 4); the first cancel is the
# drain's, and the next fee waits for it (review 2, item 5).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("drain", [True, False], ids=["runner", "bare"])
def test_a_cancel_answered_no_payment_is_confirmed_and_never_asked_again(drain):
    """The case had no payment to cancel: nothing is on the reader, so the
    cancel is done -- not asked again, and nobody told."""
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    if drain:
        controller.set_drain(lambda: None)
    controller.run_once()
    exits.exit_pay_cancel_refusal = "no_payment"
    controller.loop.clear()
    controller.observe_exit_case()
    clock.tick(EXIT_LEFT_SECONDS)
    controller.observe_exit_case()
    controller.retry_cancels()
    assert len(cancels(exits)) == 1 and "exit-1" not in controller._cancels
    for _ in range(10):
        clock.tick(60)
        controller.retry_cancels()
    assert len(cancels(exits)) == 1, "a cancel answered no_payment was asked again"
    assert not told(controller, "cancel_pending") and not told(controller, "cancel_refused")


def test_a_pending_cancel_answered_no_payment_on_a_later_ask_is_settled():
    controller, vend, exits, clock = dropped_on_the_reader(1)
    exits.exit_pay_cancel = lambda exit_id: (_ for _ in ()).throw(
        PlatformRejected(409, "{}", "no_payment"))
    clock.tick(1)
    assert controller.retry_cancels() == 1 and "exit-1" not in controller._cancels
    clock.tick(60)
    assert controller.retry_cancels() == 0


def test_the_lane_thread_never_waits_on_a_cancel_not_even_the_first():
    """The first cancel used to be asked on the lane's thread, up to the
    client's timeout, as the car left or the next car came. Now the lane's
    thread only queues it: with the platform hung on the cancel, the lane
    lets the car go and serves the next car at once, and only the drain asks."""
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.set_drain(lambda: None)
    controller.run_once()
    release, threads = threading.Event(), []
    real = exits.exit_pay_cancel

    def hung(exit_id):
        threads.append(threading.current_thread().name)
        release.wait(10)
        return real(exit_id)

    exits.exit_pay_cancel = hung
    began = time.monotonic()
    controller.loop.clear()
    controller.observe_exit_case()
    clock.tick(EXIT_LEFT_SECONDS)
    controller.observe_exit_case()
    # and the next car, covered, at the same lane
    controller.identifier = StubVehicleIdentifier([VehicleIdentity(
        plate="PASS-1", plate_region="FL", make=None, model=None, color=None,
        confidence=0.99)])
    controller.loop = SimulatedLoopInput(arrivals=1)
    assert controller.run_once().should_vend and vend.vend_count == 1
    assert time.monotonic() - began < 2.0, "the lane waited on a cancel"
    assert threads == [], "the lane's thread asked the cancel"
    assert "exit-1" in controller._cancels, "queued for the drain"
    worker = threading.Thread(target=controller.retry_cancels, name="outbox-drain")
    worker.start()
    for _ in range(100):
        if threads:
            break
        time.sleep(0.02)
    assert threads == ["outbox-drain"]
    release.set()
    worker.join(5)


def test_the_next_cars_fee_waits_for_the_last_cars_cancel_under_the_runner():
    """The order finding 2 set -- the last car's payment off the reader before
    the next car's goes on -- kept with the cancel off the lane's thread: the
    next case's fee is not handed over while the last case's cancel is
    unconfirmed, and goes on once the drain has it confirmed."""
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.set_drain(lambda: None)
    controller.run_once()
    assert pays(exits) == [("pay", "exit-1")]
    controller.loop.clear()
    controller.observe_exit_case()
    clock.tick(EXIT_LEFT_SECONDS)
    controller.observe_exit_case()
    # the next car, with a fee of its own
    controller.loop = SimulatedLoopInput(arrivals=1)
    controller.identifier = StubVehicleIdentifier([VehicleIdentity(
        plate="TRNS-7", plate_region="FL", make=None, model=None, color=None,
        confidence=0.99)])
    controller.run_once()
    assert pays(exits) == [("pay", "exit-1")], "the next fee went on before the cancel"
    assert cancels(exits) == []
    controller.retry_cancels()  # the drain confirms the last car's cancel
    assert cancels(exits) == [("pay_cancel", "exit-1")]
    clock.tick()
    controller.observe_exit_case()
    assert pays(exits) == [("pay", "exit-1"), ("pay", "exit-2")]
    assert vend.vend_count == 0


def test_a_car_held_behind_a_cancel_that_never_confirms_reaches_a_person_and_its_fee_waits():
    """Review 3: the earlier car's cancel never confirms. Its own alert names
    the earlier case; past `EXIT_FEE_HELD_TELL_SECONDS` the HELD car is sent to
    a person too, once, by its own case id -- and its fee still never goes on
    the reader while the earlier cancel is unconfirmed. Ten minutes of it."""
    from lane_controller.platform_client import PlatformUnreachable

    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    controller.set_drain(lambda: None)
    controller.run_once()

    def never(exit_id):
        exits.exit_requests.append(("pay_cancel", exit_id))
        raise PlatformUnreachable("timed out")

    exits.exit_pay_cancel = never
    controller.loop.clear()
    controller.observe_exit_case()
    clock.tick(EXIT_LEFT_SECONDS)
    controller.observe_exit_case()
    controller.loop = SimulatedLoopInput(arrivals=1)
    controller.run_once()  # the next car, with a fee of its own: held
    held = controller._exit_case
    assert held is not None and held.exit_id == "exit-2" and held.held_since is not None
    waited = 0.0
    while waited < exit_case_module.EXIT_FEE_HELD_TELL_SECONDS - 1:
        clock.tick(1)
        waited += 1
        controller.retry_cancels()
        controller.observe_exit_case()
    assert told(controller, exit_case_module.NEEDS_FEE_HELD) == [], "told before the bound"
    for _ in range(600):
        clock.tick(1)
        controller.retry_cancels()
        controller.observe_exit_case()
    [said] = told(controller, exit_case_module.NEEDS_FEE_HELD)
    assert said["detail"]["exit_id"] == "exit-2", "the alert names the held car's own case"
    assert "TRNS" not in json.dumps(said)
    assert [e["detail"]["exit_id"] for e in told(controller, "cancel_pending")] == ["exit-1"]
    assert pays(exits) == [("pay", "exit-1")], "the held car's fee was sent"
    assert vend.vend_count == 0
    # THE EARLIER CANCEL CONFIRMS: the held fee goes on, as before
    exits.exit_pay_cancel = lambda exit_id: exits._case(exit_id)
    clock.tick(31)
    controller.retry_cancels()
    clock.tick()
    controller.observe_exit_case()
    assert pays(exits) == [("pay", "exit-1"), ("pay", "exit-2")]
    assert len(told(controller, exit_case_module.NEEDS_FEE_HELD)) == 1


def test_a_person_lets_out_the_car_held_behind_a_cancel():
    from lane_controller.platform_client import PlatformUnreachable

    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    service = LaneService(controller)
    controller.set_drain(lambda: None)
    controller.run_once()
    exits.exit_pay_cancel = lambda exit_id: (_ for _ in ()).throw(PlatformUnreachable("x"))
    controller.loop.clear()
    controller.observe_exit_case()
    clock.tick(EXIT_LEFT_SECONDS)
    controller.observe_exit_case()
    controller.loop = SimulatedLoopInput(arrivals=1)
    controller.run_once()
    for _ in range(25):
        clock.tick(1)
        controller.observe_exit_case()
    assert len(told(controller, exit_case_module.NEEDS_FEE_HELD)) == 1
    clock.tick(5)
    status, said = person_opens(service, controller, key="KEY-EXIT-HELD")
    assert status == 202 and vend.vend_count == 1, said


def test_a_pending_cancel_for_a_case_the_platform_closed_is_confirmed():
    """A case the platform superseded is closed, its payment canceled by the
    supersede: a cancel answered 409 `exit_closed` is done, never asked again."""
    controller, vend, exits, clock = dropped_on_the_reader(1)
    exits.exit_pay_cancel = lambda exit_id: (_ for _ in ()).throw(
        PlatformRejected(409, "{}", "exit_closed"))
    clock.tick(1)
    assert controller.retry_cancels() == 1 and "exit-1" not in controller._cancels
    clock.tick(60)
    assert controller.retry_cancels() == 0
    assert not told(controller, "cancel_refused")


def test_no_fee_goes_to_the_reader_for_a_case_a_person_takes_mid_step():
    """Round 2 (#55's race): a person opens while the case's step is asking the
    platform how it stands; the step that follows would hand a new fee to the
    reader -- it does not, the car is let out by the person."""
    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    service = LaneService(controller)
    controller.run_once()
    assert len(pays(exits)) == 1
    exits.exit_answer = {**PAYMENT, "payment": {"status": "open", "last_failure": DECLINED}}
    real = exits.get_exit

    def person_opens_meanwhile(exit_id):
        worker = threading.Thread(target=lambda: person_opens(service, controller))
        worker.start()
        worker.join(10)
        return real(exit_id)

    exits.get_exit = person_opens_meanwhile
    clock.tick()
    controller.observe_exit_case()
    assert vend.vend_count == 1
    assert len(pays(exits)) == 1, "a fee went to the reader for a car let out"


def test_a_fee_handed_over_as_a_person_takes_the_case_has_its_cancel_queued():
    """Round 2 (#55's race): the person takes the case while the fee is on its
    way to the reader; the fee lands after their vend looked. The lane queues
    its cancel as the step ends -- nothing stays on the reader."""
    from lane_controller.platform_client import PlatformUnreachable

    controller, vend, exits, clock, _ = an_exit(answer=PAYMENT)
    service = LaneService(controller)
    real_pay = exits.exit_pay
    exits.exit_pay = lambda exit_id: (_ for _ in ()).throw(PlatformUnreachable("down"))
    controller.run_once()
    assert controller._exit_case.sent_at is None, "no fee on the reader yet"

    def person_opens_as_the_fee_goes(exit_id):
        worker = threading.Thread(target=lambda: person_opens(service, controller))
        worker.start()
        worker.join(10)
        return real_pay(exit_id)

    exits.exit_pay = person_opens_as_the_fee_goes
    clock.tick()
    controller.observe_exit_case()
    assert vend.vend_count == 1
    assert ("pay_cancel", "exit-1") in exits.exit_requests or "exit-1" in controller._cancels
