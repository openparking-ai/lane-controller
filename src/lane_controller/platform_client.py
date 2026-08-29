"""HTTP client for the Open Parking AI platform.

Stdlib only. A lane controller runs on a box in a gate housing; every
dependency added here is one more thing to cross-compile, update and have go
wrong somewhere with no keyboard attached.

The distinction this module exists to draw is between *unreachable* and
*refused*. Unreachable means try again later and keep working from cache.
Refused means the platform understood us and said no, and retrying forever
would just be a loop.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class PlatformUnreachable(Exception):
    """Network failure, timeout, or a 5xx. Retryable -- keep the work queued."""


#: The name the platform's clock-skew refusal carries in its error body.
#:
#: SOURCE: `platform/src/app.js`, `export const CLOCK_SKEW`. It is exported
#: there rather than left inline precisely because this lane pins it: it is the
#: one refusal a lane derives a MALFUNCTION from, so the string is part of that
#: platform's published surface.
#:
#: A string and not a message match. `_cause()` below already decides every
#: identification failure from a structure rather than from message text, for
#: the reason that applies here too: a message gets reworded and a check keyed
#: on its words goes quietly wrong.
CLOCK_SKEW_CODE = "clock_skew"


class PlatformRejected(Exception):
    """The platform understood us and did not do what we asked. Not retryable.

    Two shapes, and the second is why the status is optional. A 4xx is the
    ordinary one. The other is a SUCCESS whose body does not carry a field the
    request sent: a platform older than the lane accepts the call and silently
    drops what it does not know about, and re-sending that forever is no more
    useful than re-sending a 400.

    `code` is the platform's own machine-readable name for the refusal, when it
    gave one. `None` covers three different situations and they are NOT folded
    together anywhere that reads it: this was not an HTTP refusal at all, or the
    body carried no name, or the body was not JSON. A platform too old to name
    its refusals answers `None` for every one of them, which is why a consumer
    of this field may not read a missing name as "not the code I was looking
    for".
    """

    def __init__(self, status: int | None, body: str, code: str | None = None) -> None:
        where = f"HTTP {status}: " if status is not None else ""
        super().__init__(f"platform rejected the request: {where}{body}")
        self.status = status
        self.body = body
        self.code = code


class PlatformClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 5.0, opener=None) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        # Injectable so tests can simulate an unreachable platform without
        # binding a socket or waiting for a real timeout.
        self._opener = opener or urllib.request.urlopen

    # -- plumbing ----------------------------------------------------------

    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("authorization", f"Bearer {self._token}")
        if data is not None:
            request.add_header("content-type", "application/json")

        try:
            with self._opener(request, timeout=self._timeout) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else None
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", "replace")
            if err.code >= 500:
                # The platform is having a bad time. That is the same situation
                # as it being unreachable, from the lane's point of view.
                raise PlatformUnreachable(f"HTTP {err.code}: {body}") from err
            raise PlatformRejected(err.code, body, _refusal_code(body)) from err
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise PlatformUnreachable(str(err)) from err

    # -- the lane surface --------------------------------------------------

    def get_rules(self) -> dict:
        return self._request("GET", "/api/v1/lane/rules")

    def post_events(self, events: list[dict]) -> dict:
        return self._request("POST", "/api/v1/lane/events", {"events": events})

    def open_session(
        self,
        *,
        event_id: str,
        plate: str,
        entry_at: str,
        entry_confirmation: str,
        plate_region: str | None = None,
    ) -> dict:
        # event_id is the lane's own, and it is what makes a re-sent flush safe.
        # The platform keys the session on it, so this exact arrival can only
        # ever produce one session however many times we deliver it.
        return self._request(
            "POST",
            "/api/v1/lane/sessions/open",
            {
                "event_id": event_id,
                "plate": plate,
                "entry_at": entry_at,
                "plate_region": plate_region,
                # Not optional and not defaulted, here or at the platform. A
                # default would be a second copy of a claim about what
                # confirmed an entry, and the copy is the one that lies.
                "entry_confirmation": entry_confirmation,
            },
        )

    def find_open_session(self, *, plate: str) -> dict | None:
        """The session currently open for this plate, if the platform is reachable.

        Best effort on purpose. Naming the session on the close is what stops a
        stale exit landing on a later visit -- but a lane with no network still
        has to open its gate, so failing to look it up is not an error.
        """
        from urllib.parse import quote

        try:
            return self._request("GET", f"/api/v1/lane/sessions/open?plate={quote(plate)}")
        except (PlatformUnreachable, PlatformRejected):
            return None

    def close_session(
        self,
        *,
        event_id: str,
        plate: str,
        exit_at: str,
        exit_confirmation: str,
        session_id: str | None = None,
    ) -> dict:
        body = {
            "event_id": event_id,
            "plate": plate,
            "exit_at": exit_at,
            "exit_confirmation": exit_confirmation,
        }
        if session_id:
            body["session_id"] = session_id
        return self._request("POST", "/api/v1/lane/sessions/close", body)


def _refusal_code(body: str) -> str | None:
    """The platform's own name for a refusal, out of its error body.

    `None` when the body is not JSON, is not an object, or carries no `code` --
    and a `code` that is not a string is `None` too, because a number or an
    object there is not a name and treating it as one is how a comparison
    against a string quietly stops matching anything.

    Never raises. This runs on the failure path of every platform call, and a
    parse error here would replace a refusal the lane knows how to survive with
    an exception it does not.
    """
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    code = parsed.get("code")
    return code if isinstance(code, str) and code else None
