"""The entry, as the owner set it (2026-10-26).

  "IF transients are accepted to the garage there is no one single reason not
  to open. We take the picture, license plate, color, damage, shape and open
  the gate. ... IF only monthly, garage pass location ... Previously
  registered ones, we open the gate."

  "We do not care about the barrier. We have activation loop and closing loop."

Each guarantee below is paired with the case that falsifies it, and
`scripts/entry_stay_fail_control.py` breaks the lane once per guarantee and
requires this file to go red:

  1  every car through the activation loop and the closing loop is a stay,
     with or without the open command: a refused car that drove in, the second
     car through one opening, a car nobody could read -- each opens a stay,
     `confirmed` (the loops saw it) and `tailgater: true` (no open command let
     it in), carrying a plate or `plate_unreadable`, its pictures with it, and
     kept on disk through the outbox like any stay. An open command with
     nothing crossing in time is still no stay. A lane with no closing loop is
     unchanged: every stay `unconfirmable`
  2  the arming read's frames are kept with the stay, besides the pictures
     between the vend and the closing loop and at it; with an outbox file the
     pictures are on disk (0600 in a 0700 directory), survive a restart in an
     outage, are not capped, and go one at a time, each retried until it is
     taken
  3  a garage that takes transients opens for every car: low confidence, no
     plate read, an unknown vehicle, an engine that did not answer, rules past
     their age -- each opens, and the stay carries what was seen. Only full
     (a register-covered car still gets in), closed to everyone, a banned car
     and an owner's rule against a plate keep the gate shut; nothing present
     is still no open. The garage's entry settings outlive the cache's bound
  4  a monthly/pass-only garage opens for a car its registers cover today and
     DENIES every other car -- not a person's case -- whatever its default
  5  no disk on the car's path: a hung disk does not hold the next car, and
     what was recorded reaches the disk when the disk answers
"""

from __future__ import annotations

import stat
import threading
import time

import pytest

from fake_platform import FakePlatform
from lane_controller import (
    CameraConfig,
    DecisionCache,
    GateConfig,
    LaneConfig,
    LaneController,
    LoopConfig,
    VehicleIdentity,
)
from lane_controller.decision import (
    REASON_NOT_REGISTERED,
    Fallback,
    Outcome,
    Rule,
)
from lane_controller.durable import CACHE_DIR_MODE, DurableStore, OutboxStore
from lane_controller.events import EventQueue, Picture
from lane_controller.interfaces import ClosingSequence, Frame, Unavailable
from lane_controller.platform_client import PlatformClient, PlatformRejected
from lane_controller.simulated import (
    CannedCameraFeed,
    ExactBannedMatcher,
    RecordingVendOutput,
    ScriptedClosingLoops,
    SimulatedLoopInput,
    StubVehicleIdentifier,
)
from lane_controller.sync import (
    CONFIRMED,
    ENTRY_UNADMITTED,
    REASON_BANNED,
    SESSION_OPEN,
    UNCONFIRMABLE,
    PlatformTransport,
)
from test_exit_decision import NEW_YORK, NOW, gp_register, gp_row

OCC = ClosingSequence.OCCUPIED
FWD = ClosingSequence.FORWARD
WINDOW = 10.0
THRESHOLD = 0.85

HOLDER = "PASS-777"
STRANGER = "CAR-123"
SEEN = VehicleIdentity(plate=STRANGER, confidence=0.97, presence=True,
                       descriptor="opvid-opa-id/2:a")
HOLDER_SEEN = VehicleIdentity(plate=HOLDER, confidence=0.97, presence=True)
#: The holder's own plate, read below the lane's confidence: it must never be
#: matched to the holder's pass, nor become the stay's plate.
HOLDER_UNSURE = VehicleIdentity(plate=HOLDER, confidence=0.42, presence=True)
LOW = VehicleIdentity(plate=STRANGER, confidence=0.42, presence=True,
                      descriptor="opvid-opa-id/2:b")
NO_PLATE = VehicleIdentity(plate=None, confidence=0.97, presence=True,
                           descriptor="opvid-opa-id/2:c")
ENGINE_DOWN = VehicleIdentity(plate=None, confidence=0.0, unavailable=Unavailable.UNREACHABLE)
NOBODY = VehicleIdentity(plate=None, confidence=0.0, presence=False)
TAILGATER = VehicleIdentity(plate="TAIL-1", confidence=0.97, presence=True)
UNREAD = VehicleIdentity(plate=None, confidence=0.0)

