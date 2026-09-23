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
from collections.abc import Callable
from typing import Protocol

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
# GOKHAN'S SETTLED DESIGN, 2026-09-23 (brief §4, amendment A1). ONE SCREEN: the
# fee, and an optional phone number, together -- no second screen, no back
# button. The words name BOTH uses, because a driver who skips a screen that
# only offered a receipt has thrown away a validation. Skip goes straight on.
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
# WHAT IS NOT HERE: the reader's own input action. Collecting a typed number is
# a reader action like the cart, and whoever holds the key that can drive a
# reader holds the key that can charge on it; so `PhonePrompt` is handed in,
# as `post` is to the cart, and this package supplies no transport.

#: The words, as settled. Not "receipt?": the validation is named too.
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

    def __init__(
        self,
        inner: ReaderScreen,
        prompt: PhonePrompt,
        claim: Callable[[dict, str], dict | None],
    ) -> None:
        self._inner = inner
        self._prompt = prompt
        self._claim = claim

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
