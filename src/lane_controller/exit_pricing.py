"""The exit's local decision: covered from the cache, or priced on the box.

HIS REQUIREMENT, 2026-09-21: "we must identify the car and display the fee in
about a second. if no fee (garage pass, monthly) open the gate in about a
second." Until this existed the exit vended and the platform priced the stay
afterwards, from the close: the fee was computed after the car had gone, and
nothing on the lane could have shown it. Now the lane answers, from what it
already holds, before the barrier moves -- and NOTHING ON THIS PATH OPENS A
SOCKET TO THE PLATFORM. The cache is memory; the engine is in-process. The
one socket an exit read opens is to Vehicle ID on loopback, and
`tests/test_exit_decision.py` counts every connection the barrier's path makes
to prove that sentence rather than state it.

THREE ANSWERS, AND THE THIRD IS PRODUCT-VISIBLE (brief 4.5, 4.6):

  covered            a register row names this identity at this garage, on a
                     pass or agreement that stands TODAY, by the garage's own
                     local day. No fee. Which module and which row said so is
                     on the record.
  priced             no register covers it, and the garage's OPEN STAYS hold
                     an entry for it: `rate_engine.contract.run_quote` prices
                     the stay from the cached plans -- the same one function
                     behind the platform's /v1/quote, so this number and the
                     platform's are one computation.
  no_cached_entry    no register covers it and the cache holds no entry for
                     it: it entered inside one refresh interval, or during an
                     outage, or at a lane whose events have not propagated.
                     IT CANNOT BE PRICED AT THE BARRIER. The barrier opens as
                     it always has, the platform prices the stay at the close,
                     and nothing is collected at the reader -- on the record,
                     never silent. The stays interval in `config.py` is the
                     size of this class in ordinary operation.

...and two refusals of the engine's own, kept verbatim: `engine_refused`
(422: the plans cannot price this stay -- the close's unpriced backstop, 0013,
still catches it) and `engine_invalid` (400: a request this lane built wrong,
which is a bug and is said as one).

THE CLOCK IS THE GARAGE'S. Both register verbs are written to be read with
the reader's own day (garage-pass G27, monthly-billing G48: "the reader
compares the days with its own"), and the lane's day is the garage's local
day from the timezone the payload names -- never UTC's, which is a different
day for six hours out of every twenty-four.

THIS DECIDES WHAT THE BARRIER SHOWS AND WHETHER IT ASKS FOR MONEY, AND SINCE
PLATFORM 0017 IT DECIDES THE RECORD TOO. The answer travels on the close as
`local_decision`, and a close the platform can CONSUME -- `covered`, or
`priced` for this stay, in this stay's currency and space class, on a plan
version the garage holds -- is written as the fee with NEITHER MODULE'S DOOR
ASKED AND NO ENGINE CALL.

THE DOORS AND THE ENGINE ARE FOR THE PATHS THAT ARE NOT CONSUMED, and they
are every other one: a close carrying no decision at all; a `priced` decision
that names another session, another currency, another space class or a plan
version the garage does not hold; and the four this file can answer when it
cannot price -- `no_cached_entry`, `stale_facts`, `engine_refused`,
`engine_invalid`. Each of those consults both linked modules through their own
doors (0015) and prices through the engine (0013), and the decision it did not
take is kept on the row with the reason.

WHAT LOOKS AT A CONSUMED DECISION AFTERWARDS IS 0018's RECONCILER, out of
band, correcting nothing: a `priced` row is re-derived through the engine FROM
THE INPUTS THE LANE ITSELF STORED, and a `covered` row is recorded `covered`
and listed -- never re-consulted against the modules. Nothing anywhere
compares a covered close with what the modules would have said.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rate_engine.contract import run_quote

from .decision import DecisionCache

COVERED = "covered"
PRICED = "priced"
NO_CACHED_ENTRY = "no_cached_entry"
ENGINE_REFUSED = "engine_refused"
ENGINE_INVALID = "engine_invalid"
STALE_FACTS = "stale_facts"


@dataclass(frozen=True, slots=True)
class ExitPricing:
    """What the exit decided about money, and what it decided it from."""

    status: str
    #: Modules whose register covered the identity, and the rows that did
    #: (pass or agreement id; never the identity text -- this travels in an
    #: event detail, and plate text is kept out of those on purpose).
    covered_by: tuple[str, ...] = ()
    matched: tuple[dict, ...] = ()
    fee_minor: int | None = None
    currency: str | None = None
    plan_version: str | None = None
    breakdown: list = field(default_factory=list)
    entry_at: str | None = None
    exit_at: str | None = None
    session_id: str | None = None
    space_class: str | None = None
    #: The engine's own words on a refusal, verbatim.
    refusal: dict | None = None
    #: What the answer was made from: when the facts were last refreshed, the
    #: stays cursor, the day the registers were read against and whose clock.
    computed_from: dict = field(default_factory=dict)
    #: More than one open stay under this identity: the newest entry was
    #: priced and the count is said.
    ambiguous_stays: int = 0

    def to_detail(self) -> dict:
        detail = {
            "status": self.status,
            "covered_by": list(self.covered_by),
            "matched": list(self.matched),
            "computed_from": self.computed_from,
        }
        if self.status in (PRICED, ENGINE_REFUSED, ENGINE_INVALID):
            detail.update(
                entry_at=self.entry_at,
                exit_at=self.exit_at,
                session_id=self.session_id,
                space_class=self.space_class,
            )
        if self.status == PRICED:
            detail.update(
                fee_minor=self.fee_minor,
                currency=self.currency,
                plan_version=self.plan_version,
                breakdown=self.breakdown,
            )
        if self.refusal is not None:
            detail["refusal"] = self.refusal
        if self.ambiguous_stays:
            detail["ambiguous_stays"] = self.ambiguous_stays
        return detail


def local_day(now: float, timezone: str | None) -> tuple[date, str]:
    """The garage's local day at `now`, and whose clock it was. A zone the
    payload did not name, or one this box does not carry, falls to UTC and
    SAYS so -- a day judged on the wrong clock is a wrong day for six hours
    of every twenty-four, and the record must be able to show which."""
    if timezone:
        try:
            return datetime.fromtimestamp(now, ZoneInfo(timezone)).date(), timezone
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return datetime.fromtimestamp(now, UTC).date(), "UTC (the garage's zone was not usable)"


def _day(text: str | None) -> date | None:
    return date.fromisoformat(text) if text else None


def row_covers(module: str, row: dict, head: dict, day: date) -> bool:
    """Whether one register row, on the pass or agreement it names, covers
    the identity on `day` -- by the modules' own published semantics, read
    with the lane's day as both verbs ask.

    garage-pass (G27): the registration's `effective_day` has come and its
    `end_day` has not; the pass is stored `active`, readable, and `day` is
    inside `valid_from`..`valid_to` (both optional, both inclusive).
    monthly-billing (G48): the agreement's latest version is `active`, or
    `cancelled` with a cancellation day still ahead -- the paid period runs
    to its end. A row whose head the register did not list (an id the store
    holds no version of, `agreements_not_found`) covers nothing.
    """
    if not head:
        return False
    if module == "garage_pass":
        if head.get("state") != "active" or head.get("unreadable") is not None:
            return False
        effective, end = _day(row.get("effective_day")), _day(row.get("end_day"))
        if effective is None or day < effective or (end is not None and day >= end):
            return False
        valid_from, valid_to = _day(head.get("valid_from")), _day(head.get("valid_to"))
        if valid_from is not None and day < valid_from:
            return False
        if valid_to is not None and day > valid_to:
            return False
        return True
    if module == "monthly_billing":
        status = head.get("status")
        if status == "active":
            return True
        if status == "cancelled":
            until = _day(head.get("cancelled_effective_day"))
            return until is not None and day < until
        return False
    return False


def price_exit(identity_text: str, cache: DecisionCache, *, now: float) -> ExitPricing:
    """The exit's answer for one identity, from the cache alone.

    `identity_text` is the plate the camera read or the ticket the lane holds;
    `now` is the lane's clock. No argument is a network, and none of the
    cache's methods this calls open one.
    """
    day, clock = local_day(now, cache.timezone)
    exit_at = datetime.fromtimestamp(now, UTC).isoformat()
    computed_from = {
        "rules_refreshed_at": cache._refreshed_at,
        "stays_refreshed_at": cache.stays_refreshed_at,
        "stays_cursor": cache.stays_cursor,
        "day": day.isoformat(),
        "clock": clock,
    }
    if cache.is_stale(now=now):
        return ExitPricing(status=STALE_FACTS, computed_from=computed_from)

    covered_by: list[str] = []
    matched: list[dict] = []
    for module, row, head in cache.register_rows_for(identity_text):
        if row_covers(module, row, head, day):
            if module not in covered_by:
                covered_by.append(module)
            matched.append(
                {"module": module, "pass": row.get("pass"), "agreement": row.get("agreement")}
            )
    if covered_by:
        return ExitPricing(
            status=COVERED, covered_by=tuple(covered_by), matched=tuple(matched),
            computed_from=computed_from,
        )

    stays = cache.open_stays_for(identity_text)
    if not stays:
        return ExitPricing(status=NO_CACHED_ENTRY, computed_from=computed_from)
    # The newest entry, when the cache holds more than one under this
    # identity -- which the platform's own index forbids for one vehicle and
    # a ticket cannot share -- and the count is said rather than hidden.
    stay = max(stays, key=lambda s: s.get("entry_at") or "")
    request = {
        "plans": cache.plans,
        "currency": cache.currency,
        "space_class": cache.space_class,
        "entry_at": stay.get("entry_at"),
        "exit_at": exit_at,
    }
    status, body = run_quote(request)
    common = dict(
        entry_at=stay.get("entry_at"), exit_at=exit_at, session_id=stay.get("session_id"),
        space_class=cache.space_class, computed_from=computed_from,
        ambiguous_stays=len(stays) if len(stays) > 1 else 0,
    )
    if status == 200:
        return ExitPricing(
            status=PRICED, fee_minor=body["fee_minor"], currency=body["currency"],
            plan_version=body["plan_version"], breakdown=list(body["breakdown"]), **common,
        )
    return ExitPricing(
        status=ENGINE_REFUSED if status == 422 else ENGINE_INVALID, refusal=body, **common,
    )
