"""`exit_fee` on `GET /v1/lane/state`: the reader's number, published for a second screen.

Each claim is paired with the case that would falsify it. The lane is the real
controller behind the real `LaneService`, read over a socket at the moment the
driver would be reading a screen -- while the reader is being handed the fee --
and the records are the real `price_exit` with the rate engine in-process,
except where a test plants one on purpose to prove a figure is READ.

`scripts/exit_fee_fail_control.py` breaks the publication in nine ways and
requires this file to go red each time.
"""

from __future__ import annotations

import copy
import json

from rate_engine.currency import MINOR_UNIT_DIGITS

from lane_consumer import LaneConsumer
from lane_controller import reader as reader_module
from lane_controller.interfaces import VehicleIdentity
from lane_controller.reader import ValidatingScreen, cart_for
from lane_controller.runner import LaneRunner
from lane_controller.service import LaneService, make_server
from lane_controller.simulated import RecordingVendOutput, SimulatedLoopInput
from serving import serving
from test_exit_decision import ENTRY, NOW, PLAN, a_cache, a_platform, gp_register, gp_row
from test_reader_fee import CAPPED, a_capped_platform, exit_lane, priced_record
from test_reader_validation import PHONE, Claims, Prompt, held_answer

FIELDS = {"decision_at", "status", "fee_minor", "currency", "minor_unit_digits"}


class ReadsTheState:
    """A reader screen that, as it is handed a record, asks the lane's read
    contract what it is publishing -- the moment a display beside it is drawn."""

    def __init__(self) -> None:
        self.consumer: LaneConsumer | None = None
        self.shown: list[dict | None] = []
        self.states: list[dict] = []

    def present_exit(self, record):
        self.shown.append(copy.deepcopy(record))
        self.states.append(self.consumer.state())


class PromptThatReads(Prompt):
    """The phone prompt, reading the published state while it is up."""

    def __init__(self, typed, reader: ReadsTheState) -> None:
        super().__init__(typed)
        self.reader = reader
        self.states: list[dict] = []

    def ask(self, prompt):
        self.states.append(self.reader.consumer.state())
        return super().ask(prompt)


class VendThatReads(RecordingVendOutput):
    """The barrier, reading the published state as it is told to move."""

    def __init__(self) -> None:
        super().__init__()
        self.consumer: LaneConsumer | None = None
        self.states: list[dict] = []

    def vend(self, reason):
        self.states.append(self.consumer.state())
        return super().vend(reason)


def served(controller):
    return serving(make_server(LaneService(controller), port=0))


def run_exit(cache, plate, reader=None, vend=None):
    controller, vend = exit_lane(cache, plate, reader=reader, vend=vend)
    with served(controller) as base:
        consumer = LaneConsumer(base)
        for hook in (reader, vend, getattr(reader, "_inner", None)):
            if hook is not None and hasattr(hook, "consumer"):
                hook.consumer = consumer
        decision = controller.run_once()
        after = consumer.state()
    return controller, decision, after


def currency_platform(currency: str):
    plan = copy.deepcopy(CAPPED)
    plan["plan_version"] = f"capped-{currency.lower()}-2026-01"
    plan["currency"] = currency
    platform = a_capped_platform(stays=[("s-9", "TRNS-9", ENTRY)])
    platform.rate_plans = [plan]
    rules = platform.get_rules

    def get_rules():
        payload = rules()
        payload["currency"] = currency
        return payload

    platform.get_rules = get_rules
    return platform


# ---------------------------------------------------------------------------
# the one value: what the reader is handed is what is published
# ---------------------------------------------------------------------------


def test_the_published_fee_is_the_readers_fee_while_the_reader_is_showing_it():
    reader = ReadsTheState()
    _, decision, _ = run_exit(a_cache(a_capped_platform(stays=[("s-9", "TRNS-9", ENTRY)])),
                              "TRNS-9", reader=reader)
    record = decision.exit_pricing.to_detail()
    state = reader.states[0]
    fee = state["exit_fee"]
    assert fee == {
        "decision_at": state["decision"]["at"],
        "status": "priced",
        "fee_minor": record["fee_minor"],
        "currency": "USD",
        "minor_unit_digits": 2,
    }
    assert fee["fee_minor"] == cart_for(reader.shown[0])["total"] == 500


