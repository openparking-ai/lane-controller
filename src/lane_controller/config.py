"""Lane configuration, read from TOML."""

from __future__ import annotations

import math
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
    seven or it is refused -- see `_declared_loops`. These defaults exist for a
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

    A third kind of loop, BEFORE the arming loops: the DEACTIVATE loop. While it
    reads occupied the lane does not arm -- a second vehicle that close behind
    the one at the barrier is a vehicle that would follow it through an open
    boom, so the boom stays down until it backs off. Zero or one of these; a
    site with none runs exactly as it did, and its record says
    `deactivate_loops: 0` on every vehicle rather than saying nothing.
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
    #: 0 or 1. With 1, an occupied deactivate loop holds the arming cycle.
    deactivate_loops: int = 0
    #: Metres from the deactivate loop to the NEAREST arming loop. Assumed,
    #: never measured here -- and BOUNDED BELOW, see __post_init__: a loop
    #: closer than the longest vehicle the lane admits is under that vehicle's
    #: own tail while its nose is on the arming loop, and it holds itself.
    deactivate_spacing_m: float = 0.0

    def __post_init__(self) -> None:
        if self.arming_loops not in (1, 2):
            raise ValueError(f"arming_loops must be 1 or 2, got {self.arming_loops!r}")
        if self.deactivate_loops not in (0, 1):
            raise ValueError(f"deactivate_loops must be 0 or 1, got {self.deactivate_loops!r}")
        if self.deactivate_loops == 1:
            # THE LOWER BOUND, and why it is a refusal and not a warning: a
            # deactivate loop nearer the arming loop than a vehicle is long
            # reads occupied under the tail of the very car whose nose is on
            # the arming loop. That car suppresses its own arming cycle, for
            # as long as it sits there, and the lane has built a barrier that
            # holds every long vehicle for ever. The number is an assumption
            # about vehicles, stated once as `DEACTIVATE_SPACING_MIN_M`, and a
            # site whose lane admits longer ones declares a larger spacing.
            if not (
                isinstance(self.deactivate_spacing_m, (int, float))
                and math.isfinite(self.deactivate_spacing_m)
                and self.deactivate_spacing_m >= DEACTIVATE_SPACING_MIN_M
            ):
                raise ValueError(
                    f"deactivate_spacing_m must be a finite number of at least "
                    f"{DEACTIVATE_SPACING_MIN_M} m, got {self.deactivate_spacing_m!r}: a "
                    "deactivate loop closer than the longest vehicle sits under that "
                    "vehicle's own tail and holds it at the barrier for ever"
                )
        elif self.deactivate_spacing_m != 0.0:
            # No loop, no spacing. A number here would be published under
            # `geometry_assumed` as the distance to a loop that does not exist.
            raise ValueError(
                f"deactivate_spacing_m must be 0.0 when deactivate_loops is 0, got "
                f"{self.deactivate_spacing_m!r}: there is no loop for it to be the spacing of"
            )
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

    @property
    def has_deactivate_loop(self) -> bool:
        """Whether this lane holds its arming cycle for a vehicle too close behind."""
        return self.deactivate_loops == 1

    def as_published(self) -> dict:
        """The geometry, for the event detail, under a name that says what it is."""
        return {
            "arming_loops": self.arming_loops,
            "arming_spacing_m": self.arming_spacing_m,
            "closing_loops": self.closing_loops,
            "closing_spacing_m": self.closing_spacing_m,
            "confirmation_window_seconds": self.confirmation_window_seconds,
            "deactivate_loops": self.deactivate_loops,
            "deactivate_spacing_m": self.deactivate_spacing_m,
        }


#: The least a deactivate loop may sit from the nearest arming loop, in metres.
#:
#: A PER-PACKAGE ASSUMPTION ABOUT VEHICLES, NOT A MEASUREMENT: nothing here has
#: measured a vehicle, and 6 metres is not the length of one. It is drawn
#: against what the loop MEANS -- "the vehicle behind is too close" -- which is
#: only true of a loop that a single vehicle cannot cover together with the
#: arming loop. A long car or a van is under six metres; a lane that admits
#: anything longer declares a larger spacing, and the record says which value
#: was in force.
DEACTIVATE_SPACING_MIN_M = 6.0

