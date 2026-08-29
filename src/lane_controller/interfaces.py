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
from enum import StrEnum
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
    """One arming loop: an inductive loop that reports a vehicle presenting.

    A lane may have two of these, ~1.5 m apart, and arms only when both read
    occupied together -- an object then has to SPAN the gap, which a person
    standing on one loop with a piece of metal cannot. How many a site has is
    `LoopConfig.arming_loops`; a site with one is not refused and says so in
    every record it writes.

    This is the *arming* stage only. Two other loops exist and neither is this:
    the barrier's own safety closing loop, which is wired to the barrier and is
    deliberately not represented anywhere in this package (see VendOutput); and
    the pair of CONFIRMATION loops after the gate, which this package reads but
    does not drive (see ClosingLoops).
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


class ClosingSequence(StrEnum):
    """What the two loops after the gate saw, in order.

    The direction is the whole point, and it is why there are two. Every camera
    measurement PREDICTS -- is there a vehicle there now -- and a prediction can
    be quietly wrong. Two loops crossed in order CONFIRM that a vehicle-length
    object travelled forward through the gate. One loop reads an occupancy and
    cannot tell a vehicle going in from one backing out.
    """

    #: A then B. A vehicle went through the gate.
    FORWARD = "forward"
    #: B then A. Somebody backed out. No session, no occupancy.
    REVERSE = "reverse"
    #: The window elapsed with no sequence at all. NOT a confirmation and NOT a
    #: refutation -- a third state, and it is held rather than folded into
    #: either of the other two.
    NONE = "none"


class ClosingLoops(Protocol):
    """The pair of confirmation loops AFTER the barrier, read and never driven.

    Not to be confused with the barrier's own closing loop, which lowers the
    boom and is wired to the barrier. This package still cannot close a barrier
    and there is still no close() anywhere in it -- the safety case depends on
    that remaining impossible. These two loops are an INPUT: they report that
    something crossed them, in an order, and the controller reads that report.
    """

    def wait_for_sequence(self, window_seconds: float) -> ClosingSequence:
        """Block up to `window_seconds` for a crossing, and say which way it went.

        Returns NONE if the window elapses with no complete sequence. An
        implementation must not report FORWARD for a crossing that took longer
        than the window -- the window is what makes the confirmation mean "a
        vehicle went through in a plausible time" rather than "something
        happened here eventually".
        """
        ...


@dataclass(frozen=True, slots=True)
class VehicleIdentity:
    """What the vision stage believes it saw, and how strongly.

    `confidence` is the whole point of this type. A caller that reads `plate`
    without reading `confidence` has turned a maybe into a fact.

    `presence` is the second point, and it is deliberately not folded into
    `confidence`. "Nothing was there" and "something was there and I could not
    read it" are different events with opposite correct responses: the first
    must produce no transaction at all, the second must produce a ticket and a
    human.

    `unavailable` is the third, and it is not folded into `confidence` either.
    A read that was never obtained is not a read the engine was unsure about,
    and the two used to arrive at the decision stage as the same number.
    """

    plate: str | None
    plate_region: str | None = None
    make: str | None = None
    model: str | None = None
    color: str | None = None
    marks: tuple[str, ...] = field(default_factory=tuple)
    confidence: float = 0.0
    #: Whether a VEHICLE was there at all, which is a different question from
    #: whether it could be identified. `None` means nobody measured it, and a
    #: lane that has not configured a reference view of its empty tarmac must
    #: behave exactly as it did before this field existed -- NOT refuse
    #: everybody.
    presence: bool | None = None
    #: Why the lane has NO READ AT ALL, or None when it has one. This is the
    #: third question, and it is as separate from the other two as they are
    #: from each other: "nothing was there", "something was there and I could
    #: not read it", and "nobody asked, or nobody answered".
    #:
    #: A `confidence` of 0.0 is what a failed request leaves behind, and it is
    #: not a measurement -- the engine did not look at a plate and find it
    #: marginal, the engine was not reached. Reading the two as one code tells
    #: a driver at the barrier to wipe a plate while the identification service
    #: is switched off. The string names WHICH failure; the constants are in
    #: `vehicle_id_client`, and this is the only place that detail survives
    #: once the exception has been logged and swallowed.
    unavailable: str | None = None


class VehicleIdentifier(Protocol):
    """Turns frames into a VehicleIdentity.

    The real implementation runs on the Jetson. The stub in `simulated` returns
    whatever it was configured to return, which is what lets the entire lane
    sequence run and be tested with no hardware and no model.
    """

    def identify(self, frames: Sequence[Frame]) -> VehicleIdentity: ...
