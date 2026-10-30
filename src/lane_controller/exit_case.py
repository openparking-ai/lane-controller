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
that leaves the loop leaves its case to a person; nothing opens for it. Nor
for a car that rolled on after it: a loop that read clear for longer than a
flicker and is occupied again drops the case, since the car on it was not
read. The loop is watched while the platform is asked too (`LoopWatch`), and
a settled answer opens only for a car that is still there when it comes back.

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
car's fee: after the next car's frames are taken, before its fee goes on.
"""

from __future__ import annotations

import logging
import threading
import time
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

#: The longest clear a loop flicker is taken to be. A clear at least this
#: long that ends with the loop occupied again may be another car rolled on
#: unarmed, so the case is dropped rather than opened for whoever is there.
EXIT_FLICKER_SECONDS = 0.5

#: How often the arming loop is sampled while a car waits on its case: short
#: enough that a flicker reads as one clear sample, under `EXIT_FLICKER_SECONDS`.
EXIT_SAMPLE_SECONDS = 0.25

#: How many clear samples in a row are a car gone rather than a flicker.
EXIT_CLEAR_SAMPLES = 2

#: How many `reader_busy` answers in a row before a person is told the reader
#: is stuck: at `EXIT_RESEND_SECONDS` apart, half a minute of a reader that
#: never frees up.
EXIT_READER_BUSY = 5

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

#: What a person is told when the reader answered `reader_busy`
#: `EXIT_READER_BUSY` times in a row.
NEEDS_READER_BUSY = "reader_busy"

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
    #: How many `reader_busy` answers in a row the last sends got.
    busy: int = 0
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


class LoopWatch:
    """The arming loop, sampled every `EXIT_SAMPLE_SECONDS` on its own thread
    while the lane's thread waits on the platform for a case: a call can take
    up to the client's timeout, and a car can back off in that time.

    `stayed()` after `stop()`: the loop was occupied at the start and at the
    end, and no clear in between was longer than a flicker --
    `EXIT_CLEAR_SAMPLES` clear samples in a row, or `EXIT_FLICKER_SECONDS` on
    the lane's clock. A loop read that raises is taken as the car gone: a
    watch that cannot see the loop vouches for nothing."""

    def __init__(self, loop, clock) -> None:
        self._loop = loop
        self._clock = clock
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._clear_since: float | None = None
        self._clear_run = 0
        self._left = False
        self._sample()
        self._thread = threading.Thread(target=self._run, name="exit-loop-watch", daemon=True)

    def start(self) -> LoopWatch:
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop sampling, and read the loop as the call returns. A clear
        reading then is read once more after `EXIT_SAMPLE_SECONDS`, so a
        flicker at that moment is not taken for the car gone."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()
        self._sample()
        if self._clear_since is not None and not self._left:
            time.sleep(EXIT_SAMPLE_SECONDS)
            self._sample()

    def stayed(self) -> bool:
        with self._lock:
            return not self._left and self._clear_since is None

    def _run(self) -> None:
        while not self._stop.wait(EXIT_SAMPLE_SECONDS):
            self._sample()

    def _fail(self, err: Exception) -> None:
        log.warning("exit case: the arming loop could not be read while the platform "
                    "was asked; the car is taken as gone: %s", err.__class__.__name__)
        with self._lock:
            self._left = True

    def _sample(self) -> None:
        try:
            occupied = self._loop.is_occupied()
        except Exception as err:  # noqa: BLE001 -- an unread loop fails shut
            self._fail(err)
            return
        now = self._clock()
        with self._lock:
            if occupied:
                since = self._clear_since
                if since is not None and now - since >= EXIT_FLICKER_SECONDS:
                    self._left = True
                self._clear_since, self._clear_run = None, 0
                return
            if self._clear_since is None:
                self._clear_since = now
            self._clear_run += 1
            if self._clear_run >= EXIT_CLEAR_SAMPLES:
                self._left = True


def _own(case: ExitCase, answer: dict) -> dict:
    """The answer, when it is about this case: its id, and the car's own event
    the case was opened under. An answer naming another case or another event
    -- settled or not -- says nothing about this car, and opens nothing."""
    if answer.get("event_id") != case.event_id or (
        case.exit_id is not None and answer.get("id") != case.exit_id
    ):
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
            case.answer = _own(case, platform.open_exit(
                event_id=case.event_id,
                exit_at=case.exit_at,
                session_id=case.session_id,
                id_fields=case.fields,
            ))
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
            if err.code == "reader_busy":
                case.busy += 1
            case.sent_at = now
            return
        case.answer = _own(case, answer)
        case.sent_at = now
        case.busy = 0
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


#: What `cancel_on_reader` found: the payment canceled at Stripe; the card had
#: already paid (409 `exit_paid`); the case had no payment to cancel (409
#: `no_payment`); a card still being authorised (409 `payment_in_progress`);
#: the platform not reached, a 5xx or a 429 (`failed`); or a refusal no retry
#: can change (`refused`: the case is not this lane's or not there, or Stripe
#: took another amount -- a person decides).
CANCELED = "canceled"
CANCEL_FOUND_PAID = "paid"
CANCEL_NOTHING = "nothing"
CANCEL_IN_PROGRESS = "in_progress"
CANCEL_FAILED = "failed"
CANCEL_REFUSED = "refused"

#: The platform has the payment off the reader, or never had one on it: the
#: cancel is done and is not asked again.
CANCEL_CONFIRMED = frozenset({CANCELED, CANCEL_FOUND_PAID, CANCEL_NOTHING})
#: Asked again, with a bounded backoff, until the platform confirms
#: (`PendingCancels`): never dropped, and never on the lane's thread.
CANCEL_RETRY = frozenset({CANCEL_IN_PROGRESS, CANCEL_FAILED})

#: The first wait before a cancel is asked again, doubled on each failure up
#: to `EXIT_CANCEL_RETRY_MAX_SECONDS` -- asked again for as long as it takes.
EXIT_CANCEL_RETRY_FIRST_SECONDS = 1.0
EXIT_CANCEL_RETRY_MAX_SECONDS = 30.0
#: After this many failed asks a person is told, once: another car's fee may
#: still be on the reader. The lane goes on asking.
EXIT_CANCEL_TELL_AFTER = 5
#: What a person is told then, and when the platform refuses a cancel outright.
NEEDS_CANCEL_PENDING = "cancel_pending"
NEEDS_CANCEL_REFUSED = "cancel_refused"

#: The vend authorities the platform's close takes as `let_out_by` (platform
#: 0042; `human_open_and_flag` is marked flagged on its change-log line). It
#: refuses any other with a 400, and a refused close is dropped from the
#: outbox, not sent again: a close for another authority goes without it.
LET_OUT_AUTHORITIES = frozenset({"display_code_confirmed", "human_open_now",
                                 "human_open_and_flag"})


def cancel_on_reader(case: ExitCase, platform) -> str | None:
    """Cancel the case's payment at Stripe and take it off the lane's reader,
    through the platform (`/exits/:id/pay/cancel`): the lane let the case go,
    or a person opened for it. Only a case whose fee was handed to the reader;
    None for any other. Asked once here; an answer in `CANCEL_RETRY` is asked
    again by `PendingCancels`, off the lane's thread, until the platform
    confirms -- a failed cancel is never dropped (it once left another car's
    fee on the reader).

    A card that had already paid (`exit_paid`) is not a failure: the platform
    has the case settled as paid, and nothing was canceled. It opens nothing
    by itself -- a case is let go only once its car has left the loop or a
    person has opened, and the barrier opens for a paid case only through
    `_let_out`, for the car still on the loop. A card still being authorised
    (`payment_in_progress`) opens nothing either."""
    if platform is None or case.exit_id is None or case.sent_at is None:
        return None
    return cancel_payment(case.exit_id, platform)


def cancel_payment(exit_id: str, platform) -> str:
    """One ask of `/exits/:id/pay/cancel`, read into one of the outcomes above.
    The case's id is the only thing sent; nothing here names a plate."""
    try:
        platform.exit_pay_cancel(exit_id)
    except PlatformRejected as err:
        if err.code == "exit_paid":
            log.info("exit case: the card had already paid; the platform settled the case "
                     "as paid, and nothing was canceled")
            return CANCEL_FOUND_PAID
        if err.code == "no_payment":
            log.info("exit case: the case has no payment to cancel")
            return CANCEL_NOTHING
        if err.code == "payment_in_progress":
            log.info("exit case: a card is being authorised; the payment was not canceled yet")
            return CANCEL_IN_PROGRESS
        if err.status == 429:
            log.warning("exit case: the payment cancel was rate limited; it is asked again")
            return CANCEL_FAILED
        log.warning("exit case: the platform refused the payment cancel: %s",
                    err.code or err.status)
        return CANCEL_REFUSED
    except PlatformUnreachable as err:
        log.warning("exit case: the payment could not be canceled yet: %s",
                    err.__class__.__name__)
        return CANCEL_FAILED
    return CANCELED