#: The seven keys a `[loops]` table must declare. There is no default for any
#: of them at the file boundary, and that is the whole point of the list.
LOOP_KEYS = (
    "arming_loops",
    "arming_spacing_m",
    "closing_loops",
    "closing_spacing_m",
    "confirmation_window_seconds",
    "deactivate_loops",
    "deactivate_spacing_m",
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

#: The published default for `LaneConfig.completion_max_age_s`: how old the
#: decision named by `POST /v1/lane/vend` may be before the completion is
#: refused `decision_stale`.
#:
#: A PER-SITE SETTING AND AN ASSUMPTION. Nothing in this package measures how
#: long an intercom conversation takes, and 120 seconds is not a measurement of
#: one. What it IS drawn against is what a completion MEANS: an answer to a
#: driver who is at the barrier now. A completion accepted against a decision
#: from ten minutes ago would open a barrier for whoever happens to be there,
#: which is the fraud the whole round is built not to enable -- and a bound so
#: tight that an ordinary call cannot finish inside it sends every real customer
#: to a human instead.
#:
#: It is the same number the agent will use for the case it speaks, and it is a
#: setting on both sides rather than a constant on either, so a site whose
#: intercom queue is long raises it in one place and the two stay drawn against
#: the same thing.
DEFAULT_COMPLETION_MAX_AGE_S = 120.0

#: The published default for `LaneConfig.settle_grace_s`: how long PAST the
#: confirmation window the assisted vend waits for its loop driver to return
#: before it stops waiting and says so.
#:
#: A PER-SITE SETTING AND AN ASSUMPTION. Nothing in this package measures how
#: late a loop board answers. What the number IS drawn against is the difference
#: between LATE and NEVER: `resolve_transit` already handles a crossing reported
#: after the window -- it reads the clock either side of the call and refuses to
#: confirm one that took too long -- and the case it cannot handle is a driver
#: that does not return at all. That is a fault, not a slow answer, so the grace
#: is short: long enough that an ordinary late return is judged by the window
#: rule that exists for it, short enough that a lane is not left refusing every
#: completion `busy` while somebody stands at an intercom.
#:
#: The bound the settle runs under is `confirmation_window_seconds` PLUS this.
DEFAULT_SETTLE_GRACE_S = 5.0

#: The published default for `LaneConfig.arming_loop_max_occupied_s`: how long
#: an arming loop may read occupied across this lane's own observations before
#: `arming_loop_stuck_occupied` reads `active`.
#:
#: A PER-SITE SETTING AND AN ASSUMPTION, and the assumption is stated because
#: this number decides whether a barrier opens: NOTHING HAS MEASURED A DWELL.
#: No site exists, no arrival has been timed, and 600 seconds is not a
#: measurement of how long a real vehicle waits at a real barrier. It is drawn
#: against what the code MEANS: a car that has been sitting on the arming loop
#: for ten minutes is not a car waiting to be let in -- it is a broken-down
#: vehicle, a delivery van somebody parked on the loop, or a loop that is
#: reading occupied with nothing on it. All three want a human, and the third is
#: the one that turns this lane into a barrier that opens for nobody in
#: particular.
#:
#: A site with a genuinely slow entry raises it, in one place, and the record
#: says which value was in force.
DEFAULT_ARMING_LOOP_MAX_OCCUPIED_S = 600.0


#: The published default for `LaneConfig.rules_refresh_seconds`: how often the
#: production loop re-reads `GET /lane/rules` -- the garage's plans, its space
#: class, each entitlement module's register and the full set of open stays --
#: the SLOW cadence.
#:
#: A PER-SITE SETTING AND AN ASSUMPTION. Nothing in this package measures how
#: often a pass is cancelled or a plan changes, and five minutes is not a
#: measurement of either. What it IS drawn against is the trade the brief
#: states: a pass cancelled after the last refresh gets one free exit before the
#: cache catches up, against every legitimate holder waiting on the network at
#: every exit. It must be shorter than `rules_max_age_seconds`, or the cache is
#: stale by construction between two refreshes.
DEFAULT_RULES_REFRESH_S = 300.0

#: The published default for `LaneConfig.stays_refresh_seconds`: how often the
#: production loop reads `GET /lane/stays?since=<cursor>` -- every stay opened
#: or closed at the garage since the last read -- the FAST cadence.
#:
#: A PER-SITE SETTING AND AN ASSUMPTION, and this number is worth reading
#: twice: A CAR THAT ENTERED INSIDE ONE OF THESE INTERVALS CANNOT BE PRICED AT
#: THE BARRIER, because its entry time has not reached the exit lane's cache.
#: It leaves the way it always has -- the barrier opens, the platform settles
#: the stay at the close (covered if a module it asks there covers it, priced
#: through the engine if none does), nothing is collected at the reader -- and
#: the record says so. This interval IS the size of that class in ordinary operation, so
#: it is short, and a site that measures its own entry-to-exit minimum sets it
#: from that. Nothing here has measured one. It cannot be longer than
#: `rules_refresh_seconds`: the slow cadence carries the full set, and a delta
#: read less often than its own resync is no delta.
DEFAULT_STAYS_REFRESH_S = 5.0


def positive_finite(value, name: str) -> None:
    """Refuse anything that is not a real, positive number of seconds.

    THE ONE PLACE, and it is one place because it was two. Every setting below
    used to carry its own `<= 0` test, and `nan <= 0` is `False` and `inf <= 0`
    is `False` -- so `completion_max_age_s = nan` was a well-formed TOML file
    (1.0 has `nan` and `inf` as float literals) that passed the validator and
    made `decision_stale` unreachable, because `x > nan` is `False` for every
    `x`. The check tested one side of zero and nothing else, and it guarded the
    direction that refuses customers while leaving open the direction that opens
    barriers.

    `bool` is refused explicitly: `True` is an `int` in Python, and a lane
    configured with `true` seconds is a typo answering as 1.
    """
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(
            f"{name} must be a positive, finite number of seconds, got {value!r}: "
            "nan and inf are TOML float literals and neither of them is a duration"
        )


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
    # The two refresh cadences of the production loop. Per-site settings: see
    # DEFAULT_RULES_REFRESH_S and DEFAULT_STAYS_REFRESH_S above for what each is
    # and is not -- the second one is the size of the class of cars that cannot
    # be priced at the barrier.
    rules_refresh_seconds: float = DEFAULT_RULES_REFRESH_S
    stays_refresh_seconds: float = DEFAULT_STAYS_REFRESH_S
    # Where the identification service answers. None: this lane identifies
    # nothing -- `serve` wires the stub identifier and says so. Set: the lane
    # is an ordinary client of Vehicle ID at that address, and the address is
    # loopback in any real installation (`vehicle_id_client`).
    vehicle_id_url: str | None = None
    # Where the decision cache lives on disk, so a lane that restarts with the
    # platform unreachable decides from what it last held. None: memory only,
    # gone on restart -- the standalone product and every test. Set: an
    # ABSOLUTE path in a directory this process owns and nobody else can write
    # (`durable.py` says why that is refused, not warned). What the file holds
    # is personal data, bounded by `rules_max_age_seconds`.
    cache_path: str | None = None
    # How many undelivered events the outbox may hold before the health surface
    # reports `outbox_depth_growing` as `active`. A per-site setting: see
    # DEFAULT_OUTBOX_DEPTH_THRESHOLD above for what it is and is not.
    outbox_depth_threshold: int = DEFAULT_OUTBOX_DEPTH_THRESHOLD
    # How long the health route may wait on the identification service's own
    # health route before answering `unknown`. A per-site setting: see
    # DEFAULT_IDENTITY_HEALTH_TIMEOUT_S above for what it is and is not.
    identity_health_timeout_s: float = DEFAULT_IDENTITY_HEALTH_TIMEOUT_S
    # How old the decision a `POST /v1/lane/vend` completes may be. A per-site
    # setting: see DEFAULT_COMPLETION_MAX_AGE_S above for what it is and is not.
    completion_max_age_s: float = DEFAULT_COMPLETION_MAX_AGE_S
    # How long past the confirmation window the assisted vend waits for its loop
    # driver. A per-site setting: see DEFAULT_SETTLE_GRACE_S above.
    settle_grace_s: float = DEFAULT_SETTLE_GRACE_S
    # How long an arming loop may read occupied before `arming_loop_stuck_occupied`
    # is `active`. A per-site setting: see DEFAULT_ARMING_LOOP_MAX_OCCUPIED_S above.
    arming_loop_max_occupied_s: float = DEFAULT_ARMING_LOOP_MAX_OCCUPIED_S

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
        # Every duration through the SAME validator. Zero or negative would
        # mean: a health route that never waits and answers `unknown` for ever
        # while looking like it measured something; a lane whose act surface
        # refuses every completion as stale while publishing `can_vend: true`;
        # a settle that gives up before it has waited; a lane that calls its own
        # arming loop stuck the moment a car reaches it. Not finite would mean
        # none of those refusals can ever fire.
        for name in (
            "identity_health_timeout_s",
            "completion_max_age_s",
            "settle_grace_s",
            "arming_loop_max_occupied_s",
            "rules_refresh_seconds",
            "stays_refresh_seconds",
        ):
            positive_finite(getattr(self, name), name)
        # The two cadences are ordered, and both sit inside the staleness
        # bound. A delta read less often than the full set it is a delta OF is
        # no delta; a full refresh slower than the age at which the cache stops
        # being trusted makes every lane stale between two refreshes.
        if self.stays_refresh_seconds > self.rules_refresh_seconds:
            raise ValueError(
                f"stays_refresh_seconds ({self.stays_refresh_seconds!r}) must not exceed "
                f"rules_refresh_seconds ({self.rules_refresh_seconds!r}): the fast cadence "
                "carries the delta of what the slow one carries whole"
            )
        if self.rules_refresh_seconds >= self.rules_max_age_seconds:
            raise ValueError(
                f"rules_refresh_seconds ({self.rules_refresh_seconds!r}) must be less than "
                f"rules_max_age_seconds ({self.rules_max_age_seconds!r}): a cache refreshed "
                "slower than it goes stale is stale between every two refreshes"
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
            rules_refresh_seconds=float(
                lane.get("rules_refresh_seconds", DEFAULT_RULES_REFRESH_S)
            ),
            stays_refresh_seconds=float(
                lane.get("stays_refresh_seconds", DEFAULT_STAYS_REFRESH_S)
            ),
            vehicle_id_url=lane.get("vehicle_id_url"),
            cache_path=lane.get("cache_path"),
            outbox_depth_threshold=int(
                lane.get("outbox_depth_threshold", DEFAULT_OUTBOX_DEPTH_THRESHOLD)
            ),
            identity_health_timeout_s=float(
                lane.get("identity_health_timeout_s", DEFAULT_IDENTITY_HEALTH_TIMEOUT_S)
            ),
            completion_max_age_s=float(
                lane.get("completion_max_age_s", DEFAULT_COMPLETION_MAX_AGE_S)
            ),
            settle_grace_s=float(lane.get("settle_grace_s", DEFAULT_SETTLE_GRACE_S)),
            arming_loop_max_occupied_s=float(
                lane.get("arming_loop_max_occupied_s", DEFAULT_ARMING_LOOP_MAX_OCCUPIED_S)
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
                deactivate_loops=int(loops["deactivate_loops"]),
                deactivate_spacing_m=float(loops["deactivate_spacing_m"]),
            ),
        )
