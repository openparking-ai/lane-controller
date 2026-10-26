"""Offline-first, proven rather than claimed.

The lane must keep deciding with the platform unreachable, hold what it could
not deliver, and deliver it on reconnect without producing duplicates.

Every assertion here is paired with a control in tests/test_offline_control.py,
which breaks the queue on purpose and requires these to fail. A test that has
never been seen to fail is not known to be measuring anything.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

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
from lane_controller.durable import OutboxStore
from lane_controller.events import LOG_CHUNK
from lane_controller.platform_client import PlatformRejected, PlatformUnreachable
from lane_controller.simulated import (
    CannedCameraFeed,
    RecordingVendOutput,
    SimulatedLoopInput,
    StubVehicleIdentifier,
)
from lane_controller.sync import LOG_CHUNK_BYTES


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


class PlatformBehindThisLane(FakePlatform):
    """The platform as it is BEFORE it records what confirmed a session.

    It accepts the call, answers with a session, and drops the field -- which is
    what an older platform genuinely does, because the column is not there and
    the route echoes the row it wrote. Nothing about that response is an error,
    and that is the whole problem: deploy this lane ahead of its platform and
    every confirmed session and every unconfirmable one become the same row.

    BOTH ENDS, because migration 0005 adds both columns and a platform without
    it drops both. An exit is where the money is written, so a close nothing
    recorded is the same silence over a larger number.
    """

    def open_session(self, **kwargs) -> dict:
        result = super().open_session(**kwargs)
        session = {k: v for k, v in result["session"].items() if k != "entry_confirmation"}
        return {**result, "session": session}

    def close_session(self, **kwargs) -> dict:
        result = super().close_session(**kwargs)
        session = {k: v for k, v in (result["session"] or {}).items() if k != "exit_confirmation"}
        return {**result, "session": session}


def test_a_platform_that_does_not_record_the_confirmation_is_refused_loudly(caplog):
    caplog.set_level(logging.ERROR)
    platform = PlatformBehindThisLane()
    controller, vend, transport = build(platform)

    controller.run_once()

    assert vend.vend_count == 1, "the barrier still opened; the car is not the one being refused"
    assert transport.rejected == 1, "an open the platform did not record was counted as delivered"
    assert controller.events.pending == 0, "poison must not block everything behind it"
    assert any(
        "entry_confirmation" in record.getMessage() for record in caplog.records
    ), "the drop must name what the platform did not record"


def test_a_platform_that_does_record_it_delivers_the_open():
    """The control. Without it, the test above is satisfied by a lane whose
    opens are refused by every platform there is."""
    platform = FakePlatform()
    controller, _, transport = build(platform)

    controller.run_once()

    assert transport.rejected == 0
    assert platform.unique_opens == 1
    assert platform.opened[0]["entry_confirmation"] == "unconfirmable"


DESCRIPTOR = "opvid-opa-id/2:eJwBDiTx2offline"


def test_an_open_carries_the_descriptor_the_read_produced_and_the_platform_echoes_it():
    """The read produced a descriptor; the open carries it; the platform hands
    it back on the row; the lane counts the open delivered."""
    platform = FakePlatform()
    controller, vend, transport = build(
        platform,
        identities=[VehicleIdentity(plate="OFF-D", confidence=0.97, descriptor=DESCRIPTOR)],
    )

    controller.run_once()

    assert vend.vend_count == 1
    assert transport.rejected == 0
    assert platform.opened[0]["descriptor"] == DESCRIPTOR
    assert platform.opened[0]["plate"] == "OFF-D", "beside the identity, not instead of it"


def test_an_open_whose_read_produced_no_descriptor_sends_none():
    """The default. The service produces a descriptor only when a deployment
    switches it on, so most opens carry none -- and the lane requires no echo
    for a value it did not send."""
    platform = FakePlatform()
    controller, _, transport = build(platform)

    controller.run_once()

    assert transport.rejected == 0
    assert platform.opened[0]["descriptor"] is None


class PlatformWithoutTheDescriptorColumn(FakePlatform):
    """The platform as it is BEFORE migration 0009.

    It accepts the open, answers with the session, and the row it echoes has no
    `entry_descriptor` -- exactly as the real route behaves, because it
    destructures the keys it knows and ignores the rest. Not an error, not a
    400: a 201 and a silent loss, which is why the echo is the check.
    """

    def open_session(self, **kwargs) -> dict:
        result = super().open_session(**kwargs)
        session = {k: v for k, v in result["session"].items() if k != "entry_descriptor"}
        return {**result, "session": session}


def test_a_platform_that_does_not_record_the_descriptor_is_refused_loudly(caplog):
    caplog.set_level(logging.ERROR)
    platform = PlatformWithoutTheDescriptorColumn()
    controller, vend, transport = build(
        platform,
        identities=[VehicleIdentity(plate="OFF-D2", confidence=0.97, descriptor=DESCRIPTOR)],
    )

    controller.run_once()

    assert vend.vend_count == 1, "the barrier still opened; the car is not the one being refused"
    assert transport.rejected == 1, (
        "an open the platform silently dropped the descriptor from was counted as delivered"
    )
    assert controller.events.pending == 0, "poison must not block everything behind it"
    assert any("descriptor" in record.getMessage() for record in caplog.records), (
        "the drop must name what the platform did not record"
    )
    assert not any(DESCRIPTOR in record.getMessage() for record in caplog.records), (
        "and it must not copy the descriptor into a log line"
    )


def test_a_lane_with_no_descriptor_is_unchanged_against_that_platform():
    """THE CONTROL on the refusal above, and the compatibility rule: a lane
    whose service produces no descriptor sends the open it always sent, and a
    platform older than the column delivers it exactly as before."""
    platform = PlatformWithoutTheDescriptorColumn()
    controller, _, transport = build(platform)

    controller.run_once()

    assert transport.rejected == 0
    assert platform.unique_opens == 1


class PlatformWithoutTheExitDescriptorColumn(FakePlatform):
    """The platform as it is BEFORE migration 0010: it has the entry column
    and not the exit one, closes the stay, bills it, and the row it echoes has
    no `exit_descriptor`. A 200 and a silent loss, at the end where the money
    is written."""

    def close_session(self, **kwargs) -> dict:
        result = super().close_session(**kwargs)
        session = {k: v for k, v in (result["session"] or {}).items() if k != "exit_descriptor"}
        return {**result, "session": session}


def test_a_close_carries_the_exit_reads_descriptor_and_the_platform_echoes_it():
    """THE CLOSE, and no other channel. The exit read produced a descriptor;
    the close carries it; the platform hands it back on the row."""
    platform = FakePlatform()
    entry, _, _ = build(platform, direction="entry")
    entry.run_once()

    exit_lane, vend, transport = build(
        platform,
        direction="exit",
        identities=[VehicleIdentity(plate="OFF-1", confidence=0.97, descriptor=DESCRIPTOR)],
    )
    transport.session_lookup = lambda plate: platform.find_open_session(plate=plate)
    exit_lane.run_once()

    assert vend.vend_count == 1
    assert transport.rejected == 0
    assert platform.closed[0]["descriptor"] == DESCRIPTOR
    assert platform.closed[0]["plate"] == "OFF-1", "beside the identity, not instead of it"
    assert transport.last_close["session"]["exit_descriptor"] == DESCRIPTOR
    # And nothing about the exit read reached the ENTRY end of the row.
    assert transport.last_close["session"]["entry_descriptor"] is None


def test_a_platform_that_does_not_record_the_exit_descriptor_is_refused_loudly(caplog):
    caplog.set_level(logging.ERROR)
    platform = PlatformWithoutTheExitDescriptorColumn()
    entry, _, _ = build(platform, direction="entry")
    entry.run_once()

    exit_lane, vend, transport = build(
        platform,
        direction="exit",
        identities=[VehicleIdentity(plate="OFF-1", confidence=0.97, descriptor=DESCRIPTOR)],
    )
    transport.session_lookup = lambda plate: platform.find_open_session(plate=plate)
    exit_lane.run_once()

    assert vend.vend_count == 1, "the barrier still opened; the car is not the one being refused"
    assert transport.rejected == 1, (
        "a close the platform silently dropped the descriptor from was counted as delivered"
    )
    assert transport.last_close is None, "a response that does not say must not become the record"
    assert exit_lane.events.pending == 0, "poison must not block everything behind it"
    assert any(
        "exit_descriptor" in record.getMessage() for record in caplog.records
    ), "the drop must name which end the platform did not record"
    assert not any(DESCRIPTOR in record.getMessage() for record in caplog.records)


def test_a_close_with_no_descriptor_is_unchanged_against_that_platform():
    """THE CONTROL, and the compatibility rule at the exit end."""
    platform = PlatformWithoutTheExitDescriptorColumn()
    entry, _, _ = build(platform, direction="entry")
    entry.run_once()

    exit_lane, _, transport = build(platform, direction="exit")
    transport.session_lookup = lambda plate: platform.find_open_session(plate=plate)
    exit_lane.run_once()

    assert transport.rejected == 0
    assert platform.closed[0]["descriptor"] is None
    assert transport.last_close is not None


def test_a_platform_that_does_not_record_the_exit_confirmation_is_refused_loudly(caplog):
    """C4, at the other end of the stay. The exit is where the money is written,
    and against a platform that predates the column the close is accepted, the
    stay is billed, and nothing records what saw the car leave."""
    caplog.set_level(logging.ERROR)
    platform = PlatformBehindThisLane()
    entry, _, _ = build(platform, direction="entry")
    entry.run_once()  # the session exists on the platform; its open was dropped loudly

    exit_lane, vend, transport = build(platform, direction="exit")
    transport.session_lookup = lambda plate: platform.find_open_session(plate=plate)
    exit_lane.run_once()

    assert vend.vend_count == 1, "the barrier still opened; the car is not the one being refused"
    assert transport.rejected == 1, "a close the platform did not record was counted as delivered"
    assert transport.last_close is None, "a response that does not say must not become the record"
    assert exit_lane.events.pending == 0, "poison must not block everything behind it"
    assert any(
        "exit_confirmation" in record.getMessage() for record in caplog.records
    ), "the drop must name what the platform did not record"


def test_a_platform_that_does_record_it_delivers_the_close():
    """The control. Without it, the test above is satisfied by a lane whose
    closes are refused by every platform there is."""
    platform = FakePlatform()
    entry, _, _ = build(platform, direction="entry")
    entry.run_once()

    exit_lane, _, transport = build(platform, direction="exit")
    transport.session_lookup = lambda plate: platform.find_open_session(plate=plate)
    exit_lane.run_once()

    assert transport.rejected == 0
    assert len(platform.closed) == 1
    assert platform.closed[0]["exit_confirmation"] == "unconfirmable"
    assert transport.last_close["session"]["exit_confirmation"] == "unconfirmable"


def test_a_refused_item_is_dropped_and_counted_not_retried_forever():
    """A close with no matching open is poison: it can never succeed."""
    platform = FakePlatform()
    controller, _, transport = build(platform, direction="exit")

    controller.run_once()  # closes a session that was never opened

    assert transport.rejected == 1
    assert controller.events.pending == 0, "poison must not block everything behind it"


def test_a_refused_log_batch_is_dropped_and_counted_rather_than_raised(caplog):
    """A 4xx on the batched log post behaves like every other refusal.

    It was the one call on this surface that was not guarded. The platform now
    refuses a kind it does not know, so this became reachable: the exception
    left `flush()`, left `run_once()` and ended the process -- after the barrier
    had already opened -- taking a memory-only outbox with it, while the counter
    that exists so a lost item is never silent stayed at zero.
    """
    platform = FakePlatform()
    platform.reject_event_kinds = {"totally_invented_kind"}
    controller, vend, transport = build(platform)

    with caplog.at_level(logging.ERROR):
        controller.run_once()
        vends_before = vend.vend_count
        controller.events.record("totally_invented_kind", "lane-entry")
        delivered = controller.events.flush()

    # The process is alive: nothing propagated out of flush().
    assert delivered > 0
    assert transport.rejected == 1
    assert controller.events.pending == 0, "the poisoned batch must not stay queued forever"
    assert vend.vend_count == vends_before, "a refused log post must not touch the barrier"
    dropped = [r.getMessage() for r in caplog.records if "dropping it" in r.getMessage()]
    assert dropped, "a dropped batch must not be silent"
    assert "400" in dropped[0], f"the drop must name the status: {dropped[0]}"
    assert "totally_invented_kind" in dropped[0], f"the drop must name the kind: {dropped[0]}"


def test_a_log_batch_the_platform_accepts_is_not_counted_as_rejected():
    """The control. Without it the test above is satisfied by a lane whose every
    log post is refused."""
    platform = FakePlatform()
    controller, _, transport = build(platform)

    controller.run_once()
    controller.events.record("totally_invented_kind", "lane-entry")
    assert controller.events.flush() > 0

    assert transport.rejected == 0
    assert controller.events.pending == 0


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


def test_session_actions_are_never_dropped_to_make_room():
    """Review item 6.

    The original outbox was one queue, oldest dropped first. In a long outage
    the oldest items are the session opens, so the cheapest thing to throw away
    was exactly the most expensive: cars that entered would have no session,
    exit to a refusal, and park free -- while the log entries that would have
    explained it were the ones still queued.
    """
    platform = FakePlatform()
    platform.online = False
    queue = EventQueue(PlatformTransport(platform), max_events=3)

    for i in range(3):
        queue.record("session_open", "lane-1", plate=f"CAR-{i}", at="t")
    for i in range(50):
        queue.record("frames_captured", "lane-1", n=i)

    assert queue.pending_sessions == 3, "not one session action may be dropped"
    assert queue.dropped > 0, "log events are what gets dropped"
    kinds = [e.kind for e in queue._queue]
    assert kinds.count("session_open") == 3
    assert len([k for k in kinds if k != "session_open"]) == 3, "the log stays bounded"


def test_a_dropped_log_event_is_counted_not_silent():
    queue = EventQueue(max_events=2)
    for i in range(5):
        queue.record("frames_captured", "lane-1", n=i)
    assert queue.pending == 2
    assert queue.dropped == 3


def test_the_exit_names_the_session_when_the_platform_is_reachable():
    """C5(a). Asked as the close is delivered, off the lane's thread
    (`tests/test_outbox_drain.py` holds which thread): a close delivered late
    is matched as one sent on the plate alone would be, and the platform's
    order check refuses an exit that precedes the open stay's entry."""
    platform = FakePlatform()
    entry, _, _ = build(platform, direction="entry")
    entry.run_once()

    exit_lane, _, transport = build(platform, direction="exit")
    transport.session_lookup = lambda plate: platform.find_open_session(plate=plate)
    exit_lane.run_once()

    assert len(platform.closed) == 1
    assert platform.closed[0]["session_id"] is not None, "the close must name the session"


