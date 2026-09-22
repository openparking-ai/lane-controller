"""The outbox drains off the lane thread.

Measured after PR #28: `run_once` flushed the outbox ON THE LANE THREAD after
the vend. Against a platform that refuses that cost 0.3 ms; against one that
black-holes -- the ordinary shape of a site outage, the condition the exit
round was designed for -- it cost the client's timeout (5 s by default) per
queued session action, before the next car could be served. Not the car
being served: the car behind it, when the queue is longest.

Now the controller's every "the record leaves the box" is `deliver()`, and
WHICH THREAD WAITS is the whole of the change: a bare controller (the tests,
the demo) drains inline and `run_once` still returns delivered -- the offline
suite's shape, untouched; under `LaneRunner` the drain is a thread of its
own, `deliver()` is a signal, and the lane thread never waits on the network
after the vend either. The queue itself is now safe to record into while a
drain is mid-send, and a drain removes what it delivered, never what was
recorded meanwhile.

Each claim is paired with the case that falsifies it, and
`scripts/drain_fail_control.py` breaks the drain four ways and requires this
file to go red.
"""

from __future__ import annotations

import threading
import time

from fake_platform import FakePlatform
from lane_controller import CameraConfig, DecisionCache, GateConfig, LaneConfig
from lane_controller.controller import LaneController
from lane_controller.events import EventQueue, EventTransport
from lane_controller.interfaces import VehicleIdentity
from lane_controller.runner import LaneRunner
from lane_controller.simulated import (
    CannedCameraFeed,
    RecordingVendOutput,
    SimulatedLoopInput,
    StubVehicleIdentifier,
)
from lane_controller.sync import PlatformTransport

CAR = VehicleIdentity(plate="CAR-1", confidence=0.97, presence=True)
BLACK_HOLE_S = 3.0


def a_config() -> LaneConfig:
    return LaneConfig(
        lane_id="lane-drain",
        site_id="site-drain",
        camera=CameraConfig(camera_id="sim-cam", rtsp_url="", frames_per_read=1),
        gate=GateConfig(),
    )


def a_lane(transport, *, arrivals: int):
    cache = DecisionCache()
    cache.load([], default_action="allow")
    vend = RecordingVendOutput()
    controller = LaneController(
        a_config(),
        loop=SimulatedLoopInput(arrivals=arrivals),
        camera=CannedCameraFeed(),
        vend=vend,
        identifier=StubVehicleIdentifier([CAR] * arrivals),
        cache=cache,
        events=EventQueue(transport),
    )
    return controller, vend


class BlackHole(EventTransport):
    """A platform that accepts the connection and never answers: every send
    costs the client's whole timeout and delivers nothing."""

    def __init__(self, seconds: float = BLACK_HOLE_S) -> None:
        self.seconds = seconds
        self.sends = 0
        self.threads: list[str] = []

    def send(self, events) -> bool:
        self.sends += 1
        self.threads.append(threading.current_thread().name)
        time.sleep(self.seconds)
        return False


class TimedVend(RecordingVendOutput):
    def __init__(self) -> None:
        super().__init__()
        self.at: list[float] = []

    def vend(self, reason: str) -> None:
        self.at.append(time.monotonic())
        super().vend(reason)


def a_runner(controller) -> LaneRunner:
    return LaneRunner(controller, client=None, rules_refresh_s=300.0, stays_refresh_s=5.0)


def wait_for(predicate, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)
    return True


# ---------------------------------------------------------------------------
# the lane thread never waits on the network after the vend
# ---------------------------------------------------------------------------


def test_under_the_runner_the_next_car_is_served_while_the_platform_black_holes():
    """Two cars, a platform that swallows every send for three seconds. The
    second car vends well inside one second of the first: the drain thread
    is the one stuck in the send, and it is named as such."""
    hole = BlackHole()
    controller, _ = a_lane(hole, arrivals=2)
    vend = TimedVend()
    controller.vend = vend
    runner = a_runner(controller)
    runner.start()
    try:
        assert sorted(t.name for t in runner._threads) == ["lane", "outbox-drain"]
        assert wait_for(lambda: len(vend.at) == 2, seconds=2.0), (
            f"{len(vend.at)} vend(s) inside two seconds: the lane thread waited on the platform"
        )
        gap = vend.at[1] - vend.at[0]
        assert gap < 1.0, f"the second car waited {gap:.2f}s behind the first car's flush"
        assert wait_for(lambda: hole.sends >= 1, seconds=1.0)
        assert set(hole.threads) == {"outbox-drain"}, hole.threads
    finally:
        runner.stop(timeout=0.1)
    # the control: a bare controller, no runner, waits on the same black hole
    controller, _ = a_lane(BlackHole(seconds=0.4), arrivals=1)
    started = time.monotonic()
    controller.run_once()
    assert time.monotonic() - started >= 0.4, "the bare controller no longer delivers inline"


