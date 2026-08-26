"""The lane sequence.

    arming loop  ->  grab frames  ->  identify  ->  decide  ->  vend

and then nothing. The barrier closes itself on its own closing loop; the
controller has no part in it and no way to interfere with it.

The whole sequence runs against simulated implementations of all three seams,
which is why `tests/` needs no hardware.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from .config import LaneConfig
from .decision import Decision, DecisionCache, Outcome, decide
from .events import EventQueue
from .interfaces import CameraFeed, LoopInput, VehicleIdentifier, VendOutput
from .sync import SESSION_CLOSE, SESSION_OPEN, to_iso

log = logging.getLogger(__name__)


class LaneController:
    def __init__(
        self,
        config: LaneConfig,
        *,
        loop: LoopInput,
        camera: CameraFeed,
        vend: VendOutput,
        identifier: VehicleIdentifier,
        cache: DecisionCache | None = None,
        events: EventQueue | None = None,
        clock: Callable[[], float] = time.time,
        session_lookup: Callable[[str], dict | None] | None = None,
    ) -> None:
        self.config = config
        self.loop = loop
        self.camera = camera
        self.vend = vend
        self.identifier = identifier
        # `cache or DecisionCache(...)` would be wrong, and was: DecisionCache
        # defines __len__, so a freshly synced cache that happens to hold zero
        # plate rules is falsy and would be silently thrown away and replaced
        # with an empty, never-refreshed one -- which then reports itself STALE
        # and sends every vehicle to fallback. A transient garage syncs exactly
        # zero plate rules, so this is the normal case, not an edge case.
        self.cache = (
            cache
            if cache is not None
            else DecisionCache(max_age_seconds=config.rules_max_age_seconds)
        )
        self.events = events if events is not None else EventQueue()
        # Injectable so a demo or a test can put a car through a three-hour
        # stay without waiting three hours. The lane's own clock is what stamps
        # session times, which is the point: the platform must price the stay
        # from when the car was there, not from when it heard about it.
        self._clock = clock
        # Injected rather than reached for, so a lane can be built with no
        # platform at all and the tests need no network.
        self.session_lookup = session_lookup

    def handle_arrival(self) -> Decision:
        """One vehicle, from arming to vend. Assumes the loop has already armed."""
        lane = self.config.lane_id

        frames = self.camera.grab(self.config.camera.frames_per_read)
        self.events.record("frames_captured", lane, count=len(frames), camera=self.camera.camera_id)

        identity = self.identifier.identify(frames)
        self.events.record(
            "vehicle_identified",
            lane,
            plate=identity.plate,
            confidence=identity.confidence,
            make=identity.make,
            model=identity.model,
            color=identity.color,
        )

        decision = decide(
            identity,
            self.cache,
            confidence_threshold=self.config.confidence_threshold,
        )
        self.events.record(
            "decision",
            lane,
            outcome=decision.outcome.value,
            reason=decision.reason,
            fallback=decision.fallback.value if decision.fallback else None,
            rate_plan=decision.rate_plan,
        )

        if decision.should_vend:
            self.vend.vend(decision.reason)
            self.events.record("vended", lane, reason=decision.reason, plate=identity.plate)

            # The session action goes on the SAME queue as the log, so a lane
            # that was offline replays what happened in the order it happened.
            # The timestamp is the lane's, not the platform's: the car arrived
            # when it arrived, whatever time the server eventually hears about
            # it. Pricing a stay by when the network came back would be wrong.
            at = to_iso(self._clock())
            if self.config.direction == "entry":
                self.events.record(
                    SESSION_OPEN,
                    lane,
                    plate=identity.plate,
                    plate_region=identity.plate_region,
                    at=at,
                )
            else:
                # Ask the platform which session this is, while the answer is
                # still unambiguous. If it cannot be reached the close goes out
                # without an id and the platform falls back to matching on the
                # plate -- which works, and is merely less precise.
                session_id = None
                if self.session_lookup is not None:
                    found = self.session_lookup(identity.plate)
                    if found:
                        session_id = found.get("session", {}).get("id")
                self.events.record(
                    SESSION_CLOSE, lane, plate=identity.plate, at=at, session_id=session_id
                )

        elif decision.outcome is Outcome.FALLBACK:
            # Not a guess and not a silent drop. The fallback is a named path
            # with an event behind it, so an operator can see it happened and
            # the record shows why the lane declined to decide.
            #
            # The event is the whole of the fallback for now: the human/phone
            # path that answers it belongs with Claim Check and is not built.
            # It is a stub that LOGS, not a stub that pretends.
            log.info("lane %s falling back: %s", lane, decision.reason)
            self.events.record(
                "fallback_needs_human",
                lane,
                reason=decision.reason,
                fallback=decision.fallback.value if decision.fallback else None,
                plate=identity.plate,
                confidence=identity.confidence,
            )

        # Best effort, and after the barrier has already been told what to do.
        # Nothing above this line waits on the network.
        self.events.flush()
        return decision

    def run_once(self, timeout: float | None = None) -> Decision | None:
        """Wait for one vehicle and serve it. None if none arrived in time."""
        if not self.loop.wait_for_vehicle(timeout=timeout):
            return None
        return self.handle_arrival()

    def run_forever(self, timeout: float | None = 1.0) -> None:  # pragma: no cover
        while True:
            self.run_once(timeout=timeout)