def test_an_offline_exit_still_closes_without_a_session_id():
    """The lookup is best effort: a lane with no network must still open its gate."""
    platform = FakePlatform()
    platform.reject_close_without_open = False
    exit_lane, vend, transport = build(platform, direction="exit")
    transport.session_lookup = lambda plate: platform.find_open_session(plate=plate)

    platform.online = False
    decision = exit_lane.run_once()

    assert decision.outcome is Outcome.ALLOW
    assert vend.vend_count == 1
    queued = [e for e in exit_lane.events._queue if e.kind == "session_close"]
    assert len(queued) == 1
    assert queued[0].detail["session_id"] is None, "no id available offline, and that is fine"


# ---------------------------------------------------------------------------
# The outbox survives a restart.
#
# The stay opens and closes waiting for the platform lived in memory only, so
# a restart during an outage -- a power cut, a watchdog, an update -- lost every
# one the outbox held: the cars it opened left free (their close found no
# stay) and the stays it closed stayed open for ever. With a `[lane]
# cache_path`, they are kept in a file beside the cache as they are recorded
# and removed once the platform has taken them.
# ---------------------------------------------------------------------------


def a_narrow_directory(tmp_path):
    """A directory this process owns, mode 0700: what the store requires."""
    directory = tmp_path / "lane-state"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory


