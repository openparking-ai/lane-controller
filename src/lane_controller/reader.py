"""The card reader's screen at an exit: what it shows the driver, from the exit's own decision.

**THE READER IS HANDED THE DECISION, NOT A PICTURE OF IT.** At an exit the lane
decides about money before the barrier moves -- `exit_pricing.price_exit`, the
one computation -- and the record of that decision (`ExitPricing.to_detail()`)
is what travels: on the `decision` event, on the close as `local_decision`,
and, from here, to the reader. The platform's close writes that record's fee
onto the row (0017), so the number on the reader, the number on the row and
the number the barrier display is drawn from are one value from one
computation. **Nothing here computes money.** A total that was re-summed from
the lines would be a second answer to the question the engine already
answered, and it would agree right up to the day it did not.

**WHAT THE DRIVER SEES FIRST AT THE READER IS THE SCREEN'S CHOICE, NOT THE
LANE'S.** The lane calls `ReaderScreen.present_exit(record)` with the whole
record, or `None` when there is nothing for this reader to show; it never
builds a cart and never names one. A cart is ONE way to present that record
(`StripeCartScreen`). A screen that collects an input first -- the fee in its
title, and an optional phone number -- is another, and it plugs into the same
seam with no change to the lane: `tests/test_reader_fee.py` holds that with a
screen that is not a cart.

**The cart is DISPLAY-ONLY.** It is what the driver reads. Nothing charges from
it and nothing in this package can charge at all.

**What goes on the cart, and what does not:**

  * the line items are the engine's breakdown AS IT IS STORED on the record --
    each line's `delta_minor` as its amount and its `text` as its description,
    quantity one, in the engine's order. Not re-summed, not re-worded, not
    filtered: a zero line (the stay itself) and a negative one (a cap) are the
    engine's ledger too;
  * the total is the record's `fee_minor`, read, never added up;
  * no `tax`: the engine states none, and a figure nobody computed is not put
    on a screen;
  * the currency is the record's, lower-cased because the API takes it so.

**Every state with no payment clears the reader** -- covered, a priced stay of
zero, the engine's refusal, a stay the lane could not price here, a record this
module cannot read. The barrier display is what tells the driver which of
those it is (`gate-agent`'s fee frame); a reader holding the previous car's
cart would be telling them something false.

**WHAT IS NOT HERE.** No HTTP client, no Stripe library, no key and no reader
id from any configuration: `StripeCartScreen` is handed the function that posts
a form (`post`), and this package builds none. Whoever holds the key that can
DISPLAY on a reader holds the key that can CHARGE on it, and where that key
lives is decided with the charge, not here.

**Verified against the API reference on 2026-09-23**
(`docs.stripe.com/api/terminal/readers/set_reader_display`): `type=cart`,
`cart[currency]` (required, lower case), `cart[line_items][]` (required; each
`amount`, `description`, `quantity`, all required), `cart[total]` (required),
`cart[tax]` (optional). **Not stated there, and not known here:** whether a
line item may carry a zero or a negative amount, and how long a description may
be. Both occur in the engine's ledger. No live call was made to find out.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Protocol

from rate_engine.currency import is_known, minor_unit_digits

log = logging.getLogger(__name__)

#: The record's status for a stay the lane priced (`exit_pricing.PRICED`).
#: Spelled here rather than imported so this module reads the record by its
#: published words, as any other reader of it would.
PRICED = "priced"


class ReaderScreen(Protocol):
    """One card reader's screen, as the lane sees it: something it hands a decision to.

    `record` is the exit decision exactly as `ExitPricing.to_detail()` wrote it,
    or `None` when this reader has nothing to show -- which is also what is
    handed once the car has left, so the next driver never reads the last one's
    fee. What appears, and in what order, is the implementation's business.
    """

    def present_exit(self, record: dict | None) -> None: ...


def cart_for(record: dict | None) -> dict | None:
    """The cart for this record, or `None` when there is nothing to pay here.

    `None` for every status but `priced`, for a priced fee of zero, and for a
    record whose fee, currency or lines are not the shape the lane writes -- a
    line this cannot show AS STORED is not re-worded into one it can.
    """
    if not isinstance(record, dict) or record.get("status") != PRICED:
        return None
    fee = record.get("fee_minor")
    currency = record.get("currency")
    breakdown = record.get("breakdown")
    if isinstance(fee, bool) or not isinstance(fee, int) or fee <= 0:
        return None
    if not isinstance(currency, str) or not currency:
        return None
    if not isinstance(breakdown, list) or not breakdown:
        return None
    items = []
    for line in breakdown:
        if not isinstance(line, dict):
            return None
        amount, text = line.get("delta_minor"), line.get("text")
        if isinstance(amount, bool) or not isinstance(amount, int):
            return None
        if not isinstance(text, str) or not text:
            return None
        items.append({"amount": amount, "description": text, "quantity": 1})
    return {"currency": currency.lower(), "line_items": items, "total": fee}


def exit_fee_for(decision_at: str, record: dict | None, shown: dict | None = None) -> dict | None:
    """`GET /v1/lane/state`'s `exit_fee` for what the reader was handed, or None.

    `record` is what `show_reader()` last handed over and `shown` is what a
    validating screen then put up for that same record (`shown_for`), if anything.
    The figure is READ -- the record's `fee_minor`, or the amount the screen put
    up after a held validation, in the same currency -- and never added up.
    A figure is published only where the reader has one to show (`cart_for`),
    or where it is a priced zero; anything else carries its status alone.
    """
    if not isinstance(record, dict):
        return None
    status = record.get("status")
    if not isinstance(status, str) or not status:
        return None
    fee = {"decision_at": decision_at, "status": status}
    if status != PRICED:
        return fee
    amount, currency = record.get("fee_minor"), record.get("currency")
    zero = isinstance(amount, int) and not isinstance(amount, bool) and amount == 0
    if not zero and cart_for(record) is None:
        return fee
    if not is_known(currency):
        return fee
    if isinstance(shown, dict) and shown.get("currency") == currency:
        held = shown.get("fee_minor")
        if isinstance(held, int) and not isinstance(held, bool) and held >= 0:
            amount = held
    return {**fee, "fee_minor": amount, "currency": currency,
            "minor_unit_digits": minor_unit_digits(currency)}


def cart_form(cart: dict) -> dict[str, str]:
    """The cart as the form `set_reader_display` takes: `cart[line_items][0][amount]=...`."""
    form = {"type": "cart", "cart[currency]": cart["currency"]}
    for index, item in enumerate(cart["line_items"]):
        for key in ("amount", "description", "quantity"):
            form[f"cart[line_items][{index}][{key}]"] = str(item[key])
    form["cart[total]"] = str(cart["total"])
    return form


class StripeCartScreen:
    """A server-driven reader showing the fee as a cart.

    `post(path, form)` sends one form to the API and is supplied by whoever
    holds the key; this package supplies none. A record with a cart puts it up
    (`set_reader_display`); any other record, and `None`, takes down whatever
    the reader is showing (`cancel_action`).
    """

    def __init__(self, reader_id: str, post: Callable[[str, dict[str, str]], object]) -> None:
        if not reader_id:
            raise ValueError("a reader id is required: the cart has to go to one reader")
        self.reader_id = reader_id
        self._post = post

    def present_exit(self, record: dict | None) -> None:
        cart = cart_for(record)
        base = f"/v1/terminal/readers/{self.reader_id}"
        if cart is None:
            self._post(f"{base}/cancel_action", {})
        else:
            self._post(f"{base}/set_reader_display", cart_form(cart))


# -- the phone prompt, and the validation claimed when the phone is entered -----------------
#
# THE SETTLED DESIGN (amendment A1). ONE SCREEN: the fee, and an optional
# phone number, together -- no second screen, no back button. The words
# (`PROMPT_TEXT`) name BOTH uses, a receipt and a validation, and a test holds
# that they do. Skip goes straight on.
#
# THE CLAIM IS MADE WHEN THE PHONE IS ENTERED, before anything is shown as the
# amount to pay: the lane hands the number and the decision on screen to the
# platform (`PlatformClient.claim_validation`), which claims a live validation
# for this stay ON THAT FEE and holds it; what comes back -- the line, and the
# fee after it -- is what the driver is shown next. ONE NUMBER, NO CORRECTION
# AFTER: the amount the driver is shown is already the discounted one, and the
# close records the claim the platform holds. The lane computes nothing: the
# line and the fee are the platform's, appended and read as they came.
#
# THE PHONE IS NEVER KEPT HERE. It goes from the prompt to the claim call and
# nowhere else: not onto the record, not onto an event, not into the outbox,
# not into a log line -- a failure is logged without it.
#
# WHAT THE READER SHOWED RIDES THE CLOSE (amendment A2.2). The screen keeps,
# per stay, the amount it handed to the reader, and the lane SEALS it when it
# records that stay's close (`seal`): the close carries what was sealed as
# `reader_shown`, and the platform records a held validation only when that is
# the discounted fee. Sealing is one step under one lock with putting a figure
# up, so either the discount went up first and the close says so, or the close
# was recorded first and the screen puts up the fee as priced -- the number on
# the reader and the number on the row are the same in every order. A claim
# the lane gave up on (a slow platform) shows the fee as priced, the close
# says so, and the platform gives the hold back.
#
# WHAT IS NOT HERE: the reader's own input action. Collecting a typed number is
# a reader action like the cart, and whoever holds the key that can drive a
# reader holds the key that can charge on it; so `PhonePrompt` is handed in,
# as `post` is to the cart, and this package supplies no transport.

#: The words, as settled. Both uses are named, not the receipt alone.
PROMPT_TEXT = "Enter your phone number for a text receipt, or to use a restaurant validation."
SKIP_TEXT = "Skip"

#: Minor units per major, for the TITLE ONLY -- display, never arithmetic. A
#: currency not listed here is shown by its code and minor units, not guessed.
_EXPONENT = {"USD": 2, "EUR": 2, "GBP": 2, "CAD": 2, "AUD": 2, "JPY": 0}


def _shown(fee_minor: int, currency: str) -> str:
    exponent = _EXPONENT.get(currency.upper())
    if exponent is None:
        return f"{fee_minor} {currency.upper()} minor units"
    if exponent == 0:
        return f"{fee_minor} {currency.upper()}"
    whole, part = divmod(fee_minor, 10**exponent)
    return f"{whole}.{part:0{exponent}d} {currency.upper()}"


def prompt_for(record: dict | None) -> dict | None:
    """The one screen for this record -- the fee and the optional phone -- or
    `None` when there is nothing to pay here, or no stay to claim a validation
    for: exactly the records `cart_for` shows no cart for, and a priced record
    that names no session."""
    if cart_for(record) is None or not record.get("session_id"):
        return None
    return {
        "title": f"Parking fee {_shown(record['fee_minor'], record['currency'])}",
        "text": PROMPT_TEXT,
        "skip": SKIP_TEXT,
        "fee_minor": record["fee_minor"],
        "currency": record["currency"],
    }


class PhonePrompt(Protocol):
    """The reader's input screen: shows `prompt` and returns the number the
    driver entered, or `None` when they pressed Skip (or did not answer)."""

    def ask(self, prompt: dict) -> str | None: ...


def with_validation(record: dict, answer: dict | None) -> dict | None:
    """The record as the driver is shown it once a validation is HELD for the
    stay: the platform's line appended to the ledger AS IT CAME and the fee the
    PLATFORM'S fee after it, read. `None` when the answer is anything else, or
    was made on a fee other than this record's -- a number this screen did not
    show is not put up in its place."""
    if not isinstance(answer, dict) or answer.get("outcome") != "held":
        return None
    line, fee = answer.get("line"), answer.get("fee_minor")
    if answer.get("fee_before_minor") != record.get("fee_minor"):
        return None
    if answer.get("currency") != record.get("currency"):
        return None
    if not isinstance(line, dict) or isinstance(fee, bool) or not isinstance(fee, int) or fee < 0:
        return None
    return {
        **record,
        "fee_minor": fee,
        "breakdown": [*record["breakdown"], line],
        "validation": {
            "held": True,
            "fee_before_minor": record["fee_minor"],
            "discount_minor": answer.get("discount_minor"),
        },
    }


class ValidatingScreen:
    """The reader at an exit with the phone prompt in front of what it shows.

    For a record with a fee to pay: the prompt (`prompt_for`), then -- when a
    number was entered -- the claim (`claim(record, phone)`, the platform's
    answer), then the record the driver pays from handed to `inner`: the
    discounted one when a validation is held, the record as priced otherwise
    (skipped, nothing live, the platform unreachable). Any other record goes to
    `inner` untouched, as it did before this existed.
    """

    #: How many stays' shown amounts are remembered: far more than one exit
    #: has in flight, so a close always finds its own.
    REMEMBERED = 256

    def __init__(
        self,
        inner: ReaderScreen,
        prompt: PhonePrompt,
        claim: Callable[[dict, str], dict | None],
    ) -> None:
        self._inner = inner
        self._prompt = prompt
        self._claim = claim
        self._lock = threading.Lock()
        #: session id -> (the record the lane handed over, the `{"fee_minor",
        #: "currency"}` put up for it). THE ONLY COPY of a figure this screen
        #: gave the reader, and it belongs to that one hand-over: see `shown_for`.
        self._shown: OrderedDict[str, tuple[dict, dict]] = OrderedDict()
        #: The stays whose close is recorded: nothing discounted goes up for them.
        self._sealed: OrderedDict[str, None] = OrderedDict()

    def seal(self, session_id: str | None) -> dict | None:
        """What this screen put up for `session_id`, `{fee_minor, currency}`, or
        None when it put up nothing for that stay -- and from now on nothing
        discounted goes up for it. Called by the lane as it records the close."""
        if not session_id:
            return None
        with self._lock:
            if session_id in self._sealed:
                # Sealed once: a later close of the same stay was given nothing new.
                return None
            self._sealed[session_id] = None
            while len(self._sealed) > self.REMEMBERED:
                self._sealed.popitem(last=False)
            # READ, not dropped: what went up stays what `shown_for` answers
            # for as long as the lane is still showing that hand-over.
            kept = self._shown.get(session_id)
            return None if kept is None else dict(kept[1])

    def shown_for(self, record: dict | None) -> dict | None:
        """What this screen put up for `record` -- the record the lane handed
        it, as the lane holds it -- `{fee_minor, currency}`, or None. READ ONLY:
        unlike `seal`, it changes nothing. The lane's read contract asks it, so
        a display beside this reader draws the amount the reader was given and
        not the fee as priced when a validation is held.

        THE FIGURE'S LIFETIME IS THE HAND-OVER'S, and this is the one place it
        is decided. A figure is answered for the very record it went up for,
        and for no other -- not another hand-over of the same stay, however
        alike. So it exists from the moment it is registered to go up until the
        lane stops showing that record: sealing the close takes nothing away
        from it, and once the lane hands over anything else -- `None` after the
        close, or the same stay again, priced again -- it is never answered."""
        session_id = record.get("session_id") if isinstance(record, dict) else None
        if not session_id:
            return None
        with self._lock:
            kept = self._shown.get(session_id)
            if kept is None or kept[0] is not record:
                return None
            return dict(kept[1])

    def present_exit(self, record: dict | None) -> None:
        prompt = prompt_for(record)
        if prompt is None:
            self._inner.present_exit(record)
            return
        shown = record
        phone = self._prompt.ask(prompt)
        if phone:
            try:
                answer = self._claim(record, phone)
            except Exception as err:  # noqa: BLE001 -- the fee as priced is still shown
                # Named by its class, never with the number: the phone does not
                # reach a log line even on the failure path.
                log.warning(
                    "the validation could not be claimed (%s); the fee is shown as priced",
                    type(err).__name__,
                )
                answer = None
            shown = with_validation(record, answer) or record
        with self._lock:
            session_id = record["session_id"]
            if session_id in self._sealed:
                # The close is already recorded and said what it said: nothing
                # discounted goes up after it.
                shown = record
            self._shown[session_id] = (
                record, {"fee_minor": shown["fee_minor"], "currency": shown["currency"]},
            )
            # The newest hand-over is the last one forgotten, even for a stay
            # this screen has put something up for before.
            self._shown.move_to_end(session_id)
            while len(self._shown) > self.REMEMBERED:
                self._shown.popitem(last=False)
        # Outside the lock: what was registered is exactly what goes up, and a
        # reader on the network never holds the lane's close.
        self._inner.present_exit(shown)


def claim_through(client) -> Callable[[dict, str], dict | None]:
    """The claim `ValidatingScreen` takes, through a `PlatformClient`: the
    record's own session and the record itself, as the reader showed it."""

    def claim(record: dict, phone: str) -> dict | None:
        return client.claim_validation(record["session_id"], phone, record)

    return claim


__all__ = [
    "PRICED",
    "PROMPT_TEXT",
    "SKIP_TEXT",
    "PhonePrompt",
    "ReaderScreen",
    "StripeCartScreen",
    "ValidatingScreen",
    "cart_for",
    "cart_form",
    "claim_through",
    "prompt_for",
    "with_validation",
]
