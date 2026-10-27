"""The car at an exit this lane could not let out on its own copy: a case on
the platform, and the barrier opens only when that case is settled.

THE GATE OPENS ONLY WHEN SETTLED (the owner's rule for the exit, 2026-10-27):
a fee of zero, covered by a pass or monthly agreement, paid, or decided by a
person. The lane settles the first two itself, from its copy
(`exit_pricing`), in about a second and with nothing on the network. Every
other car at an exit is a CASE (platform 0037):

  priced          the lane priced the stay at a fee above zero. The case names
                  the stay (`session_id`, method `lane`) and the fee is paid at
                  the lane's reader (platform 0038): the platform hands its
                  PaymentIntent to the reader bound to this lane, and the case
                  is settled when Stripe says it succeeded at that amount.
  not found here  no stay in the lane's copy (`no_cached_entry`), a copy too
                  old to price from, an engine that refused, or a car whose
                  plate the lane could not stand behind. The platform finds
                  the stay by the car's ID -- its positioned plate characters,
                  colour code and size -- or asks the picker, and when none is
                  found the driver's word: the exit display asks for the entry
                  date and time, and the platform prices it.

THERE IS NO "OPEN AND COLLECT NOTHING". The exit that once let a car out
whose entry the lane did not hold, and collected nothing, is gone: that car
is a case like any other. A platform that cannot be reached leaves the gate
down and the car to a person (`fallback_needs_human`), who can let it out at
the lane (`POST /v1/lane/vend`) or on the platform (the case's let-out).

ONE CAR AT A TIME. A case is advanced on the lane's idle turns while the car
is still on the arming loop, the platform asked at most every
`EXIT_POLL_SECONDS` (the platform reads Stripe at most that often). A car
that leaves the loop leaves its case to a person; nothing opens for it.

WHAT TRAVELS. The case carries the car's ID fields (`id_fields`) and nothing
of the plate text. A low-confidence plate gives no characters at all: an ID
the lane cannot stand behind is not sent as one. The driver's entry time and
receipt phone go in one request each, and the phone is kept nowhere here.

AN EMPTY ID. The identity service does not fill the positioned characters,
colour code or size yet (its contract for them has not landed), so a plate
the lane could not stand behind opens a case with no ID fields at all. That
excludes no stay: every open stay of the garage is a candidate, and the case
goes to the picker (10 or fewer) or to the driver's word.

THE READER IS THE PLATFORM'S ONCE THE FEE IS ON IT. The platform hands the
case's PaymentIntent to the reader bound to this lane; from then until the
car's close the lane's own reader screen is not driven for it, since a cart
or a clear it put up would take the intent off the reader. A case the lane
lets go of without opening for it -- the car left, a new car arrived, a
person opened at the lane -- has its payment taken off the reader through
the platform (`cancel_on_reader`), so the next car is never shown this
car's fee.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from .exit_pricing import PRICED, ExitPricing
from .interfaces import ExitDisplay, VehicleIdentity
from .platform_client import PlatformRejected, PlatformUnreachable

log = logging.getLogger(__name__)

#: How often a case is asked about while it waits. The platform reads Stripe
#: at most once every 2 seconds per payment; asking faster learns nothing.
EXIT_POLL_SECONDS = 2.0

#: How long a fee handed to the reader is left before it is handed over again
#: with nothing new from the platform. The answer carries no reader state and
#: a second decline with the same code looks like the first, so a fee waiting
#: for a card is offered again on this interval; a reader still showing it
#: answers `reader_busy`, which is not an attempt.
EXIT_RESEND_SECONDS = 6.0

#: How many times the fee may reach the reader before a person is told. The
#: lane goes on offering it after that; the person decides.
EXIT_PAY_ATTEMPTS = 3

#: How long the arming loop must read clear before the waiting car is taken
#: to have left. One clear sample is a loop flicker as often as a car gone,
#: and a paid car whose case is dropped needs a person to get out.
EXIT_LEFT_SECONDS = 2.0

#: The platform's refusals of a `/pay` that are not refusals of the case: the
#: reader still has the intent (`reader_busy`), or it was paid since the case
#: was last asked about (`exit_settled`).
_PAY_NOT_REFUSED = frozenset({"reader_busy", "exit_settled"})

#: The positioned-characters rule a whole plate read at the lane's confidence
#: is sent under, folded as the platform folds it (platform `exitMatch.js`).
PLATE_RULE = "opvid-plate-chars/1"
_FOLD = str.maketrans({"O": "0", "I": "1", "B": "8", "S": "5"})
_PATTERN_MAX = 16

#: What a case can answer it needs (platform `exits.settledOf`).
NEEDS_PAYMENT = "payment"
NEEDS_DRIVER_WORD = "driver_word"
NEEDS_PERSON = "person"

#: What a person is told when the fee reached the reader `EXIT_PAY_ATTEMPTS`
#: times and the case is still not paid.
NEEDS_PAY_ATTEMPTS = "payment_attempts"

#: What `advance` answers.
SETTLED = "settled"
WAITING = "waiting"
UNANSWERED = "unanswered"

#: The refusals the lane makes itself.
OTHER_CASE = "another_exit_case"
ENTRY_WITHOUT_OFFSET = "entry_time_without_offset"


def fold(plate: str) -> str:
    """A plate as the platform compares it: upper case, letters and digits
    only, O/I/B/S folded to 0/1/8/5."""
    return "".join(c for c in plate.upper() if c.isascii() and c.isalnum()).translate(_FOLD)


def id_fields(identity: VehicleIdentity, *, plate_confident: bool) -> dict:
    """The car's ID as the platform's exit matching takes it.

    The plate's positioned characters as the identity service stated them;
    failing that, a whole plate the lane read at its confidence, every
    position sure; failing that, nothing about the plate -- which excludes no
    stay, where `unreadable` would exclude every stay that was read. The
    colour code and size only when measured.
    """
    fields: dict = {}
    characters = identity.plate_characters
    if characters is not None:
        fields["plate_characters"] = {
            "state": characters.state,
            "rule": characters.rule,
            "pattern": characters.pattern,
        }
    elif identity.plate and plate_confident:
        pattern = fold(identity.plate)
        if 0 < len(pattern) <= _PATTERN_MAX:
            fields["plate_characters"] = {"state": "read", "rule": PLATE_RULE, "pattern": pattern}
    if identity.color_code:
        fields["color_code"] = identity.color_code
    if identity.size:
        fields["size"] = identity.size
    return fields


def named_stay(pricing: ExitPricing | None) -> str | None:
    """The stay the lane priced, named on the case (method `lane`), or None.
    Only a priced stay: a stay the engine refused is the platform's to find
    and price again."""
    if pricing is not None and pricing.status == PRICED and pricing.session_id:
        return pricing.session_id
    return None


@dataclass
class ExitCase:
    """One car at this exit, from the moment the lane could not settle it
    until the barrier opens or the car leaves the loop."""

    event_id: str
    exit_at: str
    identity: VehicleIdentity
    pricing: ExitPricing | None
    fields: dict
    session_id: str | None = None
    exit_id: str | None = None
    #: The case as the platform last answered it.
    answer: dict | None = None
    asked_at: float | None = None
    #: When the fee was last handed to the reader, or None: never.
    sent_at: float | None = None
    #: The decline the payment was last sent after (None: sent before any).
    paid_after: str | None = None
    #: How many times the reader took the fee (`sent` true or unknown).
    attempts: int = 0
    #: When the arming loop first read clear under the waiting car, or None.
    clear_since: float | None = None
    word_given: bool = False
    receipt_sent: bool = False
    #: Whether the last ask went unanswered, so it is said once per outage.
    unanswered: bool = False
    #: The last refusal the driver should be told about, or None.
    refused: str | None = None
    #: What a person was last told this case needs, so it is said once.
    told: str | None = None
    #: What the last step answered (`SETTLED`, `WAITING`, `UNANSWERED`).
    outcome: str | None = None

    @property
    def needs(self) -> str | None:
        return None if self.answer is None else self.answer.get("needs")

    @property
    def settled(self) -> bool:
        return bool(self.answer and self.answer.get("settled") is True)

    @property
    def why(self) -> str | None:
        return None if self.answer is None else self.answer.get("why")

    def view(self) -> dict:
        """What the exit display shows while the case waits."""
        fee = (self.answer or {}).get("fee")
        view = {"needs": self.needs, "fee": fee}
        if self.refused:
            view["refused"] = self.refused
        return view


def _own(case: ExitCase, answer: dict) -> dict:
    """The answer, when it is about this case. An answer naming another case
    -- settled or not -- says nothing about this car, and opens nothing."""
    if answer.get("id") != case.exit_id:
        raise PlatformRejected(None, "the platform answered another exit case", OTHER_CASE)
    return answer


def has_offset(entry_at: str) -> bool:
    """Whether the driver's entry time is ISO 8601 with a timezone offset. The
    platform reads a time without one on its own clock, which is not the
    garage's, and would charge for hours the car was not there."""
    try:
        parsed = datetime.fromisoformat(entry_at)
    except (TypeError, ValueError):
        return False
    return parsed.utcoffset() is not None


