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
# Deliberate breakage, for the fallback-cause fail-control.
#
# scripts/fallback_cause_fail_control.py sets BREAK_FALLBACK_CAUSE and requires
# the engine-unreachable suite to FAIL. Each mode breaks exactly one point that
# carries the guarantee -- the ordering in `decide`, the classification in the
# client, or the cause reaching the record -- so a control that passes says the
# suite measures that point and not something beside it.
#
# The last mode is the one facing the other way: a change that reported EVERY
# fallback as an unreachable engine would satisfy every assertion about a dead
# engine, and is the same defect with the arrow reversed.
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_fallback_cause(monkeypatch):
    mode = os.environ.get("BREAK_FALLBACK_CAUSE")
    if not mode:
        return

    from dataclasses import replace

    from lane_controller import decision as decision_module
    from lane_controller import vehicle_id_client as client_module
    from lane_controller.decision import Fallback
    from lane_controller.events import EventQueue

    original_decide = decision_module.decide

    def patch_decide(replacement):
        # `decide` is bound by name in every module that imported it -- the
        # module that defines it, `controller`, the package's `__init__`, and
        # each test module -- and rebinding one of them leaves the others
        # holding the original function object.
        #
        # Found the hard way on this control's first run: patching `decision`
        # and `controller` still turned the suite red, on ONE test, through the
        # lane rather than through the break this describes. A check that
        # passes for the wrong reason has failed. So the bindings are DERIVED
        # -- every one that is the original object is replaced -- rather than
        # listed, because a list cannot notice the next importer.
        import sys

        for module in list(sys.modules.values()):
            if getattr(module, "decide", None) is original_decide:
                monkeypatch.setattr(module, "decide", replacement)

    if mode == "merge":
        # The ordering in `decide` is gone: `unavailable` is set on the
        # identity and nothing looks at it, so the 0.0 left behind by a failed
        # request is compared against a threshold again. This is the defect
        # exactly as it stood.
        def blind_decide(identity, cache, **kwargs):
            return original_decide(replace(identity, unavailable=None), cache, **kwargs)

        patch_decide(blind_decide)

    elif mode == "blind":
        # A different point, and one `merge` cannot reach: the CLIENT stops
        # saying which failure it had, so there is nothing for the ordering to
        # find. Two lines carry this guarantee and each is broken on its own.
        original_identify = client_module.VehicleIdClient.identify

        def forgetful_identify(self, frames):
            return replace(original_identify(self, frames), unavailable=None)

        monkeypatch.setattr(client_module.VehicleIdClient, "identify", forgetful_identify)

    elif mode == "swallow":
        # The arrow reversed: every fallback becomes an unreachable engine, so
        # a plate the engine looked at and would not vouch for is reported as a
        # service being down. Only the control catches this one.
        def greedy_decide(identity, cache, **kwargs):
            outcome = original_decide(identity, cache, **kwargs)
            if outcome.fallback is not None:
                return replace(outcome, fallback=Fallback.ENGINE_UNREACHABLE)
            return outcome

        patch_decide(greedy_decide)

    elif mode == "onecause":
        # One code for what the lane did is the design; one CAUSE for every
        # failure is not. A camera that handed over nothing, a service that is
        # off and a service that is slow are three repairs, and this collapses
        # them into a record nobody can act on.
        monkeypatch.setattr(client_module, "_cause", lambda exc: client_module.CAUSE_UNREACHABLE)

    elif mode == "nodetail":
        # The cause is measured and never leaves the process. Whoever answers
        # the intercom sees a code and no reason for it.
        original_record = EventQueue.record

        def silent_record(self, kind, lane_id, **detail):
            if kind == "fallback_needs_human":
                detail.pop("cause", None)
            return original_record(self, kind, lane_id, **detail)

        monkeypatch.setattr(EventQueue, "record", silent_record)

    else:
        raise RuntimeError(f"unknown BREAK_FALLBACK_CAUSE mode: {mode}")