def test_a_bare_controller_still_delivers_before_run_once_returns():
    platform = FakePlatform()
    controller, _ = a_lane(PlatformTransport(platform), arrivals=1)
    controller.run_once()
    assert platform.unique_opens == 1, "the offline suite's shape: delivered inline, no runner"
    assert controller.events.pending == 0


# ---------------------------------------------------------------------------
# the drain delivers: on the signal, and again on its own while anything waits
# ---------------------------------------------------------------------------


def test_under_the_runner_the_record_reaches_the_platform_without_the_lane_waiting():
    platform = FakePlatform()
    controller, _ = a_lane(PlatformTransport(platform), arrivals=1)
    runner = a_runner(controller)
    runner.start()
    try:
        assert wait_for(lambda: platform.unique_opens == 1, seconds=2.0), (
            "the signal drained nothing"
        )
        assert runner.state.drain_turns >= 1
        assert controller.events.pending == 0
    finally:
        runner.stop()
    # after stop, the controller drains inline again
    assert controller._drain is None


def test_an_outbox_the_platform_refused_is_retried_by_the_drain_without_another_car():
    platform = FakePlatform(online=False)
    controller, _ = a_lane(PlatformTransport(platform), arrivals=1)
    runner = a_runner(controller)
    runner.drain_retry_s = 0.05
    runner.start()
    try:
        assert wait_for(lambda: runner.state.drain_turns >= 1, seconds=2.0)
        assert platform.unique_opens == 0 and controller.events.pending >= 1
        turns = runner.state.drain_turns
        platform.online = True  # the platform comes back; nobody arrives
        assert wait_for(lambda: platform.unique_opens == 1, seconds=2.0), (
            "the drain never retried an outbox the platform had refused"
        )
        assert runner.state.drain_turns > turns
        assert controller.events.pending == 0
    finally:
        runner.stop()


def test_a_lane_with_no_platform_has_no_drain_thread_and_no_drain_installed():
    controller, _ = a_lane(None, arrivals=0)
    runner = a_runner(controller)
    runner.start()
    try:
        assert [t.name for t in runner._threads] == ["lane"]
        assert controller._drain is None
    finally:
        runner.stop()


# ---------------------------------------------------------------------------
# the queue under two threads
# ---------------------------------------------------------------------------


class SendThatWaits(EventTransport):
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.batches: list[list] = []

    def send(self, events) -> bool:
        self.batches.append(list(events))
        self.entered.set()
        self.release.wait(timeout=5)
        return True


def test_an_event_recorded_during_a_send_is_not_thrown_away_with_the_batch():
    """The old `flush()` cleared the deques after the send returned. With
    the send on another thread that is every event the lane recorded while
    the platform was slow -- a car served during the outage, gone from the
    record. What leaves the queue is the batch, and only the batch."""
    transport = SendThatWaits()
    queue = EventQueue(transport)
    queue.record("session_open", "lane-drain", plate="CAR-1")
    queue.record("armed", "lane-drain")
    drain = threading.Thread(target=queue.flush, name="outbox-drain", daemon=True)
    drain.start()
    assert transport.entered.wait(timeout=5)
    # mid-send, the lane thread records the next car
    queue.record("session_open", "lane-drain", plate="CAR-2")
    queue.record("vended", "lane-drain")
    transport.release.set()
    drain.join(timeout=5)
    assert len(transport.batches[0]) == 2
    assert queue.pending == 2, "the events recorded during the send were cleared with the batch"
    assert queue.pending_sessions == 1
    transport.release.set()
    assert queue.flush() == 2
    assert [e.detail.get("plate") for e in transport.batches[1]] == ["CAR-2", None]
    assert queue.pending == 0


def test_two_drains_at_once_do_not_race_each_others_removal():
    """The settle worker and the drain thread can both flush. Serialised,
    both see a consistent queue; the outcome is every event delivered once
    or twice (idempotent endpoints) and none lost."""
    platform = FakePlatform()
    queue = EventQueue(PlatformTransport(platform))
    for n in range(50):
        queue.record(
            "session_open", "lane-drain", plate=f"CAR-{n}",
            at="2026-06-10T18:00:00+00:00", entry_confirmation="confirmed",
        )
    threads = [threading.Thread(target=queue.flush, daemon=True) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert queue.pending == 0
    assert platform.unique_opens == 50