def test_the_figure_is_read_even_when_it_is_not_the_sum_of_the_lines():
    """PLANTED: a record whose fee is not its lines' sum. Only a publication that
    READS the fee can put 777 out; one that added the lines up would say 900."""
    record = priced_record()
    record["fee_minor"] = 777
    assert sum(line["delta_minor"] for line in record["breakdown"]) != 777
    fee = reader_module.exit_fee_for("2026-06-10T18:00:00+00:00", record)
    assert fee["fee_minor"] == 777 == cart_for(record)["total"]


def test_a_held_validation_publishes_the_fee_the_reader_was_given_after_it():
    """The prompt is up: the fee as priced, which is the prompt's own figure.
    The validation is held: the discounted fee, which is the cart's total."""
    inner = ReadsTheState()
    prompt = PromptThatReads(PHONE, inner)
    screen = ValidatingScreen(inner, prompt, Claims(held_answer))
    _, decision, _ = run_exit(a_cache(a_capped_platform(stays=[("s-9", "TRNS-9", ENTRY)])),
                              "TRNS-9", reader=screen)
    assert decision.exit_pricing.to_detail()["fee_minor"] == 500
    assert prompt.states[0]["exit_fee"]["fee_minor"] == prompt.asked[0]["fee_minor"] == 500
    assert inner.states[0]["exit_fee"]["fee_minor"] == cart_for(inner.shown[0])["total"] == 300


def test_a_skipped_prompt_publishes_the_fee_as_priced():
    inner = ReadsTheState()
    screen = ValidatingScreen(inner, Prompt(None), Claims(held_answer))
    run_exit(a_cache(a_capped_platform(stays=[("s-9", "TRNS-9", ENTRY)])), "TRNS-9",
             reader=screen)
    assert inner.states[0]["exit_fee"]["fee_minor"] == cart_for(inner.shown[0])["total"] == 500


def run_exit_read_at_the_close(reader):
    """The exit, with the published state read INSIDE the close: the close's
    detail is built first -- `seal()` has run -- and the reader is not yet
    cleared. Returns (the state then, what the close sealed, the decision)."""
    controller, _ = exit_lane(a_cache(a_capped_platform(stays=[("s-9", "TRNS-9", ENTRY)])),
                              "TRNS-9", reader=reader)
    seen = {}
    record = controller.events.record

    def reading(kind, *args, **detail):
        if kind == "session_close":
            assert controller.exit_screen is not None, "the reader was cleared first"
            seen["state"] = consumer.state()
            seen["sealed"] = detail.get("reader_shown")
        return record(kind, *args, **detail)

    controller.events.record = reading
    with served(controller) as base:
        consumer = LaneConsumer(base)
        for hook in (reader, getattr(reader, "_inner", None)):
            if hook is not None and hasattr(hook, "consumer"):
                hook.consumer = consumer
        decision = controller.run_once()
    assert "state" in seen, "no close was recorded"
    return seen["state"], seen["sealed"], decision


def test_a_held_validation_is_still_the_published_fee_inside_the_close():
    """The close seals what the reader showed BEFORE the reader is cleared. In
    between, the published fee is still the discounted one the reader was
    given -- never the fee as priced, which the reader never showed."""
    inner = ReadsTheState()
    screen = ValidatingScreen(inner, Prompt(PHONE), Claims(held_answer))
    state, sealed, decision = run_exit_read_at_the_close(screen)
    assert decision.exit_pricing.to_detail()["fee_minor"] == 500
    assert sealed == {"fee_minor": 300, "currency": "USD"}, "the close sealed nothing"
    assert state["exit_fee"]["fee_minor"] == 300 == cart_for(inner.shown[0])["total"]


def test_without_a_validation_the_fee_inside_the_close_is_the_fee_as_priced():
    """The control for the one above: the same moment, nothing held."""
    inner = ReadsTheState()
    state, sealed, _ = run_exit_read_at_the_close(inner)
    assert sealed is None
    assert state["exit_fee"]["fee_minor"] == 500 == cart_for(inner.shown[0])["total"]