def advance(case: ExitCase, platform, display: ExitDisplay | None, *, now: float) -> str:
    """One step of the case: open it, or ask how it stands; then do what it
    needs that the lane can do. `SETTLED` when the barrier may open,
    `UNANSWERED` when the platform could not be reached, otherwise `WAITING`.

    Every request here goes to this lane's own platform under its device
    token, and nothing in a request or a log line names the plate.
    """
    try:
        if case.exit_id is None:
            case.answer = platform.open_exit(
                event_id=case.event_id,
                exit_at=case.exit_at,
                session_id=case.session_id,
                id_fields=case.fields,
            )
            case.exit_id = case.answer["id"]
        elif case.asked_at is not None and now - case.asked_at < EXIT_POLL_SECONDS:
            return SETTLED if case.settled else WAITING
        else:
            case.answer = _own(case, platform.get_exit(case.exit_id))
        case.asked_at = now
        case.unanswered = False
        if case.settled:
            return SETTLED
        _act(case, platform, display, now=now)
    except PlatformUnreachable as err:
        if not case.unanswered:
            log.warning("exit case unanswered; the gate stays down: %s", err)
        case.unanswered = True
        case.asked_at = now
        return UNANSWERED
    except PlatformRejected as err:
        # A case the platform will not open or act on: refused by name, kept
        # for a person. The gate stays down.
        log.warning("exit case refused; the gate stays down: %s", err.code or err.status)
        case.refused = err.code or "refused"
        case.asked_at = now
    if display is not None:
        display.show_exit(case.view())
    return SETTLED if case.settled else WAITING


