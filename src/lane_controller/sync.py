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
                        )
                    )
                elif event.kind == SESSION_CLOSE:
                    result = self._guarded(
                        lambda e=event: self._client.close_session(
                            event_id=e.event_id,
                            plate=e.detail["plate"],
                            exit_at=e.detail.get("at") or to_iso(e.at),
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
