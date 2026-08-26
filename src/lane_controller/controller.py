"""The lane sequence.

    arming loop  ->  grab frames  ->  identify  ->  decide  ->  vend

and then nothing. The barrier closes itself on its own closing loop; the
controller has no part in it and no way to interfere with it.

The whole sequence runs against simulated implementations of all three seams,
which is why `tests/` needs no hardware.
"""

from __future__ import annotations

import logging

from .config import LaneConfig
from .decision import Decision, DecisionCache, Outcome, decide
from .events import EventQueue
from .interfaces import CameraFeed, LoopInput, VehicleIdentifier, VendOutput

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
    ) -> None:
        self.config = config
        self.loop = loop
        self.camera = camera
        self.vend = vend
        self.identifier = identifier
        self.cache = cache or DecisionCache(max_age_seconds=config.rules_max_age_seconds)
        self.events = events or EventQueue()

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
        elif decision.outcome is Outcome.FALLBACK:
            # Not a guess and not a silent drop. The fallback is a named path
            # with an event behind it, so an operator can see it happened and
            # the record shows why the lane declined to decide.
            log.info("lane %s falling back: %s", lane, decision.reason)

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
