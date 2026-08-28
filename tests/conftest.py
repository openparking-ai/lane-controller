import pytest

from lane_controller import (
    CameraConfig,
    DecisionCache,
    GateConfig,
    LaneConfig,
    LaneController,
    Rule,
    VehicleIdentity,
)
from lane_controller.simulated import (
    CannedCameraFeed,
    RecordingVendOutput,
    SimulatedLoopInput,
    StubVehicleIdentifier,
)


@pytest.fixture
def config():
    return LaneConfig(
        lane_id="lane-test",
        site_id="site-test",
        camera=CameraConfig(camera_id="sim-cam-1", rtsp_url="", frames_per_read=3),
        gate=GateConfig(),
        confidence_threshold=0.85,
    )


@pytest.fixture
def cache():
    cache = DecisionCache()
    cache.load(
        [
            Rule(plate="SIM-0001", allow=True, rate_plan="monthly"),
            Rule(plate="BANNED-1", allow=False),
        ]
    )
    return cache


def build_lane(config, cache, identities, arrivals=1):
    """A whole lane, wired to simulated hardware. No Jetson, no camera, no barrier."""
    vend = RecordingVendOutput()
    controller = LaneController(
        config,
        loop=SimulatedLoopInput(arrivals=arrivals),
        camera=CannedCameraFeed(),
        vend=vend,
        identifier=StubVehicleIdentifier(identities),
        cache=cache,
    )
    return controller, vend


@pytest.fixture
def lane(config, cache):
    def _build(identities: list[VehicleIdentity], arrivals: int = 1):
        return build_lane(config, cache, identities, arrivals)

    return _build


# ---------------------------------------------------------------------------
# Deliberate breakage, for the offline fail-control.
#
# scripts/offline_fail_control.py sets BREAK_OFFLINE_QUEUE and requires the
# offline suite to FAIL. An offline guarantee that has never been observed
# failing is not known to be a guarantee.
# ---------------------------------------------------------------------------
import os  # noqa: E402

import pytest as _pytest  # noqa: E402


@_pytest.fixture(autouse=True)
def _break_the_queue(monkeypatch):
    mode = os.environ.get("BREAK_OFFLINE_QUEUE")
    if not mode:
        return

    from lane_controller.events import EventQueue
    from lane_controller.sync import PlatformTransport

    if mode == "drop":
        # The queue assumes delivery succeeded and clears regardless. This is
        # what "offline-tolerant" looks like when it is only claimed.
        def lossy_flush(self):
            # Must clear the REAL storage. _queue is a computed view now, so
            # clearing it would clear a temporary list and drop nothing -- the
            # breakage would then be simulating nothing and the control would
            # pass for the wrong reason.
            batch = self._queue
            if self._transport is not None:
                try:
                    self._transport.send(batch)
                except Exception:
                    pass
            self._log.clear()
            self._sessions.clear()
            return len(batch)

        monkeypatch.setattr(EventQueue, "flush", lossy_flush)

    elif mode == "noid":
        # Delivery happens twice -- an acknowledgement lost on the way back,
        # which is ordinary -- and the event id is regenerated each time, so
        # the platform cannot tell the second attempt from new activity.
        import uuid

        original = PlatformTransport.send

        def duplicating_send(self, events):
            def fresh(batch):
                return [
                    type(e)(
                        kind=e.kind,
                        lane_id=e.lane_id,
                        at=e.at,
                        detail=e.detail,
                        event_id=str(uuid.uuid4()),
                    )
                    for e in batch
                ]

            if not original(self, fresh(events)):
                return False
            return original(self, fresh(events))

        monkeypatch.setattr(PlatformTransport, "send", duplicating_send)

    else:
        raise RuntimeError(f"unknown BREAK_OFFLINE_QUEUE mode: {mode}")


