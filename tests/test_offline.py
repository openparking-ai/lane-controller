"""Offline-first, proven rather than claimed.

The lane must keep deciding with the platform unreachable, hold what it could
not deliver, and deliver it on reconnect without producing duplicates.

Every assertion here is paired with a control in tests/test_offline_control.py,
which breaks the queue on purpose and requires these to fail. A test that has
never been seen to fail is not known to be measuring anything.
"""

from __future__ import annotations

import pytest

from fake_platform import FakePlatform
from lane_controller import (
    CameraConfig,
    DecisionCache,
    EventQueue,
    GateConfig,
    LaneConfig,
    LaneController,
    Outcome,
    PlatformTransport,
    VehicleIdentity,
    sync_rules,
)
from lane_controller.simulated import (
    CannedCameraFeed,
    RecordingVendOutput,
    SimulatedLoopInput,
    StubVehicleIdentifier,
)


def lane_config(direction="entry"):
    return LaneConfig(
        lane_id=f"lane-{direction}",
        site_id="site-1",
        camera=CameraConfig(camera_id="cam", rtsp_url="", frames_per_read=1),
        gate=GateConfig(),
        direction=direction,
        confidence_threshold=0.85,
    )


def build(platform, *, direction="entry", arrivals=1, identities=None):
    cache = DecisionCache()
    sync_rules(platform, cache)  # platform stands in for PlatformClient
    transport = PlatformTransport(platform)
    vend = RecordingVendOutput()
    controller = LaneController(
        lane_config(direction),
        loop=SimulatedLoopInput(arrivals=arrivals),
        camera=CannedCameraFeed(),
        vend=vend,
        identifier=StubVehicleIdentifier(
            identities or [VehicleIdentity(plate="OFF-1", confidence=0.97)]
        ),
        cache=cache,
        events=EventQueue(transport),
    )
    return controller, vend, transport


def test_the_gate_still_opens_with_the_platform_unreachable():
    platform = FakePlatform()
    controller, vend, _ = build(platform)

    platform.online = False  # the outage starts here
    decision = controller.run_once()

    assert decision.outcome is Outcome.ALLOW, "the decision comes from cache, not from the network"
    assert vend.vend_count == 1, "an unreachable platform must not keep a car at the barrier"


def test_nothing_reaches_the_platform_while_it_is_down():
    platform = FakePlatform()
    controller, _, _ = build(platform)

    platform.online = False
    controller.run_once()

    assert platform.unique_events == 0
    assert platform.opened == []
    assert controller.events.pending > 0, "the work is held, not lost"


def test_the_queue_drains_on_reconnect():
    platform = FakePlatform()
    controller, _, _ = build(platform)

    platform.online = False
    controller.run_once()
    held = controller.events.pending
    assert held > 0

    platform.online = True
    delivered = controller.events.flush()

    assert delivered == held
    assert controller.events.pending == 0
    assert platform.unique_opens == 1, "the session that happened offline reached the platform"


def test_reconnecting_does_not_duplicate_anything():
    """The point of the event id and of every endpoint being idempotent."""
    platform = FakePlatform()
    controller, _, _ = build(platform)

    platform.online = False
    controller.run_once()

    # Three flushes: the first two fail because the platform is still down, so
    # the third re-sends everything from the beginning -- which is exactly what
    # a real lane does, and exactly where duplicates would come from.
    plain_count = len(
        [
            e
            for e in list(controller.events._queue)
            if e.kind not in ("session_open", "session_close")
        ]
    )

    assert controller.events.flush() == 0
    assert controller.events.flush() == 0
    platform.online = True
    controller.events.flush()

    assert platform.unique_opens == 1, "the same arrival must not open two sessions"
    # Compared against what the lane actually recorded, not against itself: a
    # self-consistent count would stay true even if every id were regenerated
    # on each attempt, which is the failure this is here to catch.
    assert (
        platform.unique_events == plain_count
    ), (
        f"{platform.unique_events} events landed for {plain_count} recorded"
        " — delivery is not idempotent"
    )