FULL = {"state": "closed", "reason": "full", "message": "Full.", "closed_at": None}
EVERYONE = {"state": "closed", "reason": "everyone", "message": "Closed.", "closed_at": None}
BANNED_DESCRIPTOR = "opvid-opa-id/2:banned"


def narrow(tmp_path):
    directory = tmp_path / "lane-state"
    directory.mkdir(mode=CACHE_DIR_MODE)
    directory.chmod(CACHE_DIR_MODE)
    return directory


def a_lane(
    identities,
    *,
    crossings=((OCC, 3.0),),
    closing_loops=1,
    transient=True,
    default_action="allow",
    register=None,
    closing=None,
    rules=(),
    banned=(),
    stale=False,
    store=None,
    platform=None,
    images=None,
    frames_per_read=2,
    arrivals=1,
    cache=None,
):
    """An entry lane at a garage that has said what it takes, wired to the
    simulated seams and to a FakePlatform, on the lane's clock at NOW."""
    config = LaneConfig(
        lane_id="lane-entry",
        site_id="site-1",
        camera=CameraConfig(camera_id="sim-cam-1", rtsp_url="", frames_per_read=frames_per_read),
        gate=GateConfig(),
        direction="entry",
        confidence_threshold=THRESHOLD,
        loops=LoopConfig(
            arming_loops=1,
            closing_loops=closing_loops,
            closing_spacing_m=0.0 if closing_loops == 1 else 1.5,
            confirmation_window_seconds=WINDOW,
        ),
    )
    if cache is None:
        cache = DecisionCache(max_age_seconds=3600)
        cache.load(list(rules), default_action=default_action,
                   now=time.time() - (7200 if stale else 0))
        cache.transient_available = transient
        cache.timezone = NEW_YORK
        if register is not None:
            cache.entitlements["garage_pass"] = {"consulted": True, "register": register}
            cache._reindex_coverage()
        if closing is not None:
            cache._take_lane({"lane": closing})
        cache.banned = list(banned)
    platform = platform if platform is not None else FakePlatform()
    vend = RecordingVendOutput()
    controller = LaneController(
        config,
        loop=SimulatedLoopInput(arrivals=arrivals),
        camera=CannedCameraFeed(images=images),
        vend=vend,
        identifier=StubVehicleIdentifier(list(identities)),
        closing_loops=ScriptedClosingLoops(list(crossings)) if closing_loops else None,
        cache=cache,
        events=EventQueue(PlatformTransport(platform), store=store),
        banned_matcher=ExactBannedMatcher(),
        clock=lambda: NOW,
    )
    return controller, vend, platform


def a_register():
    return gp_register(gp_row(HOLDER))


def pictures_of(platform, event_id):
    return [p for p in platform.pictures.values() if p["event_id"] == event_id]


def kinds(controller):
    return [e.kind for e in controller.events._queue]


# ---------------------------------------------------------------------------
# 1 -- every car through both loops is a stay
# ---------------------------------------------------------------------------


def test_a_refused_car_that_drove_in_is_a_stay_flagged_as_no_open_command_let_it_in():
    controller, vend, platform = a_lane(
        [SEEN, TAILGATER], rules=[Rule(plate=STRANGER, allow=False)]
    )
    assert controller.run_once().outcome is Outcome.DENY
    assert vend.vend_count == 0, "the premise: no open command"
    assert controller.run_once() is None, "an idle turn: the car crosses the closing loop"
    [opened] = platform.opened
    assert (opened["plate"], opened["entry_confirmation"], opened["tailgater"]) == (
        "TAIL-1", CONFIRMED, True)
    assert len(pictures_of(platform, opened["event_id"])) == 2, "the closing-loop pictures"


@pytest.mark.parametrize("crossing", [UNREAD, LOW, ENGINE_DOWN],
                         ids=["nothing read", "below the confidence", "engine down"])
def test_a_car_nobody_could_read_through_the_gate_is_a_stay_with_an_unreadable_plate(crossing):
    """The plate unreadable is a value of the car's identity, never a reason
    to leave the car out of the record -- and a read below the lane's
    confidence is unreadable, never the stay's plate."""
    controller, _, platform = a_lane([SEEN, crossing], rules=[Rule(plate=STRANGER, allow=False)])
    controller.run_once()
    controller.run_once()
    [opened] = platform.opened
    assert opened["plate"] is None and opened["plate_unreadable"] is True
    assert opened["tailgater"] is True and opened["entry_confirmation"] == CONFIRMED
    assert opened["descriptor"] == crossing.descriptor, "what was seen goes with it"
    assert len(pictures_of(platform, opened["event_id"])) == 2