def queue_an_outage(queue, plates, *, close=None):
    """Session opens (and one close) recorded with the platform away."""
    for n, plate in enumerate(plates):
        queue.record("session_open", "lane-entry", plate=plate,
                     at=f"2026-10-16T09:0{n}:00+00:00", entry_confirmation="confirmed")
    if close is not None:
        queue.record("session_close", "lane-entry", plate=close,
                     at="2026-10-16T12:30:00+00:00", exit_confirmation="confirmed",
                     session_id=None)


def test_queued_stays_survive_the_process_being_killed_mid_outage(tmp_path):
    """The process is KILLED -- no stop, no flush, nothing run on the way out
    -- with three opens and a close queued and the platform unreachable. The
    next process, on the same file, delivers all four."""
    path = a_narrow_directory(tmp_path) / "lane.outbox.sqlite"
    child = textwrap.dedent(
        f"""
        import os, signal
        from lane_controller.durable import OutboxStore
        from lane_controller.events import EventQueue
        queue = EventQueue(store=OutboxStore({str(path)!r}))
        for n, plate in enumerate(["KILL-0", "KILL-1", "KILL-2"]):
            queue.record("session_open", "lane-entry", plate=plate,
                         at=f"2026-10-16T09:0{{n}}:00+00:00", entry_confirmation="confirmed")
        queue.record("session_close", "lane-entry", plate="KILL-0",
                     at="2026-10-16T12:30:00+00:00", exit_confirmation="confirmed",
                     session_id=None)
        queue.record("armed", "lane-entry")
        print(queue.pending_sessions, flush=True)
        os.kill(os.getpid(), signal.SIGKILL)
        """
    )
    src = Path(__file__).resolve().parent.parent / "src"
    killed = subprocess.run(
        [sys.executable, "-c", child], env={**os.environ, "PYTHONPATH": str(src)},
        capture_output=True, text=True, timeout=60,
    )
    assert killed.returncode == -signal.SIGKILL, killed.stderr
    assert killed.stdout.strip() == "4"

    platform = FakePlatform()
    queue = EventQueue(PlatformTransport(platform), store=OutboxStore(path))
    assert queue.pending_sessions == 4, "the stays the outbox held died with the process"
    assert queue.pending == 4, "log events are not kept: they are bounded and dropped first"
    assert queue.flush() == 4
    assert platform.unique_opens == 3
    assert [c["plate"] for c in platform.closed] == ["KILL-0"]
    assert platform.open_sessions.keys() == {"KILL-1", "KILL-2"}
    assert EventQueue(store=OutboxStore(path)).pending_sessions == 0, (
        "what the platform took is still in the file"
    )


