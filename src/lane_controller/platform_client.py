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


class PlatformRejected(Exception):
    """A 4xx. The platform understood and refused; retrying will not help."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"platform rejected the request: HTTP {status}: {body}")
        self.status = status
        self.body = body


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
            raise PlatformRejected(err.code, body) from err
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise PlatformUnreachable(str(err)) from err

    # -- the lane surface --------------------------------------------------

    def get_rules(self) -> dict:
        return self._request("GET", "/api/v1/lane/rules")

    def post_events(self, events: list[dict]) -> dict:
        return self._request("POST", "/api/v1/lane/events", {"events": events})

    def open_session(self, *, plate: str, entry_at: str, plate_region: str | None = None) -> dict:
        return self._request(
            "POST",
            "/api/v1/lane/sessions/open",
            {"plate": plate, "entry_at": entry_at, "plate_region": plate_region},
        )

    def close_session(self, *, plate: str, exit_at: str) -> dict:
        return self._request(
            "POST", "/api/v1/lane/sessions/close", {"plate": plate, "exit_at": exit_at}
        )
