"""Keeping the local decision cache fresh, and the outbox draining.

Both are best effort by design. The lane decides from what it already has; this
module only changes what it has, and never blocks a barrier.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from .decision import DecisionCache, Rule
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

ENTRY_PENDING = "entry_pending"
ENTRY_CONFIRMED = "entry_confirmed"
ENTRY_BACKED_OUT = "entry_backed_out"
ENTRY_HELD = "entry_held"
ENTRY_UNCONFIRMABLE = "entry_unconfirmable"

EXIT_PENDING = "exit_pending"
EXIT_CONFIRMED = "exit_confirmed"
EXIT_BACKED_IN = "exit_backed_in"
EXIT_HELD = "exit_held"
EXIT_UNCONFIRMABLE = "exit_unconfirmable"

#: Why an entry or an exit ended the way it did. One reason per outcome, and
#: the reason travels to the platform with the session so the money record can
#: say what confirmed it.
REASON_FORWARD = "closing_sequence_forward"
REASON_REVERSE = "closing_sequence_reverse"
REASON_WINDOW_ELAPSED = "confirmation_window_elapsed"
REASON_NO_CLOSING_LOOPS = "no_closing_loops_configured"
REASON_ARMING_INCOMPLETE = "only_one_arming_loop_occupied"
#: The closing-loop driver did not return inside the lane's own settle deadline
#: -- the confirmation window plus `[lane] settle_grace_s`. NOT the same fact as
#: `confirmation_window_elapsed`, which is the loops answering that nothing
#: crossed: this is the loops not answering at all, and the two want different
#: repairs.
REASON_LOOP_DRIVER_TIMEOUT = "loop_driver_timeout"

#: What the platform is told confirmed a session. `confirmed` means two loops
#: after the gate saw a vehicle cross them forward. `unconfirmable` means this
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

    plate_rules = [
        Rule(plate=r["plate"], allow=bool(r.get("allow", False)), rate_plan=r.get("rate_plan"))
        for r in payload.get("plate_rules", [])
    ]
    cache.load(plate_rules, default_action=payload.get("default_action"))
    log.info("rules synced: %d plate rule(s), default=%s", len(plate_rules), cache.default_action)
    return payload


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
    and all, and the platform absorbs them.
    """

    def __init__(self, client: PlatformClient) -> None:
        self._client = client
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
                self._guarded(lambda: self._client.post_events(plain))
        except PlatformUnreachable as err:
            log.info("platform unreachable, %d item(s) stay queued: %s", len(events), err)
            return False
        return True

    def _open_session(self, event: LaneEvent) -> dict:
        """Open the session, and require the platform to say it recorded WHAT
        confirmed it. The rule is `require_confirmation_echo` above.
        """
        declared = event.detail["entry_confirmation"]
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
        )
        require_confirmation_echo(result, declared, end="entry", action="open")
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
        result = self._client.close_session(
            event_id=event.event_id,
            plate=event.detail.get("plate"),
            ticket_ref=event.detail.get("ticket_ref"),
            exit_at=event.detail.get("at") or to_iso(event.at),
            # Recorded at the moment of the exit, when it was still unambiguous
            # which session was open. By the time a queued close is delivered it
            # may not be.
            session_id=event.detail.get("session_id"),
            exit_confirmation=declared,
        )
        require_confirmation_echo(result, declared, end="exit", action="close")
        return result

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
