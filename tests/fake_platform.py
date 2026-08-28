"""A platform that can be switched off mid-run.

Stands in for the HTTP layer only. It records what it received and deduplicates
exactly as the real platform does, so a test can assert that a reconnecting
lane produced no duplicates rather than assuming it.
"""

from __future__ import annotations

from lane_controller.platform_client import PlatformRejected, PlatformUnreachable
from lane_controller.sync import CONFIRMED, UNCONFIRMABLE

#: What the platform accepts as an answer to "what confirmed this?". Taken from
#: the names the lane publishes rather than written out here, so the fake and
#: the lane cannot come to disagree about the vocabulary.
ACCEPTED_CONFIRMATIONS = frozenset({CONFIRMED, UNCONFIRMABLE})


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
        accepted = 0
        for event in events:
            self.event_deliveries += 1
            if event["event_id"] not in self.events:
                self.events[event["event_id"]] = event
                accepted += 1
        return {"accepted": accepted, "duplicates": len(events) - accepted}

    def open_session(
        self,
        *,
        event_id: str,
        plate: str,
        entry_at: str,
        entry_confirmation: str,
        plate_region=None,
    ) -> dict:
        self._check()
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
                "entry_at": entry_at,
                "entry_confirmation": entry_confirmation,
            }
        )
        # Keyed on the event, exactly as the platform is. An entry replayed
        # after the car has left must resolve to the session it originally
        # opened -- not open a second one.
        if event_id in self.sessions_by_open_event:
            return {"session": self.sessions_by_open_event[event_id], "created": False}
        if plate in self.open_sessions:
            return {"session": self.open_sessions[plate], "created": False}
        session = {"plate": plate, "entry_at": entry_at, "fee_minor": None}
        self.open_sessions[plate] = session
        self.sessions_by_open_event[event_id] = session
        return {"session": session, "created": True}

    def find_open_session(self, *, plate: str) -> dict | None:
        if not self.online:
            return None
        session = self.open_sessions.get(plate)
        return {"session": {**session, "id": id(session)}} if session else None

    def close_session(
        self,
        *,
        event_id: str,
        plate: str,
        exit_at: str,
        exit_confirmation: str,
        session_id: str | None = None,
    ) -> dict:
        self._check()
        if exit_confirmation not in ACCEPTED_CONFIRMATIONS:
            raise PlatformRejected(400, f"exit_confirmation {exit_confirmation!r} is not one of "
                                       f"{sorted(ACCEPTED_CONFIRMATIONS)}")
        self.closed.append(
            {
                "event_id": event_id,
                "plate": plate,
                "exit_at": exit_at,
                "session_id": session_id,
                "exit_confirmation": exit_confirmation,
            }
        )
        if event_id in self.sessions_by_close_event:
            return {"session": self.sessions_by_close_event[event_id], "replay": True}
        session = self.open_sessions.pop(plate, None)
        if session is None:
            if self.reject_close_without_open:
                raise PlatformRejected(404, "no open session for this vehicle")
            return {"session": None, "replay": True}
        session = {**session, "exit_at": exit_at, "fee_minor": 250}
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