def test_a_seal_between_reading_the_screen_and_asking_it_changes_nothing():
    """The contract is served on its own thread. It reads what the reader was
    handed, then asks the screen what went up -- and the lane's thread can seal
    the stay between the two. The answer must be the same either side."""
    from lane_controller.service import LaneService

    inner = ReadsTheState()
    screen = ValidatingScreen(inner, Prompt(PHONE), Claims(held_answer))
    controller, _ = exit_lane(a_cache(a_capped_platform(stays=[("s-9", "TRNS-9", ENTRY)])),
                              "TRNS-9", reader=screen)
    record = priced_record()
    assert record["session_id"] == "s-9"
    controller.last_decision_at = "2026-06-10T18:00:00+00:00"
    with served(controller) as base:
        inner.consumer = LaneConsumer(base)
        controller.show_reader(record)
    service = LaneService(controller)
    assert service.exit_fee().fee_minor == 300, "the discount is not up"

    asked = screen.shown_for
    screen.shown_for = lambda handed: (screen.seal(handed["session_id"]), asked(handed))[1]
    fee = service.exit_fee()
    assert "s-9" in screen._sealed, "the seal did not land in between"
    assert fee.fee_minor == 300 == cart_for(inner.shown[0])["total"]


def test_a_closed_stay_presented_again_publishes_what_the_reader_is_given_again():
    """The same plate read at the exit again after its close, before the next
    rules sync -- the lane's cache still lists the stay -- is the same stay put
    in front of the reader a second time, priced again. Its close is recorded,
    so nothing discounted goes up for it: the prompt and the cart say the fee as
    priced, and so must the figure published beside them. The first
    presentation's discount belonged to the first presentation."""
    inner = ReadsTheState()
    prompt = PromptThatReads(PHONE, inner)
    screen = ValidatingScreen(inner, prompt, Claims(held_answer))
    controller, _ = exit_lane(a_cache(a_capped_platform(stays=[("s-9", "TRNS-9", ENTRY)])),
                              "TRNS-9", reader=screen)
    controller.loop = SimulatedLoopInput(arrivals=2)
    closes = []
    record = controller.events.record

    def reading(kind, *args, **detail):
        if kind == "session_close":
            closes.append((consumer.state(), detail.get("reader_shown")))
        return record(kind, *args, **detail)

    controller.events.record = reading
    with served(controller) as base:
        consumer = LaneConsumer(base)
        inner.consumer = consumer
        first = controller.run_once()
        second = controller.run_once()
        after = consumer.state()
    assert first.exit_pricing.session_id == second.exit_pricing.session_id == "s-9"
    assert second.exit_pricing.to_detail()["fee_minor"] == 500
    assert len(closes) == 2 and len(inner.shown) == 4, inner.shown
    # The first presentation: the discount, up to and inside its close.
    assert inner.states[0]["exit_fee"]["fee_minor"] == cart_for(inner.shown[0])["total"] == 300
    assert closes[0][1] == {"fee_minor": 300, "currency": "USD"}
    assert closes[0][0]["exit_fee"]["fee_minor"] == 300
    assert inner.shown[1] is None
    # The second: the fee as priced on the prompt, on the cart and inside its
    # close -- and the close says the reader was given nothing new.
    assert prompt.states[1]["exit_fee"]["fee_minor"] == prompt.asked[1]["fee_minor"] == 500
    assert inner.states[2]["exit_fee"]["fee_minor"] == cart_for(inner.shown[2])["total"] == 500
    assert closes[1][0]["exit_fee"]["fee_minor"] == 500
    assert closes[1][1] is None
    assert inner.shown[3] is None and after["exit_fee"] is None


def test_a_stay_presented_again_before_its_close_publishes_the_new_prompts_figure():
    """A second decision for the same stay with no close in between -- the car
    left the loop and came back -- puts the prompt up again with the fee as
    priced. Until something new goes up, that is what is in front of the
    driver, not the discount the first presentation put up."""
    inner = ReadsTheState()
    prompt = PromptThatReads(PHONE, inner)
    screen = ValidatingScreen(inner, prompt, Claims(held_answer))
    controller, _ = exit_lane(a_cache(a_capped_platform(stays=[("s-9", "TRNS-9", ENTRY)])),
                              "TRNS-9", reader=screen)
    controller.last_decision_at = "2026-06-10T18:00:00+00:00"
    with served(controller) as base:
        inner.consumer = LaneConsumer(base)
        controller.show_reader(priced_record())
        prompt.typed = None
        controller.show_reader(priced_record())
    assert inner.states[0]["exit_fee"]["fee_minor"] == cart_for(inner.shown[0])["total"] == 300
    assert prompt.states[1]["exit_fee"]["fee_minor"] == prompt.asked[1]["fee_minor"] == 500
    assert inner.states[1]["exit_fee"]["fee_minor"] == cart_for(inner.shown[1])["total"] == 500


