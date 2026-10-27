"""Keeping the local decision cache fresh, and the outbox draining.

Both are best effort by design. The lane decides from what it already has; this
module only changes what it has, and never blocks a barrier.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from .decision import DecisionCache
from .events import EventTransport, LaneEvent
from .platform_client import (
    CLOCK_SKEW_CODE,
    PlatformClient,
    PlatformRejected,
    PlatformUnreachable,
)

log = logging.getLogger(__name__)

SESSION_OPEN = "session_open"
SESSION_CLOSE = "session_close"

#: The largest log post this lane makes, in bytes of its body. The platform
#: refuses a body over 1 MB with a 413; a quarter of that leaves room for
#: what one event's detail can grow to. A chunk of `events.LOG_CHUNK` events
#: larger than this goes in halves, and so does one the platform answers 413.
LOG_CHUNK_BYTES = 256 * 1024

# ---------------------------------------------------------------------------
# The transit events: a vend creates a PENDING entry, and the loops after the
# gate decide what becomes of it. The ticket is not the entry -- a driver can
# pull up, take one and drive away, and until this existed that abandoned
# approach became a phantom occupant, counted as inside and never seen again.
#
# EVERY OUTCOME HAS ITS OWN NAME AND NONE IS FOLDED INTO ANOTHER. A silent void
# would re-create the abandoned-ticket fraud; a silent promotion to a session
# is the phantom occupant. Both are named, both are recorded, and neither is
# the other.
#
# They ride the SAME queue as everything else, so a lane that was offline
# replays what happened in the order it happened.
# ---------------------------------------------------------------------------
ARMED = "armed"
ARMING_INCOMPLETE = "arming_incomplete"
#: THE DEACTIVATE LOOP HELD THE ARMING CYCLE. A vehicle is on the arming loop
#: and a second one is on the deactivate loop behind it -- too close to be let
#: through separately -- so the lane did not arm, did not photograph, did not
#: vend. Recorded at the START of the held interval, once, with its reason;
#: the interval is re-evaluated on every turn and ends in one of two ways,
#: each recorded as ARMING_SUPPRESSION_ENDED. Never silent: a car that was
#: held and then followed the one ahead anyway left neither picture nor event
#: of its own, and this pair is the only record that anything was there.
#:
#: THE SAME LITERALS as `platform/src/app.js` `LANE_EVENT_KINDS`, which
#: accepted both before this lane emitted either (platform PR #15).
ARMING_SUPPRESSED = "arming_suppressed"
ARMING_SUPPRESSION_ENDED = "arming_suppression_ended"

ENTRY_PENDING = "entry_pending"
ENTRY_CONFIRMED = "entry_confirmed"
ENTRY_BACKED_OUT = "entry_backed_out"
ENTRY_HELD = "entry_held"
ENTRY_UNCONFIRMABLE = "entry_unconfirmable"
#: A crossing with NO pending entry behind it: the loops after the gate saw a
#: vehicle go through and no vend preceded it. A car the lane refused that
#: drove in anyway, a car following the one admitted, a car going through a
#: barrier that is simply up -- the lane cannot tell which, and says so in the
#: detail rather than guessing. It is an ENTRY because a vehicle is inside; it
#: is a DIFFERENT entry because nothing admitted it, and it is never folded
#: into `entry_confirmed`, which had a vend behind it. It moves no
#: `TransitState`: that enum says what became of the last VEND, and this had
#: none. The car is a STAY all the same (the owner's rule: the loops and the
#: pictures make the entry): a `session_open` follows it, `confirmed` and
#: `tailgater: true`, with a plate or `plate_unreadable` -- unless the camera
#: says no car was there, which this event's `stay_opened: false` records.
#:
#: THE SAME LITERAL as `platform/src/app.js` `LANE_EVENT_KINDS`, which must
#: accept a kind before a lane may emit it -- a lane ahead of its platform is
#: refused 400. Nothing in either CI compares the two lists.
ENTRY_UNADMITTED = "entry_unadmitted"

EXIT_PENDING = "exit_pending"
EXIT_CONFIRMED = "exit_confirmed"
EXIT_BACKED_IN = "exit_backed_in"
EXIT_HELD = "exit_held"
EXIT_UNCONFIRMABLE = "exit_unconfirmable"

#: Why an entry or an exit ended the way it did. One reason per outcome, and
#: the reason travels to the platform with the session so the money record can
#: say what confirmed it.
REASON_FORWARD = "closing_sequence_forward"
#: The barrier's own closing loop, read through its detector's second output,
#: read a vehicle inside the window. Its own name, never `closing_sequence_forward`:
#: one loop saw a vehicle reach it past the gate, and did not see which way it went.
REASON_CLOSING_LOOP_OCCUPIED = "closing_loop_occupied"
REASON_REVERSE = "closing_sequence_reverse"
#: An arriving car whose descriptor matched the garage's banned list (U7f):
#: refused at the entry, the gate shut. The `decision` event's reason.
REASON_BANNED = "banned"
REASON_WINDOW_ELAPSED = "confirmation_window_elapsed"
REASON_NO_CLOSING_LOOPS = "no_closing_loops_configured"
REASON_ARMING_INCOMPLETE = "only_one_arming_loop_occupied"
#: Why an arming cycle was held: the deactivate loop read occupied.
REASON_VEHICLE_TOO_CLOSE = "vehicle_too_close"
#: How a held interval ended. `armed`: the deactivate loop cleared while the
#: vehicle was still on the arming loop, and the lane armed for it.
#: `arming_loop_cleared`: the vehicle left the arming loop without ever being
#: armed for -- it backed out, or it followed the one ahead.
SUPPRESSION_ENDED_ARMED = "armed"
SUPPRESSION_ENDED_ARMING_LOOP_CLEARED = "arming_loop_cleared"
#: The closing-loop driver did not return inside the lane's own settle deadline
#: -- the confirmation window plus `[lane] settle_grace_s`. NOT the same fact as
#: `confirmation_window_elapsed`, which is the loops answering that nothing
#: crossed: this is the loops not answering at all, and the two want different
#: repairs.
REASON_LOOP_DRIVER_TIMEOUT = "loop_driver_timeout"
#: The loops after the gate completed a crossing and nothing was pending. Its
#: own reason, never `closing_sequence_forward`: that one says a vend's car
#: went through, and this says a car went through that no vend let in.
REASON_NO_PENDING_ENTRY = "crossing_with_no_pending_entry"

#: What the platform is told confirmed a session. `confirmed` means the
#: barrier's closing loop read a vehicle after the vend, or two loops after the
#: gate saw one cross them forward; the event's reason says which. `unconfirmable` means this
#: lane has no closing loops and nothing could have confirmed or refuted it --
#: which is the honest name for it, and is not the same word as `confirmed`.
CONFIRMED = "confirmed"
UNCONFIRMABLE = "unconfirmable"

#: EXITS ONLY, and it is the one place a lane reports a session on something the
#: loops did not confirm. At an exit the vend IS the payment moment and the
#: barrier opened: the car is gone whatever the loops saw, so the session closes
#: and the stay is billed, marked `held` and flagged with an `exit_held` event
#: for a human. An entry is the opposite -- nothing confirmed it, so there is no
#: session at all -- and this value is never sent on an open.
HELD = "held"


class _TooLarge(Exception):
    """A log post the platform answered 413: posted again in halves, not dropped."""


def _body_bytes(plain: list[dict]) -> int:
    """The size of the body `PlatformClient.post_events` sends for `plain`."""
    return len(json.dumps({"events": plain}).encode("utf-8"))


def to_iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).isoformat()


def sync_rules(client: PlatformClient, cache: DecisionCache) -> dict | None:
    """Refresh the cache from the platform. Returns the payload, or None if offline.

    On failure the existing cache is left exactly as it was. Stale rules are
    still rules, and `DecisionCache.is_stale` is what eventually stops the lane
    trusting them -- not a failed request.
    """
    try:
        payload = client.get_rules()
    except PlatformUnreachable as err:
        log.warning("rule sync failed, keeping the cache we have: %s", err)
        return None

    cache.load_payload(payload)
    log.info(
        "rules synced: default=%s, %d plan(s), %d module register(s), %d open stay(s), cursor=%s",
        cache.default_action, len(cache.plans), len(cache.entitlements), len(cache.stays),
        cache.stays_cursor,
    )
    return payload


def sync_stays(client: PlatformClient, cache: DecisionCache) -> dict | None:
    """The fast cadence: every stay changed since the cache's cursor, applied.

    Returns the last answer, or None if offline -- and, like `sync_rules`, an
    offline read changes nothing. A cache with no cursor yet takes the full
    set. A page that says `more` is followed at once, every page landing
    before the next is asked for. A REFUSAL -- a cursor the platform will not
    take, a garage it no longer knows -- drops the cursor so the next read
    takes the full set again, and is logged as what it is rather than being
    retried under the same cursor for ever.
    """
    answer: dict | None = None
    try:
        for _page in range(MAX_STAY_PAGES):
            if cache.stays_cursor is None:
                answer = client.get_stays()
                cache.apply_lane(answer)
                cache.replace_stays(answer.get("open") or [], answer["cursor"])
                log.info("stays resynced: %d open, cursor=%s", len(cache.stays), cache.stays_cursor)
                return answer
            answer = client.get_stays(since=cache.stays_cursor)
            # THE CLOSING RIDES THIS READ (platform 0030): a lane closed or
            # reopened reaches the barrier inside one fast cadence, not five
            # minutes later on the slow one.
            cache.apply_lane(answer)
            cache.apply_stay_changes(answer.get("changes") or [], answer["cursor"])
            if not answer.get("more"):
                return answer
        log.warning(
            "stays delta still says more after %d pages; the next read continues from %s",
            MAX_STAY_PAGES, cache.stays_cursor,
        )
        return answer
    except PlatformUnreachable as err:
        log.warning("stays sync failed, keeping the cache we have: %s", err)
        return None
    except PlatformRejected as err:
        log.error("stays sync refused (%s); the next read takes the full set: %s", err.status, err)
        cache.stays_cursor = None
        return None


#: How many delta pages one `sync_stays` follows before handing the cadence
#: back. A bound on one call, not on the feed: the cursor is wherever the last
#: page left it, and the next tick continues from there.
MAX_STAY_PAGES = 50


def _reader_shown(event) -> dict:
    """`{"reader_shown": ...}` when the close recorded what the reader showed, else {}."""
    shown = event.detail.get("reader_shown")
    return {"reader_shown": shown} if shown is not None else {}


def require_descriptor_echo(
    result: dict | None, sent: str | None, *, end: str = "entry", action: str = "open"
) -> None:
    """Refuse a session action the platform accepted without recording the descriptor it was sent.

    The same silence as `require_confirmation_echo` below, on a different field
    and with one difference: the descriptor is OPTIONAL. An action that sent
    none has nothing to require, and this returns. One that sent a descriptor
    requires the platform to hand it back on the row -- because both routes
    destructure the keys they know and ignore the rest, so a platform older
    than the column (`sessions.entry_descriptor`, migration 0009;
    `sessions.exit_descriptor`, 0010) accepts the call, does the thing, and
    drops the field with nothing anywhere saying so. To the search, that stay
    is then unmatchable, or that exit uncomparable, silently. So an action that
    does not come back carrying the descriptor it was sent is NOT DELIVERED:
    counted, logged at error, dropped rather than re-sent for ever -- the same
    path as the confirmation, and for the same reason. The barrier already
    opened; what is lost is this lane's report, and the log line is what says
    so.

    ONE function for both ends, as the confirmation's is, and a seam on purpose
    (`BREAK_DESCRIPTOR=echo` in the fail-control).
    """
    if sent is None:
        return
    field = f"{end}_descriptor"
    migration = {"entry": "0009", "exit": "0010"}[end]
    echoed = ((result or {}).get("session") or {}).get(field)
    if echoed != sent:
        raise PlatformRejected(
            None,
            f"the {action} was accepted but the platform did not echo the descriptor it was sent "
            f"(it said {'nothing' if echoed is None else 'a different value'} for {field}). That "
            f"platform does not record an {end} descriptor: its migration {migration} goes before "
            "this lane build.",
        )


def require_confirmation_echo(
    result: dict | None, declared: str, *, end: str, action: str
) -> None:
    """Refuse a session action the platform accepted without recording what confirmed it.

    ONE function for both ends of a stay, because two copies of this rule would
    be two claims about the same thing and the copy is the one that goes wrong.

    What confirmed an entry or an exit travels WITH it, and a platform that
    knows the field refuses an action that does not carry it. THE OTHER
    DIRECTION IS THE SILENT ONE: a platform older than this lane accepts the
    call, answers with a session, and drops the field, because the column is not
    there and the route echoes the row it wrote. No error, nothing queued,
    nothing in a log -- and a confirmed session and an unconfirmable one become
    the same row.

    So an action that does not come back carrying the value it was sent is NOT
    DELIVERED. Its caller goes down `_guarded`, the path a stale close already
    takes: counted, logged at error, and dropped rather than re-sent forever
    with the whole outbox stuck behind it. The platform did do the thing -- an
    old route does -- so the car can still leave; what is lost is this lane's
    report of what saw it, and the log line is what says so.
    """
    field = f"{end}_confirmation"
    echoed = ((result or {}).get("session") or {}).get(field)
    if echoed != declared:
        raise PlatformRejected(
            None,
            f"the {action} was accepted but the platform did not echo "
            f"{field}={declared!r} (it said {echoed!r}). That platform does not "
            f"record what confirmed an {end}: its migration 0005 goes before this lane build.",
        )


class PlatformTransport(EventTransport):
    """Delivers the outbox to the platform.

    The queue holds two kinds of thing: session actions, which have to reach
    their own endpoints and in order, and everything else, which is log. Both
    ride the same queue so that a lane which was offline replays what happened
    in the order it happened.

    Every endpoint this calls is idempotent, which is what lets `send` be all
    or nothing: on any unreachable error the whole batch is retried, duplicates
    and all, and the platform absorbs them. The queue hands it one part at a
    time (`EventQueue.flush`) -- a session action, or a chunk of log events --
    so what is retried is that part, not everything queued.
    """

    def __init__(
        self,
        client: PlatformClient,
        *,
        session_lookup: Callable[[str], dict | None] | None = None,
    ) -> None:
        self._client = client
        #: At an exit, how a close that carries no session id has its session
        #: named: the platform's open stay for the plate, asked as the close is
        #: DELIVERED -- on the drain's thread, never the lane's. None (an entry
        #: lane, a test) sends a close as it was recorded.
        self.session_lookup = session_lookup
        self.rejected = 0
        #: Every platform call this transport has ATTEMPTED. Without it, a lane
        #: that has never sent anything and a lane whose every send was accepted
        #: are the same number, and `clock_skew_rejected` would answer `ok` --
        #: a confident negative about a question nobody asked.
        self.attempted = 0
        #: Refusals whose named reason was the platform's clock-skew code,
        #: SINCE THE LAST WRITE THE PLATFORM ACCEPTED. A lane whose clock runs
        #: fast has its session opens and closes dead-lettered, and the money
        #: record loses them.
        #:
        #: It is cleared by an accepted write rather than counted for the life
        #: of the process, because `clock_skew_rejected` is a STATE and a state
        #: has to be able to leave. Counted forever, one skew held the code
        #: `active` however long ago the clock was fixed, `recovered` could never
        #: fire at a monitor, and the surface trained its reader to skim it.
        self.skew_rejected = 0
        #: CONFLICTS the platform did not name, SINCE THE LAST ACCEPTED WRITE.
        #: A platform older than the field answers every refusal this way, so
        #: this is the count that stops a missing name being read as "not a
        #: skew". Separate from `rejected`, which counts every terminal refusal
        #: whatever it was and is never cleared.
        self.conflicts_unnamed = 0
        self.last_close: dict | None = None

    def send(self, events: list[LaneEvent]) -> bool:
        plain: list[dict] = []
        try:
            for event in events:
                if event.kind == SESSION_OPEN:
                    self._guarded(lambda e=event: self._open_session(e))
                elif event.kind == SESSION_CLOSE:
                    result = self._guarded(lambda e=event: self._close_session(e))
                    if result is not None:
                        self.last_close = result
                else:
                    plain.append(
                        {
                            "event_id": event.event_id,
                            "kind": event.kind,
                            "occurred_at": to_iso(event.at),
                            "detail": event.detail,
                        }
                    )
            if plain:
                # The one call on this surface that was not guarded, and the
                # platform now refuses a kind it does not know. Unguarded, that
                # 4xx left `flush()`, left `run_once()` and killed the process
                # -- after the barrier had already opened -- taking a
                # memory-only outbox with it, while `rejected` stayed at zero.
                # A batched log post is one lost batch, counted and logged with
                # the status and the kind the platform named, and the queue
                # keeps draining behind it.
                self._post_log(plain)
        except PlatformUnreachable as err:
            log.info("platform unreachable, %d item(s) stay queued: %s", len(events), err)
            return False
        return True

    def _post_log(self, plain: list[dict]) -> None:
        """Post log events, in halves while the body is larger than the
        platform takes: over `LOG_CHUNK_BYTES` before it is sent, or answered
        413 after. A 413 is not a refusal of these events -- the platform did
        not read them -- so it is not counted; the halves are posted, and one
        refused on its own is one lost post, counted and logged as any other.
        Only a single event too large to post alone is refused for its size.
        """
        if len(plain) > 1 and _body_bytes(plain) > LOG_CHUNK_BYTES:
            self._post_halves(plain)
            return

        def post():
            try:
                return self._client.post_events(plain)
            except PlatformRejected as err:
                if err.status == 413 and len(plain) > 1:
                    raise _TooLarge from err
                raise

        try:
            self._guarded(post)
        except _TooLarge:
            log.info("the platform answered 413 (too large) for %d log events; posting them "
                     "in halves", len(plain))
            self._post_halves(plain)

    def _post_halves(self, plain: list[dict]) -> None:
        half = len(plain) // 2
        self._post_log(plain[:half])
        self._post_log(plain[half:])

    def send_pictures(self, pictures) -> bool:
        """The pictures kept with an entry (U7f), one request each. True only if
        every one was taken; a refusal is counted and logged as any other and
        the picture is not sent again."""
        try:
            for picture in pictures:
                self._guarded(
                    lambda p=picture: self._client.post_picture(
                        picture_ref=p.picture_ref,
                        event_id=p.event_id,
                        taken_at=to_iso(p.taken_at),
                        image=p.image_bytes,
                    )
                )
        except PlatformUnreachable as err:
            log.info("platform unreachable, %d picture(s) stay queued: %s", len(pictures), err)
            return False
        return True

    def _open_session(self, event: LaneEvent) -> dict:
        """Open the session, and require the platform to say it recorded WHAT
        confirmed it. The rule is `require_confirmation_echo` above.
        """
        declared = event.detail["entry_confirmation"]
        # `.get`, and no key on most opens: only a read the identity service
        # produced a descriptor for carries one (`_session_descriptor`).
        descriptor = event.detail.get("descriptor")
        result = self._client.open_session(
            event_id=event.event_id,
            # EXACTLY ONE of the two, and which one is decided where the record
            # was written -- `LaneController._identity_detail` -- not here. A
            # `.get` on both rather than a `[...]` on the plate: a stay opened
            # on a ticket carries no plate key at all, and a KeyError on this
            # path would kill the flush after the barrier had already opened.
            plate=event.detail.get("plate"),
            ticket_ref=event.detail.get("ticket_ref"),
            entry_at=event.detail.get("at") or to_iso(event.at),
            plate_region=event.detail.get("plate_region"),
            entry_confirmation=declared,
            descriptor=descriptor,
            # Only when true: every other open calls the client as it always did.
            **({"tailgater": True} if event.detail.get("tailgater") else {}),
            # A plate nobody could read: the third identity (platform 0036).
            **({"plate_unreadable": True} if event.detail.get("plate_unreadable") else {}),
        )
        require_confirmation_echo(result, declared, end="entry", action="open")
        require_descriptor_echo(result, descriptor)
        return result

    def _close_session(self, event: LaneEvent) -> dict:
        """Close the session, and require the platform to say it recorded WHAT
        confirmed the exit.

        The same silence, at the other end of the stay, through the same
        function -- not a second check that can come to disagree with the
        first. The close route answers with the row it wrote, so
        `exit_confirmation` comes back from a platform that has the column and
        is simply absent from one that does not, with no error either way.
        A close that does not come back carrying the value it was sent is NOT
        DELIVERED: it goes down `_guarded`, counted and logged at error, and
        `last_close` is left alone rather than being set from a response that
        does not say what closed the stay.
        """
        declared = event.detail["exit_confirmation"]
        descriptor = event.detail.get("descriptor")
        session_id = event.detail.get("session_id") or self._open_session_id(event)
        result = self._client.close_session(
            event_id=event.event_id,
            plate=event.detail.get("plate"),
            ticket_ref=event.detail.get("ticket_ref"),
            exit_at=event.detail.get("at") or to_iso(event.at),
            # The session the platform has open for this car, asked as the
            # close goes out (`_open_session_id`). A close delivered late is
            # matched as one sent on the plate alone would be: the platform's
            # own order check refuses an exit that precedes the open stay's
            # entry, so a car that came back meanwhile keeps its later visit.
            session_id=session_id,
            exit_confirmation=declared,
            descriptor=descriptor,
            # The barrier's decision, as recorded at the exit. The platform
            # consumes it or says why not; either way the row answers.
            local_decision=event.detail.get("local_decision"),
            # What the reader showed, when a screen said (A2.2); absent, the
            # close is the one this lane sent before it existed.
            **_reader_shown(event),
        )
        require_confirmation_echo(result, declared, end="exit", action="close")
        require_descriptor_echo(result, descriptor, end="exit", action="close")
        return result

    def _open_session_id(self, event: LaneEvent) -> str | None:
        """The id of the session the platform has open for this close's
        plate, or None: no lookup wired, a ticket (the lookup takes a plate),
        nothing open, or a platform that could not be asked -- the close then
        goes out without one and the platform matches it on the identity.

        HERE, AND NOT WHERE THE CLOSE IS RECORDED. This runs on whichever
        thread drains the outbox -- under `LaneRunner` its own -- so a
        platform that does not answer costs the drain its timeout, and the
        car behind at the barrier nothing."""
        plate = event.detail.get("plate")
        if self.session_lookup is None or not plate:
            return None
        found = self.session_lookup(plate)
        if not found:
            return None
        return (found.get("session") or {}).get("id")

    def _guarded(self, call):
        """Run one platform call, dropping it if the platform refused it outright.

        A rejected item is poison: retrying it forever would block every later
        item behind it. So it is dropped -- but counted and logged, never
        silently, because a dropped session is a gap in the money record.

        This is why the platform answers a stale exit with 409 rather than
        letting a constraint surface as 500: a 5xx is classified unreachable
        above, and would be re-sent forever with the whole outbox stuck behind
        it. A terminal refusal is the difference between one lost item and a
        lane that never reports again.
        """
        self.attempted += 1
        try:
            result = call()
        except PlatformRejected as err:
            self.rejected += 1
            self._classify(err)
            log.error("platform refused an item, dropping it: %s", err)
            return None
        # THE WAY BACK. A write the platform ACCEPTED is a platform that took
        # this lane's clock, so whatever it refused before is over: the skew is
        # fixed, or the conflict it would not name is behind us. Without this
        # both counts only ever go up and `clock_skew_rejected` is a latch that
        # reads like a state -- `active` for the life of the process, with the
        # operator's repair invisible and no `recovered` ever sent.
        self.skew_rejected = 0
        self.conflicts_unnamed = 0
        return result

    def _classify(self, err: PlatformRejected) -> None:
        """Separate the one refusal that is a MALFUNCTION from the ordinary ones.

        A 409 is the platform's terminal refusal and seven different conditions
        produce one. Six are ordinary -- a wrong lane direction, a re-used event
        id, a stale exit. The seventh is a clock skew, and it means every
        session open and close this lane sends is being dropped: the barrier
        still works, the driver still gets in, and the money record silently
        loses the stay.

        Only a 409 is looked at. A 400 is a malformed request and a 404 is a
        session that is not there; neither can be a skew, and counting them here
        would make an ordinary bad request read as a clock this build could not
        classify.

        An unnamed conflict is counted SEPARATELY and is never treated as "not a
        skew". A platform that predates the `code` field refuses a skew exactly
        as it refuses everything else, so reading the absence as a negative
        would report a healthy clock on precisely the deployment where the
        failure is invisible.
        """
        if err.status != 409:
            return
        if err.code == CLOCK_SKEW_CODE:
            self.skew_rejected += 1
        elif err.code is None:
            self.conflicts_unnamed += 1