def test_the_second_car_through_one_opening_gets_its_own_stay():
    """The vend lets one car in; the car behind it follows through the same
    opening. Two stays: the first the vend's, the second its own, identified
    from the closing-loop pictures."""
    controller, vend, platform = a_lane(
        [SEEN, TAILGATER], crossings=[(FWD, 3.0), (FWD, 3.0)], closing_loops=2
    )
    assert controller.run_once().outcome is Outcome.ALLOW
    assert vend.vend_count == 1
    assert controller.run_once() is None
    first, second = platform.opened
    assert (first["plate"], first["tailgater"]) == (STRANGER, False)
    assert (second["plate"], second["tailgater"]) == ("TAIL-1", True)
    assert vend.vend_count == 1, "one opening"


def test_an_open_command_with_nothing_crossing_in_time_is_no_stay():
    controller, vend, platform = a_lane([SEEN], crossings=[])
    controller.run_once()
    assert vend.vend_count == 1
    assert platform.opened == [], "held: the car did not pass both loops"
    assert controller.transit_state == "held"


def test_a_lane_with_no_closing_loop_is_unchanged_every_stay_unconfirmable():
    controller, _, platform = a_lane([LOW], closing_loops=0, crossings=())
    assert controller.run_once().outcome is Outcome.ALLOW
    [opened] = platform.opened
    assert opened["entry_confirmation"] == UNCONFIRMABLE
    assert opened["plate_unreadable"] is True


def test_the_admitted_car_crossing_late_is_not_a_tailgater_and_only_once():
    """Held, then a car crosses: nothing can tell an unread car from the one
    let in, so it is taken for that car -- never put on a banned list for
    being slow. A second crossing after the same held vend is not that car."""
    controller, _, platform = a_lane(
        [LOW, UNREAD, UNREAD], crossings=[(OCC, WINDOW + 1.0), (OCC, 3.0), (OCC, 3.0)]
    )
    controller.run_once()
    assert platform.opened == [], "held"
    controller.run_once()
    controller.run_once()
    late, follower = platform.opened
    assert late["tailgater"] is False
    assert follower["tailgater"] is True


def test_a_stay_with_no_open_command_survives_a_restart_in_an_outage(tmp_path):
    """It is a session action: kept on disk through the outbox until the
    platform has it, exactly as a confirmed entry is."""
    path = narrow(tmp_path) / "lane.outbox.sqlite"
    platform = FakePlatform(online=False)
    controller, _, _ = a_lane(
        [SEEN, UNREAD], rules=[Rule(plate=STRANGER, allow=False)],
        store=OutboxStore(path), platform=platform,
    )
    controller.run_once()
    controller.run_once()
    assert platform.opened == []
    assert controller.events.wait_written(5)
    # The restart: a new outbox on the same file, the platform back.
    platform.online = True
    after = EventQueue(PlatformTransport(platform), store=OutboxStore(path))
    assert after.pending_sessions == 1
    assert after.flush() == 1
    [opened] = platform.opened
    assert (opened["plate_unreadable"], opened["tailgater"]) == (True, True)
    assert after.flush_pictures() == 2, "and its pictures, from the same file"


def test_the_open_carries_plate_unreadable_on_the_wire_and_nothing_else():
    sent = {}
    client = PlatformClient("https://platform.test", "token")
    client._request = lambda method, path, body=None: sent.update(body) or {
        "session": {"entry_confirmation": CONFIRMED}}
    client.open_session(event_id="e-1", entry_at="2026-10-27T10:00:00+00:00",
                        entry_confirmation=CONFIRMED, plate_unreadable=True)
    assert sent["plate_unreadable"] is True
    assert "plate" not in sent and "ticket_ref" not in sent
    with pytest.raises(PlatformRejected):
        client.open_session(event_id="e-2", entry_at="2026-10-27T10:00:00+00:00",
                            entry_confirmation=CONFIRMED, plate="X1", plate_unreadable=True)


# ---------------------------------------------------------------------------
# 2 -- the pictures: the arming read kept; on disk, not capped, one at a time
# ---------------------------------------------------------------------------

#: One distinct picture per grab, in order: the arming read takes the first
#: `frames_per_read`, the vend's between-pictures the next two, the closing loop
#: the two after.
IMAGES = [b"\x89PNG\r\n\x1a\n" + bytes([n]) * 8 for n in range(1, 12)]


