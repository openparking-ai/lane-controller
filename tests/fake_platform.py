"""A platform that can be switched off mid-run.

Stands in for the HTTP layer only. It records what it received and deduplicates
exactly as the real platform does, so a test can assert that a reconnecting
lane produced no duplicates rather than assuming it.
"""

from __future__ import annotations

from lane_controller.platform_client import PlatformRejected, PlatformUnreachable
from lane_controller.sync import CONFIRMED, HELD, UNCONFIRMABLE

#: What the platform accepts as an answer to "what confirmed this?". Taken from
#: the names the lane publishes rather than written out here, so the fake and
#: the lane cannot come to disagree about the vocabulary.
#:
#: The two sets are DIFFERENT and the difference is the decision, not an
#: oversight: an exit the loops did not confirm still closes and bills, marked
#: `held`, because the barrier opened and the car is gone. An entry nothing
#: confirmed is not a session at all, so `held` on an open is refused.
ACCEPTED_CONFIRMATIONS = frozenset({CONFIRMED, UNCONFIRMABLE})
ACCEPTED_EXIT_CONFIRMATIONS = frozenset({CONFIRMED, UNCONFIRMABLE, HELD})


class FakePlatform:
    def __init__(self, *, online: bool = True, default_action: str = "allow") -> None:
        self.online = online
        self.default_action = default_action
        self.events: dict[str, dict] = {}  # event_id -> event
        self.event_deliveries = 0  # every accepted item, duplicates included
        self.opened: list[dict] = []
        self.closed: list[dict] = []
        self.open_sessions: dict[str, dict] = {}
        self.sessions_by_open_event: dict[str, dict] = {}
        self.sessions_by_close_event: dict[str, dict] = {}
        self.reject_close_without_open = True
        #: Kinds this platform refuses outright, as the real one refuses a kind
        #: it does not know about. The SET is supplied by whoever builds the
        #: fake: which kinds the real platform accepts is its own list, in
        #: another repository, and a copy of it here would be exactly the second
        #: copy the kind-set check exists to catch.
        self.reject_event_kinds: set[str] = set()
        #: What `get_rules` and `get_stays` serve (platform 0016), and what a
        #: test moves to make a delta.
        self.rate_plans: list[dict] = [{"plan_version": "flat-250-USD", "currency": "USD"}]
        self.entitlements: dict = {"complete": True,
                                   "garage_pass": {"consulted": False, "reason": "not linked"},
                                   "monthly_billing": {"consulted": False, "reason": "not linked"}}
        self.stays: list[dict] = []
        self.cursor = 0
        self.stay_page = 500
        self.rules_reads = 0
        self.stays_reads: list[str | None] = []

    # -- the PlatformClient surface ---------------------------------------

    def _check(self):
        if not self.online:
            raise PlatformUnreachable("simulated outage")

    def get_rules(self) -> dict:
        """The payload as platform 0016 serves it: the plans whole, the space
        class, each module's register (or its unavailability), the open stays
        with their cursor. `hourly_minor` is gone from the real one and is
        gone from here."""
        self._check()
        self.rules_reads += 1
        return {
            "garage_id": "garage-1",
            "currency": "USD",
            "space_class": "standard",
            "default_action": self.default_action,
            "active": True,
            "rate_plans": list(self.rate_plans),
            "entitlements": {"read_at": "2026-09-21T00:00:00Z", **self.entitlements},
            "stays": {"cursor": str(self.cursor), "open": [s for s in self.stays if s["open"]]},
            "synced_at": "2026-09-21T00:00:00Z",
        }

    def get_stays(self, since: str | None = None) -> dict:
        """`GET /lane/stays`: the full open set, or every change past `since`
        in cursor order, closed rows included, paged by `stay_page`."""
        self._check()
        self.stays_reads.append(since)
        if since is None:
            return {"cursor": str(self.cursor), "open": [s for s in self.stays if s["open"]]}
        if not since.isdigit():
            raise PlatformRejected(400, "since must be a cursor this route handed out")
        changed = [s for s in self.stays if int(s["change_seq"]) > int(since)]
        page = changed[: self.stay_page]
        more = len(changed) > self.stay_page
        cursor = page[-1]["change_seq"] if page else since
        return {"since": since, "cursor": cursor, "changes": page, "more": more}

    # -- the stays the fake holds, and how a test moves them ------------------

    def stay_opened(self, session_id: str, plate: str | None = None, ticket_ref: str | None = None):
        self.cursor += 1
        stay = {
            "session_id": session_id, "open": True, "plate": plate, "plate_region": None,
            "ticket_ref": ticket_ref, "entry_at": "2026-09-21T10:00:00Z", "entry_lane": "E",
            "change_seq": str(self.cursor),
        }
        self.stays = [s for s in self.stays if s["session_id"] != session_id] + [stay]
        return stay

    def stay_closed(self, session_id: str):
        self.cursor += 1
        for i, s in enumerate(self.stays):
            if s["session_id"] == session_id:
                self.stays[i] = {**s, "open": False, "change_seq": str(self.cursor)}
                return self.stays[i]
        raise KeyError(session_id)

    def post_events(self, events: list[dict]) -> dict:
        self._check()
        for event in events:
            if event["kind"] in self.reject_event_kinds:
                raise PlatformRejected(
                    400,
                    f"kind {event['kind']!r} is not one a lane reports",
                )
        accepted = 0
        for event in events:
            self.event_deliveries += 1
            if event["event_id"] not in self.events:
                self.events[event["event_id"]] = event
                accepted += 1
        return {"accepted": accepted, "duplicates": len(events) - accepted}

    @staticmethod
    def _identity(plate, ticket_ref) -> str:
        """The one identity this stay is against, refused if it is not one.

        The real route applies exactly this rule (`laneIdentity` in
        `platform/src/app.js`) and answers 400. A fake that accepted both, or
        neither, would let the lane's side pass against a platform that would
        have refused it.
        """
        if bool(plate) == bool(ticket_ref):
            raise PlatformRejected(
                400,
                "exactly one of plate or ticket_ref is required in the body; "
                f"this request sent {'both' if plate else 'neither'}",
            )
        return plate or ticket_ref

    def open_session(
        self,
        *,
        event_id: str,
        entry_at: str,
        entry_confirmation: str,
        plate: str | None = None,
        ticket_ref: str | None = None,
        plate_region=None,
        descriptor: str | None = None,
    ) -> dict:
        self._check()
        identity = self._identity(plate, ticket_ref)
        # Refused here exactly as the real platform refuses it. A fake that
        # accepted an open with no confirmation would let the lane's side of
        # this pass while the contract it is written against says no.
        if entry_confirmation not in ACCEPTED_CONFIRMATIONS:
            raise PlatformRejected(400, f"entry_confirmation {entry_confirmation!r} is not one of "
                                        f"{sorted(ACCEPTED_CONFIRMATIONS)}")
        self.opened.append(
            {
                "event_id": event_id,
                "plate": plate,
                "ticket_ref": ticket_ref,
                "entry_at": entry_at,
                "entry_confirmation": entry_confirmation,
                "descriptor": descriptor,
            }
        )
        # Keyed on the event, exactly as the platform is. An entry replayed
        # after the car has left must resolve to the session it originally
        # opened -- not open a second one.
        if event_id in self.sessions_by_open_event:
            return {"session": self.sessions_by_open_event[event_id], "created": False}
        if identity in self.open_sessions:
            return {"session": self.open_sessions[identity], "created": False}
        # Echoed back, exactly as the route does -- it answers with the row it
        # wrote, and `PlatformClient.open_session` refuses an open that comes
        # back without the value it sent. A fake that did not echo would make
        # every lane test look like a lane talking to a platform too old to
        # record the field.
        session = {
            "plate": plate,
            "ticket_ref": ticket_ref,
            "entry_at": entry_at,
            "fee_minor": None,
            "entry_confirmation": entry_confirmation,
            # Echoed on the row exactly as the route does (migration 0009):
            # null when the open sent none, the value when it did. The lane
            # requires the echo only when it sent one.
            "entry_descriptor": descriptor,
        }
        self.open_sessions[identity] = session
        self.sessions_by_open_event[event_id] = session
        return {"session": session, "created": True}

    def find_open_session(self, *, plate: str | None = None, ticket_ref: str | None = None):
        if not self.online:
            return None
        session = self.open_sessions.get(plate or ticket_ref)
        return {"session": {**session, "id": id(session)}} if session else None

    def close_session(
        self,
        *,
        event_id: str,
        exit_at: str,
        exit_confirmation: str,
        plate: str | None = None,
        ticket_ref: str | None = None,
        session_id: str | None = None,
        descriptor: str | None = None,
    ) -> dict:
        self._check()
        identity = self._identity(plate, ticket_ref)
        if exit_confirmation not in ACCEPTED_EXIT_CONFIRMATIONS:
            raise PlatformRejected(400, f"exit_confirmation {exit_confirmation!r} is not one of "
                                       f"{sorted(ACCEPTED_EXIT_CONFIRMATIONS)}")
        self.closed.append(
            {
                "event_id": event_id,
                "plate": plate,
                "ticket_ref": ticket_ref,
                "exit_at": exit_at,
                "session_id": session_id,
                "exit_confirmation": exit_confirmation,
                "descriptor": descriptor,
            }
        )
        if event_id in self.sessions_by_close_event:
            return {"session": self.sessions_by_close_event[event_id], "replay": True}
        session = self.open_sessions.pop(identity, None)
        if session is None:
            if self.reject_close_without_open:
                raise PlatformRejected(404, "no open session for this vehicle")
            return {"session": None, "replay": True}
        # Echoed back, exactly as the close route does -- it answers with the
        # row it wrote, `exit_confirmation` with it, and
        # `PlatformTransport._close_session` refuses a close that comes back
        # without the value it sent. A fake that did not echo would make every
        # lane test look like a lane talking to a platform too old to record it.
        session = {
            **session,
            "exit_at": exit_at,
            "fee_minor": 250,
            "exit_confirmation": exit_confirmation,
            # Echoed as the close route does (migration 0010): null when the
            # close sent none, the value when it did.
            "exit_descriptor": descriptor,
        }
        self.sessions_by_close_event[event_id] = session
        return {"session": session, "closed": True}

    # -- what the tests assert on -----------------------------------------

    @property
    def unique_events(self) -> int:
        return len(self.events)

    @property
    def unique_opens(self) -> int:
        """Sessions actually created — not delivery attempts."""
        return len(self.sessions_by_open_event)

    @property
    def open_deliveries(self) -> int:
        return len(self.opened)