def _act(case: ExitCase, platform, display: ExitDisplay | None, *, now: float) -> None:
    """What the lane does for a case that is not settled: hand the fee to the
    reader (at once after a new decline, otherwise again every
    `EXIT_RESEND_SECONDS` while it waits for a card), or pass on the driver's
    word."""
    needs = case.needs
    if needs == NEEDS_PAYMENT:
        payment = case.answer.get("payment") or {}
        if payment.get("status") not in (None, "open"):
            return
        failure = payment.get("last_failure")
        due = (
            case.sent_at is None
            or (failure is not None and failure != case.paid_after)
            or now - case.sent_at >= EXIT_RESEND_SECONDS
        )
        if not due:
            return
        try:
            answer, sent = platform.exit_pay(case.exit_id)
        except PlatformRejected as err:
            if err.code not in _PAY_NOT_REFUSED:
                raise
            case.sent_at = now
            return
        case.answer = _own(case, answer)
        case.sent_at = now
        case.paid_after = failure
        if sent is True or sent == "unknown":
            case.attempts += 1
        log.info("exit case: the fee was handed to the lane's reader (sent=%s, attempt %d)",
                 sent, case.attempts)
        return
    if needs == NEEDS_DRIVER_WORD and display is not None and not case.word_given:
        entry_at = display.entry_time()
        if entry_at is None:
            return
        if not has_offset(entry_at):
            # Refused here and never sent: the display asks again.
            case.refused = ENTRY_WITHOUT_OFFSET
            return
        try:
            answer = platform.exit_word(case.exit_id, entry_at)
        except PlatformRejected as err:
            # Not a time the platform takes (in the future, before the 90
            # days, not a time at all): the display asks again.
            case.refused = err.code or "entry_time_refused"
            return
        case.answer = _own(case, answer)
        case.word_given = True
        case.refused = None


def send_receipt(case: ExitCase, platform, display: ExitDisplay | None) -> bool:
    """A text receipt for a paid case, when the driver gave a phone: once,
    best effort, the number passed on and kept nowhere. True when sent."""
    if display is None or case.receipt_sent or case.why != "paid" or case.exit_id is None:
        return False
    phone = display.receipt_phone()
    if phone is None:
        return False
    case.receipt_sent = True
    try:
        platform.exit_receipt(case.exit_id, phone)
    except (PlatformUnreachable, PlatformRejected) as err:
        log.info("exit case: the text receipt was not sent: %s", getattr(err, "code", None))
        return False
    return True


def cancel_on_reader(case: ExitCase, platform) -> bool:
    """Take the case's payment off the lane's reader, through the platform
    (`/exits/:id/pay/cancel`), when the lane lets the case go without opening
    for it. Only a case whose fee was handed to the reader. Best effort: a
    card already being authorised cannot be stopped (`reader_busy`), and the
    platform records it paid if Stripe took it. True when cleared."""
    if platform is None or case.exit_id is None or case.sent_at is None:
        return False
    try:
        platform.exit_pay_cancel(case.exit_id)
    except (PlatformUnreachable, PlatformRejected) as err:
        log.warning("exit case: the payment could not be taken off the reader: %s",
                    getattr(err, "code", None) or err.__class__.__name__)
        return False
    return True