def test_the_arming_reads_frames_are_kept_with_the_stay():
    controller, _, platform = a_lane([SEEN], images=IMAGES, frames_per_read=3)
    controller.run_once()
    [opened] = platform.opened
    kept = {p["image"] for p in pictures_of(platform, opened["event_id"])}
    assert set(IMAGES[:3]) <= kept, "the frames the car was identified from were thrown away"
    assert kept == set(IMAGES[:7]), "arming, between, and at the closing loop"


def test_a_lane_with_no_closing_loop_keeps_the_arming_read_as_the_stays_pictures():
    controller, _, platform = a_lane([SEEN], closing_loops=0, crossings=(), images=IMAGES)
    controller.run_once()
    [opened] = platform.opened
    assert {p["image"] for p in pictures_of(platform, opened["event_id"])} == set(IMAGES[:2])


def pictures(n, start=0):
    return [Frame(image_bytes=b"\x89PNG" + str(i).encode(), captured_at=1.0 + i, camera_id="c")
            for i in range(start, start + n)]


def test_pictures_are_kept_on_disk_narrow(tmp_path):
    directory = narrow(tmp_path)
    path = directory / "lane.outbox.sqlite"
    store = OutboxStore(path)
    queue = EventQueue(PlatformTransport(FakePlatform(online=False)), store=store)
    assert queue.queue_pictures("e-1", pictures(3)) == 3
    assert queue.wait_written(5)
    assert store.picture_count() == 3
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_more_pictures_than_the_old_cap_survive_a_restart_and_all_go(tmp_path):
    """250 pictures in an outage -- the old queue held 200 in memory and lost
    them all on a restart. Every one is kept and every one goes."""
    path = narrow(tmp_path) / "lane.outbox.sqlite"
    down = FakePlatform(online=False)
    before = EventQueue(PlatformTransport(down), store=OutboxStore(path))
    for car in range(50):  # five pictures a car, the cars a few seconds apart
        before.queue_pictures(f"e-{car}", pictures(5, start=5 * car))
        assert before.wait_written(5)
    assert before.flush_pictures() == 0
    assert before.pictures_dropped == 0
    platform = FakePlatform()
    after = EventQueue(PlatformTransport(platform), store=OutboxStore(path))
    assert after.pending_pictures == 250
    assert after.flush_pictures() == 250
    assert len(platform.pictures) == 250
    assert after.pending_pictures == 0


class FlakyPictures(FakePlatform):
    """Every third picture post is lost on the way: a poor link."""

    def __init__(self):
        super().__init__()
        self.posts = 0
        self.taken: dict[str, int] = {}

    def post_picture(self, *, picture_ref, **kwargs):
        self.posts += 1
        if self.posts % 3 == 0:
            from lane_controller.platform_client import PlatformUnreachable

            raise PlatformUnreachable("lost on the way")
        self.taken[picture_ref] = self.taken.get(picture_ref, 0) + 1
        return super().post_picture(picture_ref=picture_ref, **kwargs)


@pytest.mark.parametrize("on_disk", [True, False], ids=["outbox file", "memory only"])
def test_pictures_go_one_at_a_time_and_a_taken_one_is_never_sent_again(tmp_path, on_disk):
    platform = FlakyPictures()
    store = OutboxStore(narrow(tmp_path) / "lane.outbox.sqlite") if on_disk else None
    queue = EventQueue(PlatformTransport(platform), store=store)
    queue.queue_pictures("e-1", pictures(10))
    for _ in range(20):
        queue.flush_pictures()
        if queue.pending_pictures == 0:
            break
    assert len(platform.pictures) == 10
    assert set(platform.taken.values()) == {1}, "a picture already taken was sent again"


def test_a_picture_the_disk_cannot_take_is_sent_from_memory(tmp_path):
    class FullDisk(OutboxStore):
        def add_picture(self, picture):
            raise OSError(28, "No space left on device")

    platform = FakePlatform()
    queue = EventQueue(PlatformTransport(platform),
                       store=FullDisk(narrow(tmp_path) / "lane.outbox.sqlite"))
    queue.queue_pictures("e-1", pictures(2))
    assert queue.flush_pictures() == 2
    assert len(platform.pictures) == 2


# ---------------------------------------------------------------------------
# 3 -- a garage that takes transients opens for every car
# ---------------------------------------------------------------------------

