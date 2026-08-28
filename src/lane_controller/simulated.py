"""Simulated implementations of every hardware seam.

These are not toys kept alongside the real thing -- they are how the lane
sequence is developed and tested. No loop, no camera, no barrier, no Jetson and
no vision model is needed to run the whole sequence end to end.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence

from .interfaces import (
    CameraFeed,
    ClosingLoops,
    ClosingSequence,
    Frame,
    LoopInput,
    VehicleIdentifier,
    VehicleIdentity,
)


class SimulatedLoopInput(LoopInput):
    """An arming loop driven by a scripted list of arrivals."""

    def __init__(self, arrivals: int = 1) -> None:
        self._remaining = arrivals
        self._occupied = False

    def wait_for_vehicle(self, timeout: float | None = None) -> bool:
        if self._remaining <= 0:
            self._occupied = False
            return False
        self._remaining -= 1
        self._occupied = True
        return True

    def is_occupied(self) -> bool:
        return self._occupied

    def clear(self) -> None:
        """Vehicle has moved off the loop."""
        self._occupied = False


class CannedCameraFeed(CameraFeed):
    """Serves fixed frames in order, repeating the last one once exhausted."""

    def __init__(self, camera_id: str = "sim-cam-1", images: Sequence[bytes] | None = None) -> None:
        self._camera_id = camera_id
        self._images = list(images) if images else [b"\x89PNG\r\n\x1a\n<simulated frame>"]
        self._cursor = 0

    @property
    def camera_id(self) -> str:
        return self._camera_id

    def grab(self, count: int = 1) -> Sequence[Frame]:
        frames = []
        for _ in range(count):
            image = self._images[min(self._cursor, len(self._images) - 1)]
            self._cursor += 1
            frames.append(
                Frame(
                    image_bytes=image,
                    captured_at=time.time(),
                    camera_id=self._camera_id,
                    width=3840,
                    height=2160,
                )
            )
        return frames


class RecordingVendOutput:
    """A vend relay that records instead of switching.

    Note what is absent: no close. The barrier closes on its own closing loop.
    """

    def __init__(self) -> None:
        self.vends: list[tuple[float, str]] = []

    def vend(self, reason: str) -> None:
        self.vends.append((time.time(), reason))

    @property
    def vend_count(self) -> int:
        return len(self.vends)


class StubVehicleIdentifier(VehicleIdentifier):
    """Returns pre-set identities, in order.

    This is where confidence comes from in tests. Give it a 0.42 and the lane
    must take its fallback path; give it a 0.97 and the lane must decide. That
    is the behaviour worth testing, and it needs no model to test.
    """

    def __init__(self, identities: Sequence[VehicleIdentity] | None = None) -> None:
        self._identities: list[VehicleIdentity] = (
            list(identities)
            if identities
            else [
                VehicleIdentity(
                    plate="SIM-0001",
                    plate_region="FL",
                    make="Toyota",
                    model="Corolla",
                    color="silver",
                    marks=("roof rack",),
                    confidence=0.97,
                )
            ]
        )
        self._iter: Iterator[VehicleIdentity] = iter(self._identities)
        self._last = self._identities[-1]

    def identify(self, frames: Sequence[Frame]) -> VehicleIdentity:
        if not frames:
            return VehicleIdentity(plate=None, confidence=0.0)
        try:
            self._last = next(self._iter)
        except StopIteration:
            pass
        return self._last


class OccupancyLoopInput(LoopInput):
    """A loop whose occupancy is set directly, for the SECOND arming loop.

    The second loop is never the one that wakes the lane -- it is the one asked
    whether it agrees -- so what a test needs from it is a settable occupancy,
    not a scripted arrival.
    """

    def __init__(self, occupied: bool = True) -> None:
        self._occupied = occupied

    def wait_for_vehicle(self, timeout: float | None = None) -> bool:
        return self._occupied

    def is_occupied(self) -> bool:
        return self._occupied

    def set_occupied(self, occupied: bool) -> None:
        self._occupied = occupied


class ScriptedClosingLoops(ClosingLoops):
    """The two loops after the gate, driven by a script of crossings.

    Each entry is a direction AND how long the crossing took. The time is not
    decoration: the window is a real branch in the lane's behaviour, so a
    fixture that could not put a crossing on both sides of it would pin the one
    axis the answer depends on, and would read as coverage while measuring one
    point.

    A crossing that takes longer than the window is not reported as a crossing.
    Real loops would still be waiting when the window expired, and the whole
    meaning of the window is "a vehicle went through in a plausible time".
    """

    def __init__(self, crossings: Sequence[tuple[ClosingSequence, float]] | None = None) -> None:
        self._crossings: list[tuple[ClosingSequence, float]] = list(crossings or [])
        self._index = 0
        #: What was asked of it, so a test can assert the configured window was
        #: the one applied rather than a number the fixture chose for itself.
        self.windows_seen: list[float] = []

    def wait_for_sequence(self, window_seconds: float) -> ClosingSequence:
        self.windows_seen.append(window_seconds)
        if self._index >= len(self._crossings):
            # Nothing scripted means nothing crossed: the window elapses.
            return ClosingSequence.NONE
        sequence, took_seconds = self._crossings[self._index]
        self._index += 1
        if took_seconds > window_seconds:
            return ClosingSequence.NONE
        return sequence
