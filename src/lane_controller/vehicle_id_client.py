"""The lane's client for the Vehicle ID system.

Vehicle ID is a separate system with its own contract, and this lane is an
ORDINARY CLIENT of it -- the same door a third party uses. There is no
in-process path reserved for us, and there is deliberately no import of the
engine anywhere in this package: only `vehicle_id.contract`, which is the
public record shape and nothing else. `tests/test_vehicle_id_boundary.py`
enforces that, and fails if it is ever untrue.

Local, not remote. The default address is loopback, because identification runs
on the same device or the same LAN and the lane has to work with the internet
down. A hostname pointing at anything else is a deployment mistake, not a
feature.

The translation this class performs is small and deliberate:

  * The engine has already applied its MEASURED operating threshold and said
    `answer` or `fallback`. A read the engine sent to fallback arrives here with
    its confidence forced to 0.0, so the lane's own threshold cannot
    accidentally accept what the engine already refused to stand behind. The
    plate is dropped with it -- an identity the engine would not vouch for must
    not be available to match a rule.
  * Anything that goes wrong -- the service down, a timeout, a malformed body,
    a schema this build does not understand -- becomes a zero-confidence
    identity, not an exception. A car is at the barrier; the lane needs an
    outcome it can act on, and its fallback path is that outcome.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from base64 import b64encode
from collections.abc import Sequence

from vehicle_id.contract import Read

from .interfaces import Frame, VehicleIdentity

log = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "http://127.0.0.1:8088"
DEFAULT_TIMEOUT = 2.0

#: What the lane hands `decide()` when it has no usable identification. Named
#: once so that every failure path below returns the same thing, rather than
#: three subtly different empties.
NO_IDENTITY = VehicleIdentity(plate=None, confidence=0.0)


class VehicleIdClient:
    """Implements the lane's `VehicleIdentifier` over the Vehicle ID contract."""

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout: float = DEFAULT_TIMEOUT,
        opener=None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self._open = opener or _post_json

    def identify(self, frames: Sequence[Frame]) -> VehicleIdentity:
        if not frames:
            return NO_IDENTITY

        payload = {
            "camera_id": frames[0].camera_id,
            "captures": [
                {
                    "image_b64": b64encode(frame.image_bytes).decode(),
                    "camera_id": frame.camera_id,
                }
                for frame in frames
            ],
        }

        try:
            body = self._open(f"{self.endpoint}/v1/reads", payload, self.timeout)
            read = Read.from_dict(body["read"])
        except Exception as exc:
            # Includes an unrecognised schema_version. Refusing to guess which
            # fields still mean what they used to is the contract's rule, and
            # for a lane the consequence of refusing is a fallback, not a crash.
            log.warning("vehicle-id unavailable or unusable (%s); falling back", exc)
            return NO_IDENTITY

        if not read.is_answer:
            # The engine measured a confidence and declined to stand behind it.
            # Passing that number on would let the lane's own threshold second-
            # guess a decision the engine already made against measured data.
            return NO_IDENTITY

        identity = read.identity
        return VehicleIdentity(
            plate=identity.plate,
            plate_region=identity.plate_region,
            make=identity.make,
            model=identity.model,
            color=identity.color,
            marks=tuple(identity.marks),
            confidence=read.confidence,
        )

    def operating_threshold(self) -> float | None:
        """The engine's measured operating point, read from the service.

        Offered so a lane can align its own `confidence_threshold` with the
        engine actually deployed instead of a number copied into a config file
        months ago. None when the service cannot be reached.
        """
        try:
            body = self._open_health(f"{self.endpoint}/v1/health")
        except Exception as exc:
            log.warning("could not read the vehicle-id operating point: %s", exc)
            return None
        return body.get("threshold_applied")

    def _open_health(self, url: str) -> dict:
        with urllib.request.urlopen(url, timeout=self.timeout) as response:
            return json.loads(response.read())


def _post_json(url: str, payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())
