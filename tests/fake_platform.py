"""A platform that can be switched off mid-run.

Stands in for the HTTP layer only. It records what it received and deduplicates
exactly as the real platform does, so a test can assert that a reconnecting
lane produced no duplicates rather than assuming it.
"""

from __future__ import annotations

from lane_controller.platform_client import PlatformRejected, PlatformUnreachable


class FakePlatform:
    def __init__(self, *, online: bool = True, default_action: str = "allow") -> None:
        self.online = online
        self.default_action = default_action
        self.events: dict[str, dict] = {}  # event_id -> event
        self.event_deliveries = 0  # every accepted item, duplicates included
        self.opened: list[dict] = []
        self.closed: list[dict] = []
        self.open_sessions: dict[str, dict] = {}
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

    def open_session(self, *, plate: str, entry_at: str, plate_region=None) -> dict:
        self._check()
        self.opened.append({"plate": plate, "entry_at": entry_at})
        if plate in self.open_sessions:
            return {"session": self.open_sessions[plate], "created": False}
        session = {"plate": plate, "entry_at": entry_at, "fee_minor": None}
        self.open_sessions[plate] = session
        return {"session": session, "created": True}

    def close_session(self, *, plate: str, exit_at: str) -> dict:
        self._check()
        self.closed.append({"plate": plate, "exit_at": exit_at})
        session = self.open_sessions.pop(plate, None)
        if session is None:
            if self.reject_close_without_open:
                raise PlatformRejected(404, "no open session for this vehicle")
            return {"session": None, "replay": True}
        session = {**session, "exit_at": exit_at, "fee_minor": 250}
        return {"session": session, "closed": True}

    # -- what the tests assert on -----------------------------------------

    @property
    def unique_events(self) -> int:
        return len(self.events)

    @property
    def unique_opens(self) -> int:
        return len({(o["plate"], o["entry_at"]) for o in self.opened})
