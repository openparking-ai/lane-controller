"""The phone prompt at the reader, and the validation claimed when the phone is entered.

Brief §4 and amendment A1: ONE screen -- the fee and an optional phone number,
with Skip -- whose words name BOTH uses; the claim is made the moment a number
is entered, before the amount to pay is shown, so what the driver is shown is
already the discounted amount; the lane computes nothing (the line and the fee
after it are the platform's, appended and read); and the phone goes to the
claim call and nowhere else.

The records are the REAL `price_exit` with the engine in-process (the capped
plan: a zero line, two charges and a negative cap line, 500 in all). The
platform's answer is planted where a test needs one it could not otherwise get,
and a planted answer whose fee is not the record's fee minus the line is how a
READ figure is told from a computed one.

Amendment A2.2: the close carries what the reader showed (`reader_shown`),
sealed as the close is recorded, and nothing discounted goes up after that --
so the number on the reader and the number the platform records are the same
in every order.

`scripts/validation_prompt_fail_control.py` breaks each property and requires
this file to go red.
"""

from __future__ import annotations

import copy
import json
import logging
import re

from fake_platform import FakePlatform
from lane_controller import reader as reader_module
from lane_controller.platform_client import PlatformClient, PlatformUnreachable
from lane_controller.reader import (
    PROMPT_TEXT,
    ValidatingScreen,
    cart_for,
    claim_through,
    prompt_for,
    with_validation,
)
from lane_controller.sync import PlatformTransport
from test_exit_decision import ENTRY, a_cache
from test_reader_fee import RecordingReader, a_capped_platform, exit_lane, priced_record

PHONE = "(202) 555-0143"
DIGITS = "2025550143"


def holds_phone(text: str) -> bool:
    """The number, written any of the ways people write one."""
    return re.search(r"[\s().+-]{0,3}".join(DIGITS), text) is not None


def held_answer(record: dict, *, discount: int = 200, fee_after: int | None = None) -> dict:
    """The platform's answer for a held claim (platform `presentClaim`)."""
    return {
        "outcome": "held",
        "replay": False,
        "currency": record["currency"],
        "fee_before_minor": record["fee_minor"],
        "discount_minor": discount,
        "fee_minor": record["fee_minor"] - discount if fee_after is None else fee_after,
        "line": {"code": "validation", "rule_id": None, "delta_minor": -discount,
                 "text": "Validation from Invented Bistro (2.00 USD off): -2.00 USD"},
        "held_at": "2026-09-23T17:00:00.000Z",
    }


class Prompt:
    """The reader's input screen: shows the prompt, answers what the driver typed."""

    def __init__(self, typed: str | None) -> None:
        self.typed = typed
        self.asked: list[dict] = []

    def ask(self, prompt: dict) -> str | None:
        self.asked.append(copy.deepcopy(prompt))
        return self.typed


class Claims:
    """The platform's claim, recorded: what it was asked, and what it answers."""

    def __init__(self, answer=None, raises: Exception | None = None) -> None:
        self.answer = answer
        self.raises = raises
        self.asked: list[tuple[dict, str]] = []

    def __call__(self, record: dict, phone: str):
        self.asked.append((copy.deepcopy(record), phone))
        if self.raises is not None:
            raise self.raises
        return self.answer(record) if callable(self.answer) else self.answer


# -- the one screen --------------------------------------------------------------------------


def test_the_one_screen_is_the_fee_and_an_optional_phone_and_its_words_name_both_uses():
    record = priced_record()
    prompt = prompt_for(record)
    assert prompt["title"] == "Parking fee 5.00 USD"
    assert prompt["fee_minor"] == 500 and prompt["currency"] == "USD"
    assert prompt["skip"] == "Skip"
    assert prompt["text"] == PROMPT_TEXT
    # Both uses are named: a screen that only offered a receipt would be skipped
    # by a driver holding a validation.
    assert "text receipt" in PROMPT_TEXT and "validation" in PROMPT_TEXT
    assert "receipt?" not in PROMPT_TEXT


def test_no_fee_to_pay_is_no_prompt():
    record = priced_record()
    assert prompt_for(None) is None
    assert prompt_for({**record, "status": "covered"}) is None
    assert prompt_for({**record, "fee_minor": 0}) is None
    assert prompt_for({**record, "status": "no_cached_entry"}) is None
    assert prompt_for({**record, "session_id": None}) is None, "no stay to claim a validation for"


# -- entered, claimed, and the amount shown is already the discounted one -------------------


