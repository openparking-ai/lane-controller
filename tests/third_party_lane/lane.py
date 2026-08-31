"""The third-party lane itself. Four routes, hand-built payloads, nothing else.

Written as an implementer would write it: from the document, with their own
vocabulary, on the standard library. Nothing here is imported from our lane
except the contract's two published closed sets -- see the package docstring.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from lane_controller.contract import NEVER_ALARM, MalfunctionCode

#: This vendor's own word for "the attendant took over at the barrier". It is
#: not in our `Fallback` and never will be. A consumer that maps it onto
#: `unknown_vehicle` because that is the nearest thing it knows has guessed
#: about a car, which is the one thing the contract forbids.
VENDOR_REASON = "barrier_operator_intervened"


def _break(name: str) -> bool:
    """Whether the fail-control asked for this payload to be broken.

    The stub's own breakages, so `scripts/contract_fail_control.py` can prove
    the seat test is capable of going red. A stub that quietly satisfied every
    assertion would be a fixture that measures nothing.
    """
    return os.environ.get("BREAK_THIRD_PARTY_LANE") == name


class ThirdPartyLane:
    """A lane with no loops, no identity service and no platform."""

    lane_id = "tp-lane-a"
    site_id = "tp-site"
    direction = "entry"

    def describe(self) -> dict:
        payload = {
            "lane_id": self.lane_id,
            "site_id": self.site_id,
            "direction": self.direction,
            "contract_version": 2,
            # No loops, so nothing to publish. Not `null` and not our five
            # keys: this lane has no geometry, and says so with an empty one.
            "geometry": {},
            # This vendor keeps a two-event window, and says so. The number is
            # a property of THEIR window, not of ours -- which is the reason
            # the contract publishes it instead of documenting one value.
            "event_window_depth": 2,
            "capabilities": {
                "confirms_entry": False,
                "has_identity_service": False,
                "has_platform": False,
                "has_display": False,
                "can_vend": False,
            },
        }
        if _break("no_direction"):
            # A required field removed. The seat test must go red.
            del payload["direction"]
        if _break("can_vend"):
            payload["capabilities"]["can_vend"] = True
        return payload

    def state(self) -> dict:
        payload = {
            "contract_version": 2,
            "decision": {
                "outcome": "fallback",
                "reason": VENDOR_REASON,
                # Not one of ours, so this is `null` -- which is exactly the
                # signal the contract says means ESCALATE.
                "fallback": None,
                "cause": None,
                "presence": None,
                "at": "2026-08-30T14:03:11.482913+00:00",
                "read_ref": None,
            },
            # This lane cannot confirm anything, so it never has a transit to
            # report. `none` is the honest state and it is always the state.
            "transit": {"state": "none", "since": None},
        }
        if _break("our_reason"):
            # The stub stops being a third party and starts speaking our
            # vocabulary. The seat test then proves nothing about a foreign
            # lane, so it must notice.
            payload["decision"]["reason"] = "low_confidence"
            payload["decision"]["fallback"] = "low_confidence"
        if _break("no_transit"):
            del payload["transit"]
        return payload

    def health(self) -> dict:
        # Every code in the contract version this lane claims, each `unknown`
        # with no source -- which is the truth for a lane with none of this
        # instrumentation, and is a complete payload rather than a short one.
        codes = [
            {
                "code": code.value,
                "state": "unknown",
                "source": "no_source",
                "never_alarm": code in NEVER_ALARM,
                "caveat": NEVER_ALARM.get(code),
            }
            for code in MalfunctionCode
        ]
        if _break("short_health"):
            codes = codes[:-1]
        if _break("no_source_label"):
            for entry in codes:
                del entry["source"]
        return {"contract_version": 2, "codes": codes}

    def events(self, since: int) -> dict:
        # This lane keeps a two-event log and nothing else.
        log = [
            {
                "cursor": 1,
                "event_id": "tp-0001",
                "kind": "attendant_called",
                "lane_id": self.lane_id,
                "occurred_at": "2026-08-30T14:03:10.000000+00:00",
                "detail": {},
            },
            {
                "cursor": 2,
                "event_id": "tp-0002",
                "kind": "attendant_opened",
                "lane_id": self.lane_id,
                "occurred_at": "2026-08-30T14:03:11.482913+00:00",
                "detail": {},
            },
        ]
        cursor = 2
        payload = {
            "contract_version": 2,
            "cursor": cursor,
            "reset": since > cursor,
            "dropped": 0,
            "events": [item for item in log if item["cursor"] > since],
        }
        if _break("no_reset"):
            payload["reset"] = False
        return payload


class _Handler(BaseHTTPRequestHandler):
    lane: ThirdPartyLane

    server_version = "third-party-lane"
    sys_version = ""

    def log_message(self, fmt: str, *args) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/v1/lane":
            return self._json(200, self.lane.describe())
        if url.path == "/v1/lane/state":
            return self._json(200, self.lane.state())
        if url.path == "/v1/lane/health":
            return self._json(200, self.lane.health())
        if url.path == "/v1/lane/events":
            raw = parse_qs(url.query).get("since", ["0"])[0]
            try:
                since = int(raw)
            except ValueError:
                return self._json(400, {"error": "since must be an integer"})
            return self._json(200, self.lane.events(since))
        return self._json(404, {"error": "no such route"})

    def do_POST(self) -> None:  # noqa: N802
        # THIS LANE HAS NO ACT SURFACE, and that is the point of it now. Our
        # lane grew one in version 2; a lane that is not ours implements the
        # same contract version and declares `can_vend: false`, because
        # implementing the act side is optional and a consumer must have a case
        # for a lane that cannot open anything for it.
        #
        # A consumer asking "can I make you do something" gets a different
        # answer from the two lanes and needs no branch to ask the question.
        self.send_response(405)
        self.send_header("Allow", "GET")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_server(lane: ThirdPartyLane | None = None, host: str = "127.0.0.1", port: int = 0):
    handler = type("_BoundHandler", (_Handler,), {"lane": lane or ThirdPartyLane()})
    return ThreadingHTTPServer((host, port), handler)