def test_under_the_runner_the_reader_thread_publishes_the_discount_it_put_up():
    """`LaneRunner` hands the reader the record on a thread of its own. What
    that thread puts up is what is published: the hand-off carries the
    presentation the lane made, not a copy of it."""
    inner = ReadsTheState()
    screen = ValidatingScreen(inner, Prompt(PHONE), Claims(held_answer))
    controller, _ = exit_lane(a_cache(a_capped_platform(stays=[("s-9", "TRNS-9", ENTRY)])),
                              "TRNS-9", reader=screen)
    runner = LaneRunner(controller, client=None, rules_refresh_s=300.0, stays_refresh_s=5.0)
    controller.set_reader_hand(runner.hand_to_reader)
    controller.last_decision_at = "2026-06-10T18:00:00+00:00"
    with served(controller) as base:
        inner.consumer = LaneConsumer(base)
        controller.show_reader(priced_record())
        (handed,) = runner._reader_pending
        runner.reader_turn(handed)
    assert runner.state.reader_turn_errors == 0
    assert inner.states[0]["exit_fee"]["fee_minor"] == cart_for(inner.shown[0])["total"] == 300


def test_a_stay_handed_over_with_no_cart_publishes_none_of_what_went_up_before():
    """A discount went up for the stay; the lane then hands the reader a record
    for the same stay that it shows no cart for -- a priced zero. The figure the
    earlier hand-over put up is not this one's: the zero is published."""
    inner = ReadsTheState()
    screen = ValidatingScreen(inner, Prompt(PHONE), Claims(held_answer))
    controller, _ = exit_lane(a_cache(a_capped_platform(stays=[("s-9", "TRNS-9", ENTRY)])),
                              "TRNS-9", reader=screen)
    controller.last_decision_at = "2026-06-10T18:00:00+00:00"
    with served(controller) as base:
        inner.consumer = LaneConsumer(base)
        controller.show_reader(priced_record())
        zero = {**priced_record(), "fee_minor": 0}
        controller.show_reader(zero)
    assert inner.states[0]["exit_fee"]["fee_minor"] == 300
    assert cart_for(inner.shown[1]) is None
    assert inner.states[1]["exit_fee"]["fee_minor"] == 0


def test_the_hand_over_up_now_is_never_the_one_the_screen_forgets():
    """The screen remembers a bounded number of stays. The one the lane is
    showing now is always among them, even when that stay was put up long ago
    and many others since."""
    class Blank:
        def present_exit(self, record):
            pass

    screen = ValidatingScreen(Blank(), Prompt(None), Claims(held_answer))
    screen.present_exit(priced_record())
    others = [{**priced_record(), "session_id": f"s-other-{n}"}
              for n in range(ValidatingScreen.REMEMBERED)]
    for other in others[:-1]:
        screen.present_exit(other)
    again = priced_record()
    screen.present_exit(again)
    screen.present_exit(others[-1])
    assert screen.shown_for(again) == {"fee_minor": 500, "currency": "USD"}
    assert screen.shown_for(others[0]) is None, "nothing was forgotten"
    assert screen.shown_for(others[1]) == {"fee_minor": 500, "currency": "USD"}


def test_an_exit_with_no_reader_still_publishes_its_fee():
    vend = VendThatReads()
    run_exit(a_cache(a_capped_platform(stays=[("s-9", "TRNS-9", ENTRY)])), "TRNS-9",
             reader=None, vend=vend)
    assert vend.states[0]["exit_fee"]["fee_minor"] == 500


# ---------------------------------------------------------------------------
# when there is nothing to show, nothing is published
# ---------------------------------------------------------------------------


def test_the_fee_comes_down_when_the_close_is_recorded():
    reader = ReadsTheState()
    _, _, after = run_exit(a_cache(a_capped_platform(stays=[("s-9", "TRNS-9", ENTRY)])),
                           "TRNS-9", reader=reader)
    assert reader.states[0]["exit_fee"] is not None
    assert reader.shown[-1] is None
    assert after["exit_fee"] is None, "the next driver would read this one's fee"