def test_a_phone_entered_is_claimed_first_and_the_amount_shown_is_the_discounted_one():
    record = priced_record()
    inner = RecordingReader()
    claims = Claims(held_answer)
    ValidatingScreen(inner, Prompt(PHONE), claims).present_exit(record)
    # Claimed with the number and the record on screen, once.
    assert claims.asked == [(record, PHONE)]
    # ONE amount to pay, and it is the discounted one: the platform's.
    assert len(inner.shown) == 1
    shown = inner.shown[0]
    cart = cart_for(shown)
    assert cart["total"] == 300
    line_text = held_answer(record)["line"]["text"]
    assert cart["line_items"][-1] == {"amount": -200, "description": line_text, "quantity": 1}
    engine = [line["delta_minor"] for line in record["breakdown"]]
    assert [i["amount"] for i in cart["line_items"][:-1]] == engine
    assert shown["validation"]["fee_before_minor"] == 500
    assert not holds_phone(json.dumps(shown))


def test_the_fee_shown_is_the_platforms_read_not_one_the_lane_worked_out():
    # A planted answer whose fee is NOT the record's fee minus the line: only a
    # screen that READS the platform's fee shows 111.
    record = priced_record()
    inner = RecordingReader()
    claims = Claims(lambda r: held_answer(r, fee_after=111))
    ValidatingScreen(inner, Prompt(PHONE), claims).present_exit(record)
    assert cart_for(inner.shown[0])["total"] == 111


def test_skip_goes_straight_on_asking_nothing():
    record = priced_record()
    inner = RecordingReader()
    claims = Claims(held_answer)
    ValidatingScreen(inner, Prompt(None), claims).present_exit(record)
    assert claims.asked == []
    assert inner.shown == [record]


def test_a_phone_that_matches_nothing_shows_the_fee_as_priced():
    record = priced_record()
    inner = RecordingReader()
    claims = Claims({"outcome": "not_validated", "reason": "none"})
    ValidatingScreen(inner, Prompt(PHONE), claims).present_exit(record)
    assert inner.shown == [record]
    assert cart_for(inner.shown[0])["total"] == 500


def test_a_platform_that_cannot_answer_shows_the_fee_as_priced_and_logs_no_number(caplog):
    record = priced_record()
    inner = RecordingReader()
    boom = PlatformUnreachable(f"HTTP 500 while asking about {PHONE}")
    with caplog.at_level(logging.DEBUG):
        ValidatingScreen(inner, Prompt(PHONE), Claims(raises=boom)).present_exit(record)
    assert inner.shown == [record]
    assert "could not be claimed" in caplog.text
    assert not holds_phone(caplog.text)


def test_an_answer_made_on_another_fee_is_not_put_up_in_place_of_this_one():
    record = priced_record()
    other = {**held_answer(record), "fee_before_minor": 750, "fee_minor": 550}
    assert with_validation(record, other) is None
    assert with_validation(record, {**held_answer(record), "currency": "EUR"}) is None
    assert with_validation(record, {**held_answer(record), "fee_minor": -1}) is None
    inner = RecordingReader()
    ValidatingScreen(inner, Prompt(PHONE), Claims(other)).present_exit(record)
    assert inner.shown == [record]


def test_a_record_with_nothing_to_pay_passes_through_with_no_prompt():
    inner = RecordingReader()
    prompt = Prompt(PHONE)
    claims = Claims(held_answer)
    screen = ValidatingScreen(inner, prompt, claims)
    screen.present_exit(None)
    screen.present_exit({**priced_record(), "status": "covered"})
    assert prompt.asked == [] and claims.asked == []
    assert inner.shown[0] is None and inner.shown[1]["status"] == "covered"


# -- the claim call ----------------------------------------------------------------------------


class _Response:
    def __init__(self, body: dict) -> None:
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_claim_goes_to_the_platform_with_the_number_and_the_decision_and_nothing_else():
    record = priced_record()
    sent = []

    def opener(request, timeout):
        sent.append(request)
        return _Response({"validation": held_answer(record)})

    client = PlatformClient("http://platform.invalid", "lane-token", opener=opener)
    answer = claim_through(client)(record, PHONE)
    assert answer == held_answer(record)
    (request,) = sent
    assert request.get_method() == "POST"
    assert request.full_url == "http://platform.invalid/api/v1/lane/sessions/s-9/validation"
    assert json.loads(request.data) == {"phone": PHONE, "local_decision": record}
    assert not holds_phone(request.full_url), "the number is in the body, never the URL"


# -- through the lane ----------------------------------------------------------------------------