def test_a_stay_that_spans_an_outage_is_priced_from_lane_time():
    """Entry offline, exit online. The fee must come from when the car was
    actually there, not from when the network came back."""
    platform = FakePlatform()

    entry, entry_vend, _ = build(platform, direction="entry")
    platform.online = False
    entry.run_once()
    platform.online = True
    entry.events.flush()

    exit_lane, exit_vend, _ = build(platform, direction="exit")
    exit_lane.run_once()

    assert entry_vend.vend_count == 1
    assert exit_vend.vend_count == 1
    assert len(platform.closed) == 1
    assert platform.opened[0]["entry_at"] < platform.closed[0]["exit_at"]
    assert platform.open_sessions == {}, "the session was closed, not left open"


def test_an_outage_during_a_flush_leaves_the_work_queued():
    platform = FakePlatform()
    controller, _, _ = build(platform)
    controller.run_once()  # delivered while online
    assert controller.events.pending == 0

    platform.online = False
    controller.events.record("probe", "lane-entry", note="written during the outage")
    assert controller.events.flush() == 0
    assert controller.events.pending == 1


def test_a_rule_sync_failure_keeps_the_rules_it_already_had():
    platform = FakePlatform()
    cache = DecisionCache()
    assert sync_rules(platform, cache) is not None
    assert cache.default_action == "allow"

    platform.online = False
    assert sync_rules(platform, cache) is None
    assert cache.default_action == "allow", "a failed sync must not blank the cache"
    assert not cache.is_stale(), "a failed sync must not mark good rules stale"


def test_a_refused_item_is_dropped_and_counted_not_retried_forever():
    """A close with no matching open is poison: it can never succeed."""
    platform = FakePlatform()
    controller, _, transport = build(platform, direction="exit")

    controller.run_once()  # closes a session that was never opened

    assert transport.rejected == 1
    assert controller.events.pending == 0, "poison must not block everything behind it"


def test_low_confidence_offline_still_falls_back_rather_than_guessing(caplog):
    platform = FakePlatform()
    controller, vend, _ = build(
        platform, identities=[VehicleIdentity(plate="OFF-1", confidence=0.20)]
    )
    platform.online = False

    decision = controller.run_once()

    assert decision.outcome is Outcome.FALLBACK
    assert vend.vend_count == 0
    kinds = [e.kind for e in list(controller.events._queue)]
    assert "fallback_needs_human" in kinds, "the fallback must leave a record a human can find"


@pytest.mark.parametrize("direction", ["entry", "exit"])
def test_both_directions_work_offline(direction):
    platform = FakePlatform()
    platform.reject_close_without_open = False
    controller, vend, _ = build(platform, direction=direction)
    platform.online = False

    assert controller.run_once().outcome is Outcome.ALLOW
    assert vend.vend_count == 1
    assert controller.events.pending > 0


def test_an_entry_replayed_after_the_car_left_does_not_double_open():
    """A lost acknowledgement, which is ordinary, must not create a phantom.

    The entry lane delivers its open, the reply never arrives, so the item stays
    queued. Meanwhile the car leaves and the exit lane -- a different controller
    with its own outbox -- closes the session. Only then does the entry lane
    re-send.

    Keying on state rather than on the event id gets this wrong: there is no
    open session by then, so a second one is created, never exits, and the
    garage's inside-count is wrong from that moment on.
    """
    platform = FakePlatform()
    entry, _, entry_transport = build(platform, direction="entry")

    platform.online = False
    entry.run_once()
    held = list(entry.events._queue)
    platform.online = True
    assert entry.events.flush() == len(held)
    assert platform.unique_opens == 1

    exit_lane, _, _ = build(platform, direction="exit")
    exit_lane.run_once()
    assert platform.open_sessions == {}, "the car has left"

    # The entry lane never saw the reply, so it delivers the identical batch again.
    assert entry_transport.send(held) is True

    assert platform.unique_opens == 1, "a replayed entry must not open a second session"
    assert platform.open_sessions == {}, "no phantom open session may be left behind"
    assert platform.open_deliveries > platform.unique_opens, (
        "control: the replay really was delivered, so the count above means dedup, not silence"
    )