CASES = {
    "low confidence": (LOW, None),
    "no plate read": (NO_PLATE, None),
    "unknown vehicle": (SEEN, STRANGER),
    "engine unreachable": (ENGINE_DOWN, None),
}


@pytest.mark.parametrize("case", list(CASES))
def test_a_garage_that_takes_transients_opens_for_every_car_and_records_what_was_seen(case):
    identity, plate = CASES[case]
    controller, vend, platform = a_lane([identity], default_action=None)
    decision = controller.run_once()
    assert decision.outcome is Outcome.ALLOW, case
    assert vend.vend_count == 1, case
    [opened] = platform.opened
    assert opened["plate"] == plate, case
    assert opened["plate_unreadable"] is (plate is None), case
    assert opened["entry_confirmation"] == CONFIRMED and opened["tailgater"] is False
    assert not [e for e in platform.events.values() if e["kind"] == "fallback_needs_human"], (
        "a car let in is nobody's case")


def test_rules_past_their_age_open_too():
    controller, vend, _ = a_lane([SEEN], stale=True)
    assert controller.cache.is_stale(), "the premise"
    assert controller.run_once().outcome is Outcome.ALLOW
    assert vend.vend_count == 1


def test_a_car_let_in_with_no_read_says_which_failure():
    controller, _, platform = a_lane([ENGINE_DOWN])
    controller.run_once()
    [decided] = [e for e in platform.events.values() if e["kind"] == "decision"]
    assert decided["detail"]["cause"] == Unavailable.UNREACHABLE.value
    assert decided["detail"]["outcome"] == "allow"


@pytest.mark.parametrize("case", list(CASES))
def test_the_control_a_garage_that_has_not_said_decides_as_before(case):
    identity, _ = CASES[case]
    controller, vend, _ = a_lane([identity], transient=None, default_action=None)
    assert controller.run_once().outcome is Outcome.FALLBACK, case
    assert vend.vend_count == 0, case


def test_nothing_present_is_still_no_open():
    controller, vend, platform = a_lane([NOBODY])
    assert controller.run_once().outcome is Outcome.NO_VEHICLE
    assert vend.vend_count == 0 and platform.opened == []


def test_full_keeps_the_gate_shut_but_for_a_car_a_register_covers():
    for identity, opens in ((HOLDER_SEEN, True), (SEEN, False), (NO_PLATE, False),
                            (HOLDER_UNSURE, False)):
        controller, vend, _ = a_lane([identity], closing=FULL, register=a_register())
        decision = controller.run_once()
        assert decision.should_vend is opens, identity
        assert vend.vend_count == int(opens), identity
        if not opens:
            assert decision.fallback is Fallback.LANE_CLOSED, identity


def test_closed_to_everyone_keeps_it_shut():
    controller, vend, _ = a_lane([SEEN], closing=EVERYONE)
    assert controller.run_once().fallback is Fallback.LANE_CLOSED
    assert vend.vend_count == 0


def test_a_banned_car_and_an_owners_rule_against_a_plate_keep_it_shut():
    banned = VehicleIdentity(plate="B-1", confidence=0.97, presence=True,
                             descriptor=BANNED_DESCRIPTOR)
    controller, vend, _ = a_lane([banned], banned=[{"id": "ban-1",
                                                   "descriptor": BANNED_DESCRIPTOR}])
    decision = controller.run_once()
    assert (decision.outcome, decision.reason) == (Outcome.DENY, REASON_BANNED)
    assert vend.vend_count == 0
    controller, vend, _ = a_lane([SEEN], rules=[Rule(plate=STRANGER, allow=False)])
    assert controller.run_once().outcome is Outcome.DENY
    assert vend.vend_count == 0


def test_the_garages_entry_settings_outlive_the_caches_bound(tmp_path):
    """Two days into an outage the cache's personal data is wiped -- and a
    garage that takes transients still opens for every car, in memory and
    after a restart. Wiped with the rest, every car went to a person."""
    path = narrow(tmp_path) / "cache.sqlite"
    cache = DecisionCache(max_age_seconds=3600, store=DurableStore(path))
    cache.load_payload({"default_action": "allow", "transient_available": True},
                       now=time.time() - 7200)
    assert cache.expire_if_past_bound() is True
    assert cache.transient_available is True
    controller, vend, _ = a_lane([LOW], cache=cache)
    assert controller.run_once().outcome is Outcome.ALLOW
    restarted = DecisionCache(max_age_seconds=3600, store=DurableStore(path))
    assert restarted.transient_available is True and restarted.default_action == "allow"
    controller, vend, _ = a_lane([LOW], cache=restarted)
    assert controller.run_once().outcome is Outcome.ALLOW and vend.vend_count == 1


