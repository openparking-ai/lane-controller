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

    # -- the PlatformClient surface ---------------------------------------

    def _check(self):
        if not self.online:
            raise PlatformUnreachable("simulated outage")

    def get_rules(self) -> dict:
        self._check()
        return {
            "garage_id": "garage-1",
            "currency": "USD",
            "hourly_minor": 250,
            "default_action": self.default_action,
            "plate_rules": [],
        }

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
