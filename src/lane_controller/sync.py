"""Keeping the local decision cache fresh, and the outbox draining.

Both are best effort by design. The lane decides from what it already has; this
module only changes what it has, and never blocks a barrier.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from .decision import DecisionCache, Rule
from .events import EventTransport, LaneEvent
from .platform_client import PlatformClient, PlatformRejected, PlatformUnreachable

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

#: What the platform is told confirmed a session. `confirmed` means two loops
#: after the gate saw a vehicle cross them forward. `unconfirmable` means this
#: lane has no closing loops and nothing could have confirmed or refuted it --
#: which is the honest name for it, and is not the same word as `confirmed`.
CONFIRMED = "confirmed"
UNCONFIRMABLE = "unconfirmable"


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
        self.last_close: dict | None = None

    def send(self, events: list[LaneEvent]) -> bool:
        plain: list[dict] = []
        try:
            for event in events:
                if event.kind == SESSION_OPEN:
                    self._guarded(
                        lambda e=event: self._client.open_session(
                            event_id=e.event_id,
                            plate=e.detail["plate"],
                            entry_at=e.detail.get("at") or to_iso(e.at),
                            plate_region=e.detail.get("plate_region"),
                            # What confirmed this entry travels WITH it. The
                            # platform refuses an open that does not say, so a
                            # session can never exist without the record
                            # carrying whether two loops saw the car cross.
                            entry_confirmation=e.detail["entry_confirmation"],
                        )
                    )
                elif event.kind == SESSION_CLOSE:
                    result = self._guarded(
                        lambda e=event: self._client.close_session(
                            event_id=e.event_id,
                            plate=e.detail["plate"],
                            exit_at=e.detail.get("at") or to_iso(e.at),
                            # Recorded at the moment of the exit, when it was
                            # still unambiguous which session was open. By the
                            # time a queued close is delivered it may not be.
                            session_id=e.detail.get("session_id"),
                            exit_confirmation=e.detail["exit_confirmation"],
                        )
                    )
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
                self._client.post_events(plain)
        except PlatformUnreachable as err:
            log.info("platform unreachable, %d item(s) stay queued: %s", len(events), err)
            return False
        return True

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
        try:
            return call()
        except PlatformRejected as err:
            self.rejected += 1
            log.error("platform refused an item, dropping it: %s", err)
            return None