def test_a_restarted_queue_replays_what_it_held_in_the_order_it_happened(tmp_path):
    path = a_narrow_directory(tmp_path) / "lane.outbox.sqlite"
    down = FakePlatform(online=False)
    before = EventQueue(PlatformTransport(down), store=OutboxStore(path))
    queue_an_outage(before, ["CAR-A", "CAR-B", "CAR-C"], close="CAR-A")
    assert before.flush() == 0

    platform = FakePlatform()
    after = EventQueue(PlatformTransport(platform), store=OutboxStore(path))
    held = after._queue
    assert [(e.kind, e.detail["plate"]) for e in held] == [
        ("session_open", "CAR-A"), ("session_open", "CAR-B"), ("session_open", "CAR-C"),
        ("session_close", "CAR-A"),
    ]
    assert [e.event_id for e in held] == [e.event_id for e in before._queue], (
        "the replay must carry the ids the platform deduplicates on"
    )
    assert after.flush() == 4
    assert platform.unique_opens == 3 and len(platform.closed) == 1
    assert after.pending == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_serve_keeps_the_outbox_beside_the_cache_and_says_so(tmp_path):
    from lane_controller import cli

    directory = a_narrow_directory(tmp_path)
    config = LaneConfig(
        lane_id="lane-entry", site_id="site-1",
        camera=CameraConfig(camera_id="cam", rtsp_url="", frames_per_read=1),
        gate=GateConfig(), cache_path=str(directory / "cache.sqlite"),
    )
    platform = FakePlatform(online=False)
    platform.base_url = "https://platform.test"
    first = cli.wire_lane(config, platform=platform)
    outbox = directory.resolve() / "cache.outbox.sqlite"
    assert first.seams["outbox"].startswith(f"DURABLE {outbox} (0 ")
    queue_an_outage(first.controller.events, ["SERVE-1", "SERVE-2"])
    assert first.controller.events.flush() == 0

    # The restart: a new process, the same configuration, the platform back.
    platform.online = True
    second = cli.wire_lane(config, platform=platform)
    assert second.controller.events.pending_sessions == 2
    assert second.seams["outbox"].startswith(f"DURABLE {outbox} (2 ")
    assert second.controller.events.flush() == 2
    assert platform.unique_opens == 2


