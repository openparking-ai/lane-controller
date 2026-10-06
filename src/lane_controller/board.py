"""The board: what this lane's screen may show while nothing else wants it.

HIS WORDS, 2026-10-06: "Gate agent needs to display special events/ pricing as
well ... display shows what is going on. Kind of like a message board." The
lane does not draw anything -- a screen beside it does, through
`GET /v1/lane/state` -- so this module answers one question: what is on the
board NOW, as items in the order they are shown.

TWO KINDS OF ITEM, and neither is anybody's free-typed price:

  message   the owner's own words, for this lane, inside the times the owner
            gave. The times are judged HERE, on this lane's clock, from what
            the cache holds -- so an event tonight goes up and comes down by
            itself, and offline the lane keeps to the times it was told.
  prices    what this lane would charge for a few lengths of stay entering
            NOW, worked out ON THE LANE with `exit_pricing.charge` -- the very
            call the exit charges with: the engine's quote on the cached plans
            and the garage's tax at the exit. A special-event rate in the plans
            shows during its window because the engine selects it, and the
            screen cannot show a price the lane would not charge, because it is
            the same computation. Only when the owner switched it on for this
            lane.

A CACHE TOO STALE TO PRICE SHOWS NO PRICE, never an old one: past the cache's
bound, or with no plans or no tax sets, there is no prices item at all. A
length the engine refuses to price is left out rather than shown as anything.

What wins the screen -- a ticket, a fee, a closed lane's message, the board --
is the screen's rule, not this one's: the board is published whatever the
closing, and gate-agent decides.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from rate_engine.currency import is_known, minor_unit_digits

from .decision import DecisionCache
from .exit_pricing import charge

#: THE LENGTHS OF STAY THE BOARD PRICES, in minutes: one, two and three hours,
#: and a whole day -- the day's most. A driver deciding whether to come in
#: wants the short stays and the ceiling; a longer list does not fit a screen
#: read through a windscreen.
BOARD_STAY_MINUTES: tuple[int, ...] = (60, 120, 180, 1440)


def _instant(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def in_force(message: dict, now: float) -> bool:
    """Whether one owner message is up at `now`: its start has come (or it has
    none) and its end has not (or it has none). A time this build cannot read
    keeps the message DOWN -- an event message shown outside its times is the
    owner's words at the wrong hour."""
    at = datetime.fromtimestamp(now, UTC)
    start, end = message.get("starts_at"), message.get("ends_at")
    start_at, end_at = _instant(start), _instant(end)
    if (start and start_at is None) or (end and end_at is None):
        return False
    if start_at is not None and at < start_at:
        return False
    if end_at is not None and at >= end_at:
        return False
    return True


def price_lines(cache: DecisionCache, now: float) -> list[dict]:
    """What this lane would charge for each of `BOARD_STAY_MINUTES`, entering
    at `now`, or `[]` when the cache cannot price at all."""
    if cache.is_stale(now=now) or not cache.plans or not cache.tax_sets:
        return []
    entry = datetime.fromtimestamp(now, UTC)
    lines = []
    for minutes in BOARD_STAY_MINUTES:
        status, quote, tax = charge(
            cache, entry.isoformat(), (entry + timedelta(minutes=minutes)).isoformat()
        )
        if status != 200:
            continue
        currency = quote["currency"]
        if not is_known(currency):
            continue
        lines.append({
            "minutes": minutes,
            "fee_minor": quote["fee_minor"] + tax["total_minor"],
            "currency": currency,
            "minor_unit_digits": minor_unit_digits(currency),
        })
    return lines


def board_items(cache: DecisionCache, *, now: float | None = None) -> list[dict]:
    """The board, in the order it is shown: every owner message in force, as
    the cache holds them, then the prices when the owner switched them on and
    at least one length priced."""
    current = time.time() if now is None else now
    items: list[dict] = [
        {"kind": "message", "text": message["text"]}
        for message in cache.board.get("messages") or []
        if in_force(message, current)
    ]
    if cache.board.get("prices") is True:
        lines = price_lines(cache, current)
        if lines:
            items.append({"kind": "prices", "lines": lines})
    return items


__all__ = ["BOARD_STAY_MINUTES", "board_items", "in_force", "price_lines"]
