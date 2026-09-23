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


__all__ = [
    "PRICED",
    "ReaderScreen",
    "StripeCartScreen",
    "cart_for",
    "cart_form",
]