def test_a_lane_without_a_cache_path_keeps_its_outbox_in_memory_and_says_so():
    """THE CONTROL: no path, no file, and the seam table says what a restart costs."""
    from lane_controller import cli

    config = lane_config()
    platform = FakePlatform()
    platform.base_url = "https://platform.test"
    wired = cli.wire_lane(config, platform=platform)
    assert wired.controller.events._store is None
    assert wired.seams["outbox"].startswith("MEMORY ONLY")
    assert "outbox" not in cli.wire_lane(config, platform=None).seams, (
        "a standalone lane has no outbox to drain"
    )


def test_a_stay_the_disk_cannot_take_stays_queued_in_memory(tmp_path, caplog):
    class FullDisk(OutboxStore):
        def add(self, event):
            raise OSError(28, "No space left on device")

    platform = FakePlatform(online=False)
    queue = EventQueue(
        PlatformTransport(platform),
        store=FullDisk(a_narrow_directory(tmp_path) / "lane.outbox.sqlite"),
    )
    with caplog.at_level(logging.ERROR):
        queue_an_outage(queue, ["DISK-1"])
    assert queue.pending_sessions == 1, "a failed write must not cost the record"
    assert any("outbox file" in r.getMessage() for r in caplog.records)
    platform.online = True
    assert queue.flush() == 1 and platform.unique_opens == 1


