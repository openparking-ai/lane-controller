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
    package measures a spacing, and nothing here is a
    constant. They are published with the events they govern, under the key
    `geometry_assumed`, so a reader of the record cannot mistake them for
    something this software established.

    THE VALUES BELOW ARE NOT WHAT A CONFIGURATION FILE GETS. A file declares all
    five or it is refused -- see `_declared_loops`. These defaults exist for a
    `LaneConfig` built in code, and they describe the lane this package shipped
    before the loops existed.

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


#: The five keys a `[loops]` table must declare. There is no default for any of
#: them at the file boundary, and that is the whole point of the list.
LOOP_KEYS = (
    "arming_loops",
    "arming_spacing_m",
    "closing_loops",
    "closing_spacing_m",
    "confirmation_window_seconds",
)


def _declared_loops(raw: dict) -> dict:
    """The `[loops]` table, or a refusal naming the first key that is missing.

    A lane's loop geometry is DECLARED, never defaulted, and a configuration
    that does not say is refused before the lane runs rather than served a
    plausible answer -- the same shape as the engine refusing weights whose
    operating point nobody measured.

    It is not a style preference. A defaulted `closing_loops = 0` is
    indistinguishable, in the record and to an operator, from a site that
    installed the loops and mistyped the key: both write `unconfirmable` on
    every session, and only one of them is a decision. A site with no closing
    loops writes `closing_loops = 0` on the page and gets exactly what it had
    before; a site that meant to install them finds out here, not at 3am.

    A misspelt key is caught by the same rule, because a key spelt wrong is a
    key that is missing.
    """
    loops = raw.get("loops")
    if not isinstance(loops, dict):
        raise ValueError(
            "the configuration has no [loops] table. The loop geometry is declared, never "
            f"defaulted: give a [loops] table with all of {', '.join(LOOP_KEYS)} "
            "(config/lane.example.toml is the standard installation). A lane with no closing "
            "loops declares closing_loops = 0 and is not refused."
        )
    for key in LOOP_KEYS:
        if key not in loops:
            raise ValueError(
                f"[loops] does not declare {key}. Every one of {', '.join(LOOP_KEYS)} is "
                "required -- a value nobody wrote is not a value, and a key spelt wrong is a "
                "key that is missing. See config/lane.example.toml."
            )
    return loops


#: The published default for `LaneConfig.outbox_depth_threshold`: how many
#: undelivered events the outbox may hold before `outbox_depth_growing` reads
#: `active`.
#:
#: A PER-SITE SETTING AND AN ASSUMPTION. Nothing in this package measures how
#: deep a healthy lane's outbox gets, and this number is not a measurement of
#: one -- it is a line drawn well below the point at which the bounded log
#: starts discarding entries (`EventQueue`'s `max_events`, 10,000), so the code
#: fires while a human can still act on it rather than after events are already
#: lost. A site whose platform link is normally down for hours raises it; a
#: site that wants to hear about a five-minute outage lowers it.
DEFAULT_OUTBOX_DEPTH_THRESHOLD = 1_000

#: The published default for `LaneConfig.identity_health_timeout_s`: how long
#: `GET /v1/lane/health` may wait for the identification service's own health
#: route before answering `unknown` for `identity_service_degraded`.
#:
#: A PER-SITE SETTING AND AN ASSUMPTION. Nothing here measures how long a loaded
#: identification service takes to answer its health route. What this number IS
#: measured against is the other side of the seam: this route is polled by a
#: monitor, and a lane that blocks on a THIRD machine for as long as its own
#: consumers are willing to wait is published as a dead lane while it is up and
#: serving. So the bound belongs to the lane, it is short, and it is the lane's
#: own -- a hung identification service costs one field on this payload, not the
#: whole payload.
#:
#: On timeout the entry is `unknown`: nobody measured. NEVER `ok` and never
#: `active` -- a service that has not answered has not been found healthy and has
#: not been found degraded either, and `identity_service_down` is a different
#: code derived from a different signal.
DEFAULT_IDENTITY_HEALTH_TIMEOUT_S = 1.0


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
    # How many undelivered events the outbox may hold before the health surface
    # reports `outbox_depth_growing` as `active`. A per-site setting: see
    # DEFAULT_OUTBOX_DEPTH_THRESHOLD above for what it is and is not.
    outbox_depth_threshold: int = DEFAULT_OUTBOX_DEPTH_THRESHOLD
    # How long the health route may wait on the identification service's own
    # health route before answering `unknown`. A per-site setting: see
    # DEFAULT_IDENTITY_HEALTH_TIMEOUT_S above for what it is and is not.
    identity_health_timeout_s: float = DEFAULT_IDENTITY_HEALTH_TIMEOUT_S

    def __post_init__(self) -> None:
        if self.direction not in ("entry", "exit"):
            raise ValueError(f"direction must be 'entry' or 'exit', got {self.direction!r}")
        if not isinstance(self.outbox_depth_threshold, int) or self.outbox_depth_threshold < 1:
            # Zero would make every lane report the fault permanently, which is
            # the same thing as reporting nothing.
            raise ValueError(
                f"outbox_depth_threshold must be a positive integer, "
                f"got {self.outbox_depth_threshold!r}"
            )
        if (
            isinstance(self.identity_health_timeout_s, bool)
            or not isinstance(self.identity_health_timeout_s, (int, float))
            or self.identity_health_timeout_s <= 0
        ):
            # Zero or negative would mean the route never waits at all, which is
            # a lane that answers `unknown` for that code for ever while looking
            # like it measured something.
            raise ValueError(
                f"identity_health_timeout_s must be a positive number of seconds, "
                f"got {self.identity_health_timeout_s!r}"
            )

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
        loops = _declared_loops(raw)
        return cls(
            lane_id=lane["id"],
            site_id=lane["site_id"],
            direction=lane.get("direction", "entry"),
            confidence_threshold=float(lane.get("confidence_threshold", 0.85)),
            rules_max_age_seconds=float(lane.get("rules_max_age_seconds", 86_400.0)),
            server_url=lane.get("server_url"),
            outbox_depth_threshold=int(
                lane.get("outbox_depth_threshold", DEFAULT_OUTBOX_DEPTH_THRESHOLD)
            ),
            identity_health_timeout_s=float(
                lane.get("identity_health_timeout_s", DEFAULT_IDENTITY_HEALTH_TIMEOUT_S)
            ),
            camera=CameraConfig(
                camera_id=camera.get("id", "cam-1"),
                rtsp_url=camera.get("rtsp_url", ""),
                frames_per_read=int(camera.get("frames_per_read", 3)),
            ),
            gate=GateConfig(vend_pulse_ms=int(gate.get("vend_pulse_ms", 500))),
            loops=LoopConfig(
                arming_loops=int(loops["arming_loops"]),
                arming_spacing_m=float(loops["arming_spacing_m"]),
                closing_loops=int(loops["closing_loops"]),
                closing_spacing_m=float(loops["closing_spacing_m"]),
                confirmation_window_seconds=float(loops["confirmation_window_seconds"]),
            ),
        )
