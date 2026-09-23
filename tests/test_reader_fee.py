"""The fee on the card reader: the exit decision's own record, shown and never recomputed.

Each claim is paired with the case that would falsify it. The records here are
made by the REAL `price_exit` with the rate engine in-process -- the one
computation -- except where a test plants a record on purpose to prove that a
figure is READ: a record whose fee is not the sum of its lines can only come
out of the cart as its fee if the cart reads it.

`scripts/reader_fail_control.py` breaks the seam in nine ways and requires
this file to go red each time.
"""

from __future__ import annotations

import copy
import json
import threading
import time

from fake_platform import FakePlatform
from lane_controller import DecisionCache
from lane_controller import reader as reader_module
from lane_controller.controller import LaneController
from lane_controller.events import EventQueue
from lane_controller.exit_pricing import price_exit
from lane_controller.interfaces import VehicleIdentity
from lane_controller.reader import StripeCartScreen
from lane_controller.runner import LaneRunner
from lane_controller.simulated import (
    CannedCameraFeed,
    RecordingVendOutput,
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

#: The flat plan with a daily cap under it, so the engine's own ledger carries a
#: zero line (the stay), two charges and a NEGATIVE line (the cap) -- the shapes
#: a cart has to carry as stored.
CAPPED = copy.deepcopy(PLAN)
CAPPED["plan_version"] = "capped-lot-2026-01"
CAPPED["rules"].append(
    {"id": "cap", "type": "daily_max", "stage": "CAP", "space_classes": ["standard"],
     "max_minor": 500, "day_boundary": "calendar_day"}
)


def a_capped_platform(**kwargs):
    platform = a_platform(**kwargs)
    platform.rate_plans = [CAPPED]
    return platform


def priced_record():
    cache = a_cache(a_capped_platform(stays=[("s-9", "TRNS-9", ENTRY)]))
    record = price_exit("TRNS-9", cache, now=NOW).to_detail()
    assert record["status"] == "priced", record
    return record


class RecordingReader:
    """A reader screen that keeps what it was handed, and on which thread, and when."""

    def __init__(self, vend=None) -> None:
        self.shown: list[dict | None] = []
        self.threads: list[str] = []
        self.vends_by_then: list[int] = []
        self._vend = vend

    def present_exit(self, record):
        self.shown.append(copy.deepcopy(record))
        self.threads.append(threading.current_thread().name)
        self.vends_by_then.append(len(self._vend.vends) if self._vend is not None else -1)


class InputFirstReader:
    """NOT a cart: a screen that would ask for an input first, the fee in its
    title. It reads the record by its words, as the seam promises any screen may."""

    def __init__(self) -> None:
        self.titles: list[str | None] = []

    def present_exit(self, record):
        if record is None or record.get("status") != "priced":
            self.titles.append(None)
            return
        self.titles.append(f"{record['fee_minor']} {record['currency']}")


def exit_lane(cache, plate, *, reader, transport=None, vend=None):
    identity = VehicleIdentity(plate=plate, plate_region="FL", make=None, model=None, color=None,
                               confidence=0.99)
    vend = vend or RecordingVendOutput()
    controller = LaneController(
        a_config(),
        loop=SimulatedLoopInput(arrivals=1),
        camera=CannedCameraFeed(),
        vend=vend,
        identifier=StubVehicleIdentifier([identity]),
        cache=cache,
        events=EventQueue(transport) if transport else EventQueue(),
        clock=lambda: NOW,
        reader=reader,
    )
    return controller, vend


def wait_for(predicate, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)
    return True


# ---------------------------------------------------------------------------
# the cart: the stored lines and the stored fee, carried as they are
# ---------------------------------------------------------------------------


def test_the_cart_carries_the_engines_ledger_as_stored_and_the_stored_fee():
    record = priced_record()
    cart = reader_module.cart_for(record)
    assert cart["total"] == record["fee_minor"] == 500
    assert cart["currency"] == "usd"
    assert [item["amount"] for item in cart["line_items"]] == [
        line["delta_minor"] for line in record["breakdown"]
    ]
    assert [item["description"] for item in cart["line_items"]] == [
        line["text"] for line in record["breakdown"]
    ]
    assert {item["quantity"] for item in cart["line_items"]} == {1}
    # the shapes this test exists for: a zero line and a negative line, kept
    amounts = [item["amount"] for item in cart["line_items"]]
    assert 0 in amounts and min(amounts) < 0, amounts
    assert "tax" not in cart, "a tax figure nobody computed went on the cart"


def test_the_total_is_the_stored_fee_even_when_it_is_not_the_sum_of_the_lines():
    """PLANTED: a record whose fee is not its lines' sum. The engine never
    writes one (its F8); the only way for the cart to show the planted fee is
    to READ it, and a cart that added the lines up would show 900 here."""
    record = priced_record()
    record["fee_minor"] = 777
    assert sum(line["delta_minor"] for line in record["breakdown"]) != 777
    assert reader_module.cart_for(record)["total"] == 777


def test_every_state_with_no_payment_has_no_cart():
    platform = a_capped_platform(
        stays=[("s-9", "TRNS-9", ENTRY)], gp=gp_register(gp_row("PASS-1"))
    )
    cache = a_cache(platform)
    covered = price_exit("PASS-1", cache, now=NOW).to_detail()
    unknown = price_exit("NOBODY-1", cache, now=NOW).to_detail()
    assert covered["status"] == "covered" and unknown["status"] == "no_cached_entry"
    zero = priced_record()
    zero["fee_minor"] = 0
    for record in (covered, unknown, zero, None, {}, {"status": "engine_refused"},
                   {"status": "stale_facts"}, {"status": "engine_invalid"}):
        assert reader_module.cart_for(record) is None, record


def test_a_record_the_lane_did_not_write_is_not_reworded_into_a_cart():
    for plant in (
        {"fee_minor": "500"},
        {"fee_minor": True},
        {"currency": ""},
        {"breakdown": []},
        {"breakdown": [{"delta_minor": 500}]},
        {"breakdown": [{"text": "a line with no amount"}]},
        {"breakdown": [{"delta_minor": 5.0, "text": "a float"}]},
    ):
        record = {**priced_record(), **plant}
        assert reader_module.cart_for(record) is None, plant


def test_the_form_is_the_shape_the_api_reference_names():
    record = priced_record()
    form = reader_module.cart_form(reader_module.cart_for(record))
    assert form["type"] == "cart"
    assert form["cart[currency]"] == "usd"
    assert form["cart[total]"] == "500"
    count = len(record["breakdown"])
    for index, line in enumerate(record["breakdown"]):
        assert form[f"cart[line_items][{index}][amount]"] == str(line["delta_minor"])
        assert form[f"cart[line_items][{index}][description]"] == line["text"]
        assert form[f"cart[line_items][{index}][quantity]"] == "1"
    assert f"cart[line_items][{count}][amount]" not in form
    assert not any(key.startswith("cart[tax]") for key in form)


def test_the_stripe_screen_puts_up_a_cart_or_takes_the_screen_down():
    posted = []
    screen = StripeCartScreen("tmr_test_reader", lambda path, form: posted.append((path, form)))
    screen.present_exit(priced_record())
    screen.present_exit({"status": "covered", "covered_by": ["garage_pass"]})
    screen.present_exit(None)
    assert [path for path, _ in posted] == [
        "/v1/terminal/readers/tmr_test_reader/set_reader_display",
        "/v1/terminal/readers/tmr_test_reader/cancel_action",
        "/v1/terminal/readers/tmr_test_reader/cancel_action",
    ]
    assert posted[0][1]["cart[total]"] == "500" and posted[1][1] == {} and posted[2][1] == {}


# ---------------------------------------------------------------------------
# through the lane: the record the decision carries, before the vend, then gone
# ---------------------------------------------------------------------------


def test_the_reader_is_handed_the_decisions_own_record_before_the_vend_and_cleared_after():
    platform = a_capped_platform(stays=[("s-9", "TRNS-9", ENTRY)])
    fake = FakePlatform()
    fake.reject_close_without_open = False
    vend = RecordingVendOutput()
    reader = RecordingReader(vend)
    controller, _ = exit_lane(a_cache(platform), "TRNS-9", reader=reader,
                              transport=PlatformTransport(fake), vend=vend)
    decision = controller.run_once()
    assert decision.should_vend and vend.vends
    first, *rest = reader.shown
    # ONE record: what the reader got IS the decision's record and the close's
    assert first == decision.exit_pricing.to_detail()
    close = [body for body in fake.closed if body.get("local_decision")]
    assert close and close[-1]["local_decision"] == first
    assert reader_module.cart_for(first)["total"] == close[-1]["local_decision"]["fee_minor"] == 500
    # BEFORE the barrier moved, and taken down once the car was through
    assert reader.vends_by_then[0] == 0, "the reader was shown the fee after the vend"
    assert rest == [None], reader.shown


def test_the_lane_hands_a_record_and_a_screen_that_is_not_a_cart_needs_no_lane_change():
    platform = a_capped_platform(stays=[("s-9", "TRNS-9", ENTRY)])
    reader = InputFirstReader()
    controller, _ = exit_lane(a_cache(platform), "TRNS-9", reader=reader)
    controller.run_once()
    assert reader.titles == ["500 USD", None], reader.titles


def test_an_exit_with_no_money_decision_clears_what_the_last_car_left_up():
    cache = a_cache(a_capped_platform(stays=[("s-9", "TRNS-9", ENTRY)]))
    reader = RecordingReader()
    controller, _ = exit_lane(cache, "NOBODY-1", reader=reader)
    controller.run_once()
    assert reader.shown and reader.shown[0] is not None
    assert reader.shown[0]["status"] == "no_cached_entry"
    assert reader_module.cart_for(reader.shown[0]) is None


def test_an_entry_lane_never_touches_a_reader():
    cache = DecisionCache()
    cache.load([], default_action="allow")
    reader = RecordingReader()
    controller = LaneController(
        a_config(direction="entry"),
        loop=SimulatedLoopInput(arrivals=1),
        camera=CannedCameraFeed(),
        vend=RecordingVendOutput(),
        identifier=StubVehicleIdentifier([VehicleIdentity(plate="IN-1", confidence=0.99)]),
        cache=cache,
        clock=lambda: NOW,
        reader=reader,
    )
    controller.run_once()
    assert reader.shown == []


def test_a_reader_that_raises_costs_its_screen_and_not_the_barrier():
    class Broken:
        def present_exit(self, record):
            raise ConnectionError("the reader is off the network")

    platform = a_capped_platform(stays=[("s-9", "TRNS-9", ENTRY)])
    fake = FakePlatform()
    fake.reject_close_without_open = False
    controller, vend = exit_lane(a_cache(platform), "TRNS-9", reader=Broken(),
                                 transport=PlatformTransport(fake))
    decision = controller.run_once()
    assert decision.should_vend and vend.vends
    assert any(body.get("local_decision") for body in fake.closed)


# ---------------------------------------------------------------------------
# under the runner: the reader's own thread, and the barrier does not wait on it
# ---------------------------------------------------------------------------


def test_under_the_runner_the_reader_is_shown_on_its_own_thread_and_the_vend_does_not_wait():
    release = threading.Event()

    class Slow(RecordingReader):
        def present_exit(self, record):
            super().present_exit(record)
            release.wait(5.0)

    platform = a_capped_platform(stays=[("s-9", "TRNS-9", ENTRY)])
    vend = RecordingVendOutput()
    reader = Slow(vend)
    controller, _ = exit_lane(a_cache(platform), "TRNS-9", reader=reader, vend=vend)
    runner = LaneRunner(controller, client=None, rules_refresh_s=300.0, stays_refresh_s=5.0)
    runner.start()
    try:
        assert "reader-screen" in {t.name for t in runner._threads}
        # the vend happens while the reader is still stuck showing the fee
        assert wait_for(lambda: vend.vends, seconds=2.0), "the barrier waited on the reader"
        assert wait_for(lambda: reader.shown, seconds=2.0)
        assert not release.is_set()
        assert set(reader.threads) == {"reader-screen"}, reader.threads
        release.set()
        # the clear handed over while the fee was up is shown after it, not instead
        assert wait_for(lambda: len(reader.shown) == 2, seconds=2.0), reader.shown
        assert reader.shown[0]["status"] == "priced" and reader.shown[1] is None
    finally:
        release.set()
        runner.stop(timeout=0.5)
    assert controller._reader_hand is None, "stop() left the hand-off installed"


def test_a_lane_with_no_reader_starts_no_reader_thread():
    controller, _ = exit_lane(a_cache(a_capped_platform()), "TRNS-9", reader=None)
    runner = LaneRunner(controller, client=None, rules_refresh_s=300.0, stays_refresh_s=5.0)
    runner.start()
    try:
        assert "reader-screen" not in {t.name for t in runner._threads}
    finally:
        runner.stop(timeout=0.5)


def test_the_newest_decision_wins_and_a_clear_never_erases_a_fee_nobody_has_seen():
    controller, _ = exit_lane(a_cache(a_capped_platform()), "TRNS-9", reader=RecordingReader())
    runner = LaneRunner(controller, client=None, rules_refresh_s=300.0, stays_refresh_s=5.0)
    # not started: what the reader thread would show next, filled before it reads
    runner.hand_to_reader({"status": "priced", "n": 1})
    runner.hand_to_reader({"status": "priced", "n": 2})
    assert runner._reader_pending == [{"status": "priced", "n": 2}], "an older fee survived"
    runner.hand_to_reader(None)
    assert runner._reader_pending == [{"status": "priced", "n": 2}, None], (
        "the clear erased a fee the reader never showed"
    )
    runner.hand_to_reader(None)
    assert runner._reader_pending == [{"status": "priced", "n": 2}, None]
    runner.hand_to_reader({"status": "covered"})
    assert runner._reader_pending == [{"status": "covered"}]
    json.dumps(runner._reader_pending)  # plain data: nothing but the records rides here
