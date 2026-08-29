"""ONE consumer, used against our lane and against a third party's.

This is the shape the intercom agent will be: something that reads a lane over
the contract and decides what to do about a vehicle. It is here, in the tests,
because the point it exists to prove is a property of the CONTRACT, and the
proof only counts if the same code reads both lanes.

**It contains no branch on which lane it is talking to.** No hostname check, no
vendor field, no "if this is ours". If it ever needs one, the contract is wrong
and this file is where that shows up.

It also encodes the two behaviours the contract REQUIRES of a consumer, so that
they are exercised rather than described:

  * a `reason` it does not recognise is ESCALATED, never mapped onto the
    nearest code it knows;
  * a health `state` of `unknown` is never read as `ok`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class Escalate(Exception):
    """What a consumer does with a reason it cannot interpret.

    Not an error in the lane and not a failure of the read: it is the correct
    outcome, and it means a human deals with this vehicle.
    """


class LaneConsumer:
    """Reads a lane. Any lane."""

    def __init__(self, base_url: str, token: str | None = None, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # --- the four routes --------------------------------------------------

    def lane(self) -> dict:
        return self._get("/v1/lane")

    def state(self) -> dict:
        return self._get("/v1/lane/state")

    def health(self) -> dict:
        return self._get("/v1/lane/health")

    def events(self, since: int = 0) -> dict:
        return self._get(f"/v1/lane/events?since={since}")

    # --- what the contract requires of whoever reads it -------------------

    def interpret(self, decision: dict, known: tuple[str, ...]) -> str:
        """The reason, or an escalation.

        `known` is the closed subset this consumer was built against. A reason
        outside it is not guessed at, not mapped to the nearest thing, and not
        quietly treated as an ordinary fallback -- it goes to a human. Under
        "never wrong silently" that is the only available answer.
        """
        reason = decision["reason"]
        if reason not in known:
            raise Escalate(reason)
        return reason

    @staticmethod
    def actionable(entry: dict) -> bool:
        """Whether a health entry says anything a monitor may act on.

        `unknown` is not `ok` and it is not `active`: nobody measured, so there
        is nothing to act on and nothing to be reassured by. And a code the
        contract marks `never_alarm` is never actionable however it reads.
        """
        if entry.get("never_alarm"):
            return False
        return entry["state"] == "active"

    # --- plumbing ---------------------------------------------------------

    def _get(self, path: str) -> dict:
        request = urllib.request.Request(f"{self.base_url}{path}", method="GET")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read())

    def post(self, path: str, body: bytes = b"{}") -> int:
        """Try to CHANGE something, and report the status.

        Here so a test can ask the question a consumer would: is there a route
        on this lane that does anything? It is not part of reading a lane.
        """
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code