@dataclass
class _Pending:
    attempts: int
    due: float
    told: bool = False


def cancel_backoff(attempts: int) -> float:
    """The wait after `attempts` failed asks: 1, 2, 4, 8, 16, then 30 seconds
    for as long as it takes."""
    return min(EXIT_CANCEL_RETRY_MAX_SECONDS,
               EXIT_CANCEL_RETRY_FIRST_SECONDS * 2 ** max(0, attempts - 1))


class PendingCancels:
    """The payment cancels the platform has not confirmed, by exit case id,
    each with the next time it is due. Kept on disk through the event queue
    (`EventQueue.keep_cancel`) when it has a store, so a restart asks again
    for every one -- due at once.

    Two threads meet here: the lane's adds a cancel that failed, the drain's
    asks again (`LaneController.retry_cancels`). One lock, never held across
    a network call."""

    def __init__(self, clock, events=None) -> None:
        self._clock = clock
        self._events = events
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        held = events.held_cancels() if events is not None else []
        now = clock()
        for exit_id in held:
            self._pending[exit_id] = _Pending(attempts=0, due=now)

    def add(self, exit_id: str) -> None:
        """A cancel asked once and not confirmed: asked again after the first wait."""
        with self._lock:
            if exit_id in self._pending:
                return
            self._pending[exit_id] = _Pending(attempts=1, due=self._clock() + cancel_backoff(1))
        if self._events is not None:
            self._events.keep_cancel(exit_id, self._clock())

    def due(self) -> list[str]:
        now = self._clock()
        with self._lock:
            return [exit_id for exit_id, p in self._pending.items() if p.due <= now]

    def failed(self, exit_id: str) -> tuple[int, bool]:
        """One more failed ask: the attempts so far, and whether a person is
        to be told now (once, at `EXIT_CANCEL_TELL_AFTER`)."""
        with self._lock:
            pending = self._pending.get(exit_id)
            if pending is None:
                return 0, False
            pending.attempts += 1
            pending.due = self._clock() + cancel_backoff(pending.attempts)
            tell = pending.attempts >= EXIT_CANCEL_TELL_AFTER and not pending.told
            pending.told = pending.told or tell
            return pending.attempts, tell

    def settle(self, exit_id: str) -> None:
        """Confirmed, or refused for good: never asked again."""
        with self._lock:
            known = self._pending.pop(exit_id, None) is not None
        if known and self._events is not None:
            self._events.forget_cancel(exit_id)

    def __contains__(self, exit_id: object) -> bool:
        with self._lock:
            return exit_id in self._pending

    def __len__(self) -> int:
        with self._lock:
            return len(self._pending)