# ---------------------------------------------------------------------------
# 4 -- a monthly/pass-only garage: covered opens, every other car is denied
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("default_action", ["allow", "deny", None])
def test_a_pass_only_garage_opens_for_a_covered_car_and_denies_every_other(default_action):
    holder, vend, _ = a_lane([HOLDER_SEEN], transient=False, default_action=default_action,
                             register=a_register())
    assert holder.run_once().outcome is Outcome.ALLOW
    assert vend.vend_count == 1
    for other in (SEEN, LOW, NO_PLATE, ENGINE_DOWN, HOLDER_UNSURE):
        controller, vend, platform = a_lane([other], transient=False,
                                            default_action=default_action, register=a_register())
        decision = controller.run_once()
        assert (decision.outcome, decision.reason) == (Outcome.DENY, REASON_NOT_REGISTERED), other
        assert decision.fallback is None, "not handed to a person"
        assert vend.vend_count == 0 and platform.opened == [], other


def test_a_garage_that_takes_transients_but_denies_strangers_is_read_as_pass_only():
    """An operator's default of `deny` says strangers are not let in: read as
    pass-only, the stricter of the two, so a covered car still opens."""
    holder, vend, _ = a_lane([HOLDER_SEEN], transient=True, default_action="deny",
                             register=a_register())
    assert holder.run_once().outcome is Outcome.ALLOW
    stranger, vend, _ = a_lane([SEEN], transient=True, default_action="deny",
                               register=a_register())
    assert stranger.run_once().outcome is Outcome.DENY and vend.vend_count == 0


# ---------------------------------------------------------------------------
# 5 -- no disk on the car's path
# ---------------------------------------------------------------------------


class HungDisk(OutboxStore):
    """A disk that does not answer until it is released -- bounded, so a lane
    that waits on it shows up as a slow test, not a hung one."""

    HANG_S = 1.5

    def __init__(self, path):
        super().__init__(path)
        self.release = threading.Event()

    def add(self, event):
        self.release.wait(self.HANG_S)
        super().add(event)

    def add_picture(self, picture):
        self.release.wait(self.HANG_S)
        super().add_picture(picture)


def test_a_hung_disk_does_not_hold_the_next_car(tmp_path):
    store = HungDisk(narrow(tmp_path) / "lane.outbox.sqlite")
    controller, vend, platform = a_lane(
        [SEEN, SEEN], crossings=[(OCC, 3.0), (OCC, 3.0)], arrivals=2, store=store,
        platform=FakePlatform(online=False),
    )
    # The runner's shape: the lane thread hands the outbox to a drain of its
    # own and never flushes it itself.
    controller.set_drain(lambda: None)
    started = time.monotonic()
    controller.run_once()
    controller.run_once()
    took = time.monotonic() - started
    assert vend.vend_count == 2
    assert took < 1.0, f"two cars waited {took:.2f}s on the disk"
    # The disk answers: what was recorded reaches it.
    store.release.set()
    assert controller.events.wait_written(10)
    assert [e.kind for e in store.held()] == [SESSION_OPEN, SESSION_OPEN]
    assert store.picture_count() > 0


def test_the_crossing_with_no_open_command_waits_on_no_disk_either(tmp_path):
    store = HungDisk(narrow(tmp_path) / "lane.outbox.sqlite")
    controller, _, _ = a_lane([SEEN, UNREAD], rules=[Rule(plate=STRANGER, allow=False)],
                              store=store, platform=FakePlatform(online=False))
    controller.set_drain(lambda: None)
    controller.run_once()
    started = time.monotonic()
    assert controller.observe_closing_loops() == ENTRY_UNADMITTED
    assert time.monotonic() - started < 1.0
    store.release.set()
    assert controller.events.wait_written(10)
    assert [e.kind for e in store.held()] == [SESSION_OPEN]


def test_the_picture_record_is_the_platforms_shape():
    """The one picture shape the transport sends, whether it was read from
    disk or memory: the lane's ref, the record's event, the image."""
    picture = Picture(event_id="e-1", taken_at=1.0, image_bytes=b"\x89PNG")
    platform = FakePlatform()
    assert PlatformTransport(platform).send_pictures([picture])
    assert platform.pictures[picture.picture_ref]["event_id"] == "e-1"