def test_an_entry_lane_publishes_no_fee():
    from lane_controller import LaneController
    from lane_controller.events import EventQueue
    from lane_controller.simulated import (
        CannedCameraFeed,
        SimulatedLoopInput,
        StubVehicleIdentifier,
    )
    from test_exit_decision import a_config

    vend = VendThatReads()
    controller = LaneController(
        a_config(direction="entry"),
        loop=SimulatedLoopInput(arrivals=1),
        camera=CannedCameraFeed(),
        vend=vend,
        identifier=StubVehicleIdentifier([VehicleIdentity(plate="TRNS-9", confidence=0.99)]),
        cache=a_cache(a_platform()),
        events=EventQueue(),
        clock=lambda: NOW,
        reader=ReadsTheState(),
    )
    with served(controller) as base:
        vend.consumer = LaneConsumer(base)
        controller.run_once()
    assert vend.states and vend.states[0]["exit_fee"] is None


def test_every_state_with_no_payment_carries_its_status_and_no_figure():
    platform = a_capped_platform(stays=[("s-9", "TRNS-9", ENTRY)],
                                 gp=gp_register(gp_row("PASS-1")))
    at = "2026-06-10T18:00:00+00:00"
    from lane_controller.exit_pricing import price_exit

    cache = a_cache(platform)
    covered = price_exit("PASS-1", cache, now=NOW).to_detail()
    unknown = price_exit("NOBODY-1", cache, now=NOW).to_detail()
    for record, status in ((covered, "covered"), (unknown, "no_cached_entry"),
                           ({"status": "engine_refused"}, "engine_refused"),
                           ({"status": "stale_facts"}, "stale_facts")):
        published = reader_module.exit_fee_for(at, record)
        assert published == {"decision_at": at, "status": status}, status


def test_a_priced_zero_is_published_as_zero_and_the_reader_shows_nothing():
    record = priced_record()
    record["fee_minor"] = 0
    fee = reader_module.exit_fee_for("2026-06-10T18:00:00+00:00", record)
    assert cart_for(record) is None
    assert fee["fee_minor"] == 0 and fee["minor_unit_digits"] == 2


def test_a_priced_record_the_reader_has_no_cart_for_carries_no_figure():
    """Both screens refuse the same way: the reader shows nothing, and no number
    is published for another screen to show instead."""
    at = "2026-06-10T18:00:00+00:00"
    for plant in ({"breakdown": []}, {"breakdown": [{"delta_minor": 500}]},
                  {"fee_minor": "500"}, {"fee_minor": True}, {"currency": ""}):
        record = {**priced_record(), **plant}
        assert cart_for(record) is None, plant
        published = reader_module.exit_fee_for(at, record)
        assert published == {"decision_at": at, "status": "priced"}, plant


# ---------------------------------------------------------------------------
# the currency's digits are the engine's
# ---------------------------------------------------------------------------


def test_the_digits_are_the_engines_for_the_currency_it_priced_in():
    """SEK is the currency two gates measured the display refusing; JPY has no
    minor unit and KWD has three. Each priced by the real engine through the lane."""
    for currency in ("SEK", "JPY", "KWD"):
        reader = ReadsTheState()
        _, decision, _ = run_exit(a_cache(currency_platform(currency)), "TRNS-9", reader=reader)
        record = decision.exit_pricing.to_detail()
        assert record["status"] == "priced" and record["currency"] == currency, record
        fee = reader.states[0]["exit_fee"]
        assert fee["minor_unit_digits"] == MINOR_UNIT_DIGITS[currency], currency
        assert fee["fee_minor"] == cart_for(reader.shown[0])["total"] == record["fee_minor"]


def test_nothing_that_names_the_car_or_the_stay_is_published():
    reader = ReadsTheState()
    run_exit(a_cache(a_capped_platform(stays=[("s-9", "TRNS-9", ENTRY)])), "TRNS-9",
             reader=reader)
    fee = reader.states[0]["exit_fee"]
    assert set(fee) == FIELDS
    wire = json.dumps(fee)
    for leak in ("TRNS-9", "s-9", "session", "matched", "covered_by", CAPPED["plan_version"],
                 PLAN["plan_version"]):
        assert leak not in wire, leak