def test_through_the_lane_the_close_carries_the_decision_as_priced_and_the_phone_is_nowhere(caplog):
    platform = a_capped_platform(stays=[("s-9", "TRNS-9", ENTRY)])
    fake = FakePlatform()
    fake.reject_close_without_open = False
    inner = RecordingReader()
    claims = Claims(held_answer)
    with caplog.at_level(logging.DEBUG):
        controller, _ = exit_lane(a_cache(platform), "TRNS-9",
                                  reader=ValidatingScreen(inner, Prompt(PHONE), claims),
                                  transport=PlatformTransport(fake))
        decision = controller.run_once()
    record = decision.exit_pricing.to_detail()
    # The claim was made on the decision's own record; the reader showed the discount.
    assert claims.asked == [(record, PHONE)]
    assert cart_for(inner.shown[0])["total"] == 300
    # The close carries the decision AS PRICED: the platform records the claim it
    # holds; the lane sends no discount and no phone.
    close = [body for body in fake.closed if body.get("local_decision")]
    assert close and close[-1]["local_decision"] == record
    assert close[-1]["local_decision"]["fee_minor"] == 500
    assert "phone" not in close[-1]
    # ...and what the reader SHOWED: the discounted fee, so the platform records it.
    assert close[-1]["reader_shown"] == {"fee_minor": 300, "currency": "USD"}
    everything = json.dumps(fake.closed) + json.dumps(
        [event.as_dict() for _, event in controller.events.since(0)]) + caplog.text
    assert not holds_phone(everything)


def test_the_seam_is_unchanged_for_a_screen_without_the_prompt():
    # The same lane with the P2 screen alone: the record, as before.
    platform = a_capped_platform(stays=[("s-9", "TRNS-9", ENTRY)])
    inner = RecordingReader()
    controller, _ = exit_lane(a_cache(platform), "TRNS-9", reader=inner)
    decision = controller.run_once()
    assert inner.shown[0] == decision.exit_pricing.to_detail()
    assert reader_module.cart_for(inner.shown[0])["total"] == 500


# -- A2.2: what the reader showed rides the close ------------------------------------------


def test_the_seal_is_what_went_up_discounted_priced_or_nothing():
    record = priced_record()
    screen = ValidatingScreen(RecordingReader(), Prompt(PHONE), Claims(held_answer))
    screen.present_exit(record)
    assert screen.seal("s-9") == {"fee_minor": 300, "currency": "USD"}
    skipped = ValidatingScreen(RecordingReader(), Prompt(None), Claims(held_answer))
    skipped.present_exit(record)
    assert skipped.seal("s-9") == {"fee_minor": 500, "currency": "USD"}
    unshown = ValidatingScreen(RecordingReader(), Prompt(PHONE), Claims(held_answer))
    assert unshown.seal("s-9") is None


def test_a_claim_the_lane_gave_up_on_is_sealed_as_the_fee_as_priced():
    # A slow platform: the lane's call times out, the fee goes up as priced, and
    # the close says so -- the platform then gives back the hold it made late.
    record = priced_record()
    inner = RecordingReader()
    gave_up = Claims(raises=PlatformUnreachable("timed out"))
    screen = ValidatingScreen(inner, Prompt(PHONE), gave_up)
    screen.present_exit(record)
    assert cart_for(inner.shown[0])["total"] == 500
    assert screen.seal("s-9") == {"fee_minor": 500, "currency": "USD"}


def test_nothing_discounted_goes_up_after_the_close_was_recorded():
    # The close is recorded while the claim is still in flight (the barrier did
    # not wait): the seal finds nothing, and the late answer is NOT put up.
    record = priced_record()
    inner = RecordingReader()
    sealed = []

    def late(r):
        sealed.append(screen.seal("s-9"))
        return held_answer(r)

    screen = ValidatingScreen(inner, Prompt(PHONE), Claims(late))
    screen.present_exit(record)
    assert sealed == [None], "the close said nothing was shown"
    assert inner.shown == [record], "and nothing discounted went up after it"


def test_the_transport_carries_reader_shown_only_when_there_is_one():
    platform = a_capped_platform(stays=[("s-9", "TRNS-9", ENTRY)])
    fake = FakePlatform()
    fake.reject_close_without_open = False
    controller, _ = exit_lane(a_cache(platform), "TRNS-9", reader=RecordingReader(),
                              transport=PlatformTransport(fake))
    controller.run_once()
    close = [body for body in fake.closed if body.get("local_decision")]
    assert close and "reader_shown" not in close[-1], "a screen that keeps nothing sends nothing"


def test_the_client_puts_reader_shown_on_the_close_body():
    sent = []

    def opener(request, timeout):
        sent.append(json.loads(request.data))
        return _Response({"session": {"exit_confirmation": "confirmed"}})

    client = PlatformClient("http://platform.invalid", "lane-token", opener=opener)
    at = "2026-09-23T17:00:00Z"
    client.close_session(event_id="e1", exit_at=at, exit_confirmation="confirmed",
                         plate="TRNS-9", reader_shown={"fee_minor": 300, "currency": "USD"})
    client.close_session(event_id="e2", exit_at=at, exit_confirmation="confirmed", plate="TRNS-9")
    assert sent[0]["reader_shown"] == {"fee_minor": 300, "currency": "USD"}
    assert "reader_shown" not in sent[1]
