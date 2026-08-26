"""The three hardware seams.

Everything above these protocols is testable on any machine. Everything below
them is a driver for one specific piece of equipment, and there is no third
category -- if lane logic starts importing a vendor SDK, the seam has leaked.

Reference hardware, none of which is required to run this package:

  * controller  Seeed reComputer Industrial J3011 (Jetson Orin NX)
  * barrier     Q-SAQ, driven by a dry-contact vend relay
  * detection   inductive arming loop, plus the barrier's own closing loop
  * cameras     Reolink RLC-810A (default tier), Axis P1465-LE or
                Hanwha XNO-9082R (upper tiers). RTSP and PoE, always.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Frame:
    """One captured image plus enough provenance to argue about it later."""

    image_bytes: bytes
    captured_at: float
    camera_id: str
    width: int = 0
    height: int = 0


class LoopInput(Protocol):
    """The arming loop: an inductive loop that reports a vehicle presenting.

    This is the *arming* loop only. The barrier's closing loop is wired to the
    barrier and is deliberately not represented here -- see VendOutput.
    """

    def wait_for_vehicle(self, timeout: float | None = None) -> bool:
        """Block until a vehicle arms the loop. True if one did, False on timeout."""
        ...

    def is_occupied(self) -> bool:
        """Whether the loop is currently reading a vehicle over it."""
        ...


class CameraFeed(Protocol):
    """A camera the controller can pull still frames from, on demand.

    Pull, not subscribe: the sequence is triggered by the loop, so the
    controller asks for frames at the moment a vehicle is actually there.
    """

    @property
    def camera_id(self) -> str: ...

    def grab(self, count: int = 1) -> Sequence[Frame]:
        """Capture `count` frames now. May return fewer; never more."""
        ...


class VendOutput(Protocol):
    """The vend relay: a momentary dry contact that asks the barrier to open.

    There is no close() and there will not be one. The barrier closes itself on
    its own closing loop, which is wired to the barrier and never to us. A
    controller that could close a barrier is a controller that can close one on
    a vehicle -- the safety case depends on that not being possible, not on us
    being careful.
    """

    def vend(self, reason: str) -> None:
        """Pulse the relay to open the barrier."""
        ...


@dataclass(frozen=True, slots=True)
class VehicleIdentity:
    """What the vision stage believes it saw, and how strongly.

    `confidence` is the whole point of this type. A caller that reads `plate`
    without reading `confidence` has turned a maybe into a fact.
    """

    plate: str | None
    plate_region: str | None = None
    make: str | None = None
    model: str | None = None
    color: str | None = None
    marks: tuple[str, ...] = field(default_factory=tuple)
    confidence: float = 0.0


class VehicleIdentifier(Protocol):
    """Turns frames into a VehicleIdentity.

    The real implementation runs on the Jetson. The stub in `simulated` returns
    whatever it was configured to return, which is what lets the entire lane
    sequence run and be tested with no hardware and no model.
    """

    def identify(self, frames: Sequence[Frame]) -> VehicleIdentity: ...