# ---------------------------------------------------------------------------
# A failed send resumes where it stopped.
# ---------------------------------------------------------------------------


class PoorLink(FakePlatform):
    """Every `every`-th call does not get through, as on a lossy link."""

    def __init__(self, every: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self.every = every
        self.calls = 0

    def _check(self):
        super()._check()
        self.calls += 1
        if self.calls % self.every == 0:
            raise PlatformUnreachable("simulated loss")


def test_on_a_link_that_loses_every_third_call_the_backlog_still_empties():
    """Ten stays queued. Each flush gets two calls through before one is lost.
    All or nothing, that flush kept all ten -- the two the platform took
    included -- and every later flush did the same, for ever. Resumed, two
    leave per flush, and each is delivered once."""
    platform = PoorLink(every=3, online=False)
    queue = EventQueue(PlatformTransport(platform))
    queue_an_outage(queue, [f"POOR-{n}" for n in range(10)])
    platform.online = True

    for _ in range(10):
        queue.flush()
    assert queue.pending == 0, f"{queue.pending} item(s) never got through"
    assert platform.unique_opens == 10
    assert platform.open_deliveries == 10, "a stay the platform took was sent again"


def test_a_refused_item_is_counted_once_however_often_the_flush_is_retried():
    """A close with no stay is refused and dropped; the open behind it cannot
    be delivered yet. The refusal was counted again on every retry."""
    platform = FakePlatform()
    transport = PlatformTransport(platform)
    queue = EventQueue(transport)
    queue.record("session_close", "lane-exit", plate="NO-STAY",
                 at="2026-10-16T09:00:00+00:00", exit_confirmation="confirmed", session_id=None)
    queue.record("session_open", "lane-exit", plate="LATER-1",
                 at="2026-10-16T09:01:00+00:00", entry_confirmation="confirmed")

    def unreachable(**kwargs):
        raise PlatformUnreachable("simulated outage, opens only")

    real_open = platform.open_session
    platform.open_session = unreachable
    for _ in range(3):
        assert queue.flush() <= 1
    assert transport.rejected == 1, f"one refusal counted {transport.rejected} times"
    platform.open_session = real_open
    assert queue.flush() == 1
    assert transport.rejected == 1 and platform.unique_opens == 1 and queue.pending == 0


def test_a_close_waits_behind_its_open_when_the_opens_call_is_lost():
    """The flush stops at the first part that does not get through and keeps
    everything behind it, in order. Two things rest on that: the platform
    receives a lane's events in the order the lane recorded them, and
    `cli._last_send` promises one client timeout on stop, not one per queued
    item. All or nothing kept the order by construction; part by part, stopping
    at the first failure is what keeps it.

    In production an entry lane queues only opens and an exit lane only closes,
    so one outbox never holds both halves of a stay. This test puts both in one
    queue only because a close sent ahead of its lost open makes a reordering
    visible: the platform refuses a close with no stay."""
    platform = FakePlatform()
    transport = PlatformTransport(platform)
    queue = EventQueue(transport)
    queue_an_outage(queue, ["ORDER-1"], close="ORDER-1")

    calls = []
    real_open, real_close = platform.open_session, platform.close_session

    def open_lost_once(**kwargs):
        if not calls:
            calls.append(("lost", kwargs["plate"]))
            raise PlatformUnreachable("simulated loss, the open's call only")
        calls.append(("open", kwargs["plate"]))
        return real_open(**kwargs)

    def close(**kwargs):
        calls.append(("close", kwargs["plate"]))
        return real_close(**kwargs)

    platform.open_session, platform.close_session = open_lost_once, close

    settled = queue.flush()
    assert calls == [("lost", "ORDER-1")], f"the close was sent past its lost open: {calls}"
    assert settled == 0 and queue.pending_sessions == 2, "the close left ahead of its open"
    assert transport.rejected == 0
    assert platform.closed == []

    assert queue.flush() == 2
    assert calls == [("lost", "ORDER-1"), ("open", "ORDER-1"), ("close", "ORDER-1")]
    assert transport.rejected == 0, "the close was refused: it reached the platform before its open"
    assert platform.unique_opens == 1
    assert [c["plate"] for c in platform.closed] == ["ORDER-1"]
    assert platform.open_sessions == {}, "the stay was left open"
    assert queue.pending == 0


# ---------------------------------------------------------------------------
# An outage's log events arrive in chunks the platform takes.
# ---------------------------------------------------------------------------

#: The platform's body limit: express.json({ limit: '1mb' }), 1 MiB.
PLATFORM_BODY_LIMIT = 1024 * 1024


class PlatformWithABodyLimit(FakePlatform):
    """Answers a log post whose body is over `limit` bytes 413, as the real
    platform's body parser does, and keeps the size of every post it took."""

    def __init__(self, limit: int = PLATFORM_BODY_LIMIT, **kwargs) -> None:
        super().__init__(**kwargs)
        self.limit = limit
        self.posts: list[tuple[int, int]] = []
        self.too_large = 0

    def post_events(self, events):
        self._check()
        size = len(json.dumps({"events": events}).encode("utf-8"))
        if size > self.limit:
            self.too_large += 1
            raise PlatformRejected(413, '{"error": "The request body is too large.", '
                                        '"code": "body_too_large"}', "body_too_large")
        self.posts.append((len(events), size))
        return super().post_events(events)


def an_outages_log(queue, count, *, padding=300):
    for n in range(count):
        queue.record("arming_rejected", "lane-entry", reason="x" * padding, n=n)


def test_an_outages_worth_of_log_events_arrives_in_chunks_the_platform_takes():
    """6,000 log events, about 2 MB: one request was refused 413 and the lane
    dropped the whole backlog. Now every post is at most `LOG_CHUNK` events
    and `LOG_CHUNK_BYTES`, and all 6,000 arrive."""
    platform = PlatformWithABodyLimit(online=False)
    transport = PlatformTransport(platform)
    queue = EventQueue(transport)
    an_outages_log(queue, 6_000)
    assert len(json.dumps([e.as_dict() for e in queue._queue])) > 2 * PLATFORM_BODY_LIMIT
    platform.online = True

    assert queue.flush() == 6_000
    assert platform.unique_events == 6_000
    assert transport.rejected == 0 and platform.too_large == 0
    assert queue.pending == 0
    assert len(platform.posts) >= 6_000 // LOG_CHUNK
    assert max(n for n, _ in platform.posts) <= LOG_CHUNK
    assert max(size for _, size in platform.posts) <= LOG_CHUNK_BYTES


def test_a_chunk_over_the_byte_cap_goes_in_halves_before_it_is_sent():
    """200 events of 2 KB each: a 400 KB chunk the platform would take, but
    over `LOG_CHUNK_BYTES`. It goes in halves without a 413 round trip."""
    platform = PlatformWithABodyLimit()
    transport = PlatformTransport(platform)
    queue = EventQueue(transport)
    an_outages_log(queue, 200, padding=2_000)

    assert queue.flush() == 200
    assert platform.unique_events == 200 and transport.rejected == 0
    assert platform.too_large == 0
    assert len(platform.posts) > 1
    assert max(size for _, size in platform.posts) <= LOG_CHUNK_BYTES


def test_a_chunk_the_platform_answers_413_is_posted_again_in_halves():
    """A platform whose limit is below one chunk: the 413 is not a refusal of
    the events, so the halves go and none is lost or counted."""
    platform = PlatformWithABodyLimit(limit=20_000)
    transport = PlatformTransport(platform)
    queue = EventQueue(transport)
    an_outages_log(queue, 200)

    assert queue.flush() == 200
    assert platform.too_large > 0, "control: the chunk really was too large"
    assert platform.unique_events == 200
    assert transport.rejected == 0
    assert queue.pending == 0


def test_one_event_too_large_to_post_alone_is_the_only_one_lost(caplog):
    platform = PlatformWithABodyLimit(limit=20_000)
    transport = PlatformTransport(platform)
    queue = EventQueue(transport)
    an_outages_log(queue, 100)
    queue.record("arming_rejected", "lane-entry", reason="y" * 30_000)
    an_outages_log(queue, 99)

    with caplog.at_level(logging.ERROR):
        assert queue.flush() == 200
    assert platform.unique_events == 199
    assert transport.rejected == 1
    assert any("413" in r.getMessage() for r in caplog.records), "the drop must name the status"


def test_a_chunk_refused_for_its_content_is_one_lost_chunk_and_the_rest_arrive():
    """As the design says of a batch: a 400 drops that chunk, counted once.
    The other chunks of the backlog are not taken down with it."""
    platform = FakePlatform()
    platform.reject_event_kinds = {"totally_invented_kind"}
    transport = PlatformTransport(platform)
    queue = EventQueue(transport)
    an_outages_log(queue, 450, padding=10)
    queue.record("totally_invented_kind", "lane-entry")
    an_outages_log(queue, 549, padding=10)

    assert queue.flush() == 1_000
    assert transport.rejected == 1
    assert platform.unique_events == 1_000 - LOG_CHUNK
    assert queue.pending == 0


# ---------------------------------------------------------------------------
# `serve` makes one last send as it stops, and SIGTERM stops it.
# ---------------------------------------------------------------------------


def _serve_with(tmp_path, monkeypatch, serve_forever):
    """`lane-controller serve` against a fake platform, the HTTP server
    replaced by one whose `serve_forever` is `serve_forever(controller)`. The
    drain thread's retry is a minute, so nothing it sends is mistaken for the
    last send."""
    from lane_controller import cli
    from lane_controller import runner as runner_module

    example = Path(__file__).resolve().parent.parent / "config" / "lane.example.toml"
    config = tmp_path / "lane.toml"
    config.write_text(example.read_text().replace(
        'server_url = "https://platform.example/api/lane-events"',
        'server_url = "http://127.0.0.1:9"'))
    platform = FakePlatform()
    platform.base_url = "http://127.0.0.1:9"
    monkeypatch.setattr(cli, "platform_client_for", lambda config, token_file: platform)
    monkeypatch.setattr(runner_module, "DRAIN_RETRY_S", 60.0)

    class FakeServer:
        def __init__(self, service):
            self.service = service

        def serve_forever(self):
            serve_forever(self.service.controller)

        def server_close(self):
            return None

    monkeypatch.setattr(cli, "make_server", lambda service, **kwargs: FakeServer(service))
    code = cli.main(["serve", "--config", str(config)])
    return code, platform


def test_serve_makes_one_last_send_as_it_stops(tmp_path, monkeypatch):
    def a_stay_then_ctrl_c(controller):
        controller.events.record("session_open", controller.config.lane_id, plate="LAST-1",
                                 at="2026-10-16T09:00:00+00:00", entry_confirmation="confirmed")
        raise KeyboardInterrupt

    code, platform = _serve_with(tmp_path, monkeypatch, a_stay_then_ctrl_c)
    assert code == 0
    assert platform.unique_opens == 1, "the stay recorded before the stop never left the box"


def test_sigterm_stops_serve_the_way_ctrl_c_does_and_its_handler_is_put_back(
    tmp_path, monkeypatch
):
    before = signal.getsignal(signal.SIGTERM)
    seen = []

    def a_stay_then_sigterm(controller):
        controller.events.record("session_open", controller.config.lane_id, plate="TERM-1",
                                 at="2026-10-16T09:00:00+00:00", entry_confirmation="confirmed")
        handler = signal.getsignal(signal.SIGTERM)
        seen.append(handler)
        if handler is not before and callable(handler):
            handler(signal.SIGTERM, None)
        seen.append("served on")

    code, platform = _serve_with(tmp_path, monkeypatch, a_stay_then_sigterm)
    assert code == 0
    assert seen[0] is not before, "serve left SIGTERM's default: the process ends on the spot"
    assert seen[1:] == [], "SIGTERM did not stop serve"
    assert platform.unique_opens == 1
    assert signal.getsignal(signal.SIGTERM) is before, "serve did not put the handler back"
