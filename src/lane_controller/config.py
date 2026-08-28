"""Lane configuration, read from TOML."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CameraConfig:
    camera_id: str
    rtsp_url: str
    frames_per_read: int = 3


@dataclass(frozen=True, slots=True)
class GateConfig:
    vend_pulse_ms: int = 500
    # There is no close setting. The barrier closes on its own closing loop.


@dataclass(frozen=True, slots=True)
class LoopConfig:
    """The lane's loop geometry: how many loops there are and how far apart.

    EVERY VALUE HERE IS A PER-SITE SETTING AND AN ASSUMPTION. Nothing in this
    package measures a spacing or a crossing time, and nothing here is a
    constant -- the defaults are the ones Gokhan named from having run parking
    operations, and they stay assumptions until a real site measures them. They
    are published with the events they govern, under the key
    `geometry_assumed`, so a reader of the record cannot mistake them for
    something this software established.

    Two arming loops BEFORE the barrier: both must read occupied together, so an
    object has to span the gap. Two closing loops AFTER it, crossed in order:
    A then B is a vehicle that went in, B then A is one backing out, and one
    loop cannot tell those apart.

    A site that has installed neither is not refused -- it runs exactly as it
    did, and what it does not get is named in the record on every vehicle
    (`arming_loops: 1`, and an `entry_unconfirmable` event) rather than being
    absent from it.
    """

    #: 1 or 2. With 2, one loop alone arms nothing and is recorded instead.
    arming_loops: int = 1
    #: Metres between the two arming loops. Assumed, never measured here.
    arming_spacing_m: float = 1.5
    #: 0 or 2. Never 1 -- see __post_init__.
    closing_loops: int = 0
    #: Metres between the two closing loops. Assumed, never measured here.
    closing_spacing_m: float = 1.5
    #: How long after a vend the closing sequence may take before the entry is
    #: HELD. Assumed, never measured here.
    confirmation_window_seconds: float = 10.0

    def __post_init__(self) -> None:
        if self.arming_loops not in (1, 2):
            raise ValueError(f"arming_loops must be 1 or 2, got {self.arming_loops!r}")
        if self.closing_loops not in (0, 2):
            # Not an oversight and not a convenience to be relaxed. ONE closing
            # loop cannot tell a vehicle going in from one backing out -- it
            # sees an occupancy either way -- so a site wired with one would be
            # told it had confirmation and would have a signal that says yes to
            # the exact case the confirmation exists to catch.
            raise ValueError(
                f"closing_loops must be 0 or 2, got {self.closing_loops!r}: one closing loop "
                "cannot separate a vehicle entering from one backing out, so it confirms nothing"
            )
        if self.confirmation_window_seconds < 0:
            raise ValueError(
                f"confirmation_window_seconds must not be negative, got "
                f"{self.confirmation_window_seconds!r}"
            )

    @property
    def confirms_entry(self) -> bool:
        """Whether this lane can confirm that a vehicle actually went through."""
        return self.closing_loops == 2

    def as_published(self) -> dict:
        """The geometry, for the event detail, under a name that says what it is."""
        return {
            "arming_loops": self.arming_loops,
            "arming_spacing_m": self.arming_spacing_m,
            "closing_loops": self.closing_loops,
            "closing_spacing_m": self.closing_spacing_m,
            "confirmation_window_seconds": self.confirmation_window_seconds,
        }


@dataclass(frozen=True, slots=True)
class LaneConfig:
    lane_id: str
    site_id: str
    camera: CameraConfig
    gate: GateConfig
    # The loop geometry. Defaults to the lane this package has always
    # described -- one arming loop, no closing loops -- so an existing
    # configuration file keeps working and says so in its records.
    loops: LoopConfig = field(default_factory=LoopConfig)
    # 'entry' opens a parking session; 'exit' closes one and the platform
    # computes the fee. A controller does one or the other, never both, because
    # one physical lane runs in one direction.
    direction: str = "entry"
    # Below this, the lane takes its fallback path rather than deciding. The
    # default is deliberately high: the cost of a fallback is an operator
    # glancing at a screen, and the cost of a wrong open is a stranger's car
    # billed to somebody else.
    confidence_threshold: float = 0.85
    rules_max_age_seconds: float = 86_400.0
    server_url: str | None = None

    def __post_init__(self) -> None:
        if self.direction not in ("entry", "exit"):
            raise ValueError(f"direction must be 'entry' or 'exit', got {self.direction!r}")

    @classmethod
    def from_file(cls, path: str | Path) -> LaneConfig:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> LaneConfig:
        lane = raw.get("lane", {})
        camera = raw.get("camera", {})
        gate = raw.get("gate", {})
        loops = raw.get("loops", {})
        return cls(
            lane_id=lane["id"],
            site_id=lane["site_id"],
            direction=lane.get("direction", "entry"),
            confidence_threshold=float(lane.get("confidence_threshold", 0.85)),
            rules_max_age_seconds=float(lane.get("rules_max_age_seconds", 86_400.0)),
            server_url=lane.get("server_url"),
            camera=CameraConfig(
                camera_id=camera.get("id", "cam-1"),
                rtsp_url=camera.get("rtsp_url", ""),
                frames_per_read=int(camera.get("frames_per_read", 3)),
            ),
            gate=GateConfig(vend_pulse_ms=int(gate.get("vend_pulse_ms", 500))),
            loops=LoopConfig(
                arming_loops=int(loops.get("arming_loops", 1)),
                arming_spacing_m=float(loops.get("arming_spacing_m", 1.5)),
                closing_loops=int(loops.get("closing_loops", 0)),
                closing_spacing_m=float(loops.get("closing_spacing_m", 1.5)),
                confirmation_window_seconds=float(loops.get("confirmation_window_seconds", 10.0)),
            ),
        )