# ---------------------------------------------------------------------------
# Deliberate breakage, for the confirmation fail-control.
#
# scripts/confirmation_fail_control.py sets BREAK_CONFIRMATION and requires the
# loop suite to FAIL. Each mode breaks exactly one decision point -- not a
# fixture, not a stub, the code that decides -- so a control that passes is
# telling us the suite measures that decision and not something beside it.
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_confirmation(monkeypatch):
    mode = os.environ.get("BREAK_CONFIRMATION")
    if not mode:
        return

    from lane_controller.controller import LaneController
    from lane_controller.interfaces import ClosingSequence

    if mode == "window":
        # The configured window is ignored, so a crossing at any speed
        # confirms. "Something happened here eventually" is not a vehicle
        # going through the gate.
        monkeypatch.setattr(LaneController, "_confirmation_window", lambda self: float("inf"))

    elif mode == "elapsed":
        # The window is still configured, still published on the event, and no
        # longer COMPARED against: whatever the loops report FORWARD for is
        # accepted however long it took. This is the break the `window` mode
        # cannot make -- that one changes the configured value, which the
        # fixture then honours, so it proves the config reaches the loops and
        # nothing about whether the controller applies it.
        monkeypatch.setattr(
            LaneController, "_within_window", staticmethod(lambda elapsed, window: True)
        )

    elif mode == "direction":
        # B-then-A confirms too, so a vehicle backing out of the gate opens a
        # session. One loop could not tell those apart; this is what having two
        # and not reading the order looks like.
        monkeypatch.setattr(
            LaneController,
            "_confirms",
            staticmethod(lambda crossing: crossing is not ClosingSequence.NONE),
        )

    elif mode == "promote":
        # The window elapsing with nothing at all confirms. This is the phantom
        # occupant: a ticket no car followed, promoted to a billable session.
        monkeypatch.setattr(
            LaneController,
            "_confirms",
            staticmethod(lambda crossing: crossing is not ClosingSequence.REVERSE),
        )

    elif mode == "arming":
        # One arming loop is enough. A person with a piece of metal on a single
        # loop arms the lane again.
        monkeypatch.setattr(LaneController, "_arming_complete", staticmethod(lambda loop_b: True))

    else:
        raise RuntimeError(f"unknown BREAK_CONFIRMATION mode: {mode}")


# ---------------------------------------------------------------------------
# Deliberate breakage, for the barrier-guard fail-control.
#
# scripts/barrier_guard_fail_control.py sets BREAK_BARRIER_GUARD and requires
# tests/test_barrier_guard.py to FAIL. Each mode plants ONE way this package
# could come to command a barrier down -- on the protocol, on an
# implementation, in the source the controller runs, and on the controller
# itself -- because the guard asserts four different things and a control that
# only breaks one of them says nothing about the other three.
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_barrier_guard(monkeypatch, request):
    mode = os.environ.get("BREAK_BARRIER_GUARD")
    if not mode:
        return

    def _close(self, *args, **kwargs):  # pragma: no cover - planted, never run
        """Lower the boom."""

    if mode == "protocol":
        # The one interface that reaches the barrier gains a second action.
        from lane_controller.interfaces import VendOutput

        monkeypatch.setattr(VendOutput, "close", _close, raising=False)

    elif mode == "impl":
        # The protocol is untouched; a relay implementation grows the method.
        # This is the shape that would arrive in a driver for real hardware.
        from lane_controller.simulated import RecordingVendOutput

        monkeypatch.setattr(RecordingVendOutput, "close", _close, raising=False)

    elif mode == "call":
        # Nothing DECLARES a close and the controller calls one anyway. The
        # plant is real source text, put where the guard reads source from, so
        # the check is fed the thing it exists to read.
        #
        # `request.module` and not an import: pytest has already imported the
        # test module under whatever name its rootdir gives it, and importing it
        # again by a guessed name patches a SECOND copy while the running test
        # reads the first. That is what this plant did on its first run, and it
        # reported the guard as not measuring when the guard was fine.
        guard = request.module
        if not hasattr(guard, "package_sources"):
            return

        real = guard.package_sources()
        planted = dict(real)
        before = planted["controller"]
        planted["controller"] = before.replace(
            "self.vend.vend(decision.reason)", "self.vend.close(decision.reason)", 1
        )
        if planted["controller"] == before:
            raise RuntimeError(
                "the 'call' plant matched nothing in controller.py -- it would prove nothing"
            )
        monkeypatch.setattr(guard, "package_sources", lambda: planted)

    elif mode == "controller":
        # The controller itself grows one, by the name somebody would use.
        from lane_controller.controller import LaneController

        monkeypatch.setattr(LaneController, "close_barrier", _close, raising=False)

    else:
        raise RuntimeError(f"unknown BREAK_BARRIER_GUARD mode: {mode}")
