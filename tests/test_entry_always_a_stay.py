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

And what the independent review of this round asked for:

  B  "no open command let it in" is its own mark (`no_open_command`), and
     `tailgater` keeps its meaning -- a car no vend let in whose plate was
     READ; a car nobody could read is never a tailgater, so never ban-listed
  A  what was read below the confidence is kept on the stay (`unsure_plate`,
     with its confidence) and is never its plate
  C  at a pass-only garage, until the registration QR: a car whose pass cannot
     be confirmed -- plate unread or unsure, or no register held -- goes to a
     person, and a display code does not complete it; a car read with
     confidence that no register covers is denied, full or not
  4  a pass-only garage's registers outlive the cache's bound while the
     platform cannot be reached, and go when it answers again
  F  the pictures' share of the outbox file has a ceiling, the drops are
     counted on `GET /v1/lane/events`, and a removal that fails holds no flush
  E  an exit records its pending close as before; a closed exit stays closed
     past the bound (its gate was shut there before too); a stale owner's rule
     is never applied
  D  the arming read of a car a person lets in, or of a refused car that
     drives in anyway, is kept with its stay
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
from lane_controller.contract import VendAuthority, VendRefusal
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
from lane_controller.service import LaneService
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
    sync_rules,
)
from test_exit_decision import NEW_YORK, NOW, gp_register, gp_row
from test_vend import complete, settled

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
    clock=None,
    matcher="exact",
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
        banned_matcher=ExactBannedMatcher() if matcher == "exact" else matcher,
        clock=clock or (lambda: NOW),
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
    assert opened["no_open_command"] is True
    # The closing-loop pictures only: its plate says it is not the car refused.
    assert len(pictures_of(platform, opened["event_id"])) == 2


@pytest.mark.parametrize(("crossing", "arming"), [(UNREAD, 2), (LOW, 0), (ENGINE_DOWN, 2)],
                         ids=["nothing read", "below the confidence", "engine down"])
def test_a_car_nobody_could_read_through_the_gate_is_a_stay_with_an_unreadable_plate(
    crossing, arming
):
    """The plate unreadable is a value of the car's identity, never a reason
    to leave the car out of the record -- and a read below the lane's
    confidence is unreadable, never the stay's plate."""
    controller, _, platform = a_lane([SEEN, crossing], rules=[Rule(plate=STRANGER, allow=False)])
    controller.run_once()
    controller.run_once()
    [opened] = platform.opened
    assert opened["plate"] is None and opened["plate_unreadable"] is True
    assert opened["no_open_command"] is True and opened["entry_confirmation"] == CONFIRMED
    assert opened["tailgater"] is False, "a car nobody could read is never a tailgater"
    assert opened["descriptor"] == crossing.descriptor, "what was seen goes with it"
    # The closing loop's two, and the refused car's arming read (two frames)
    # when nothing says it is another car -- the read below the confidence
    # has a descriptor that does not match the refused car's, so not there.
    assert len(pictures_of(platform, opened["event_id"])) == 2 + arming


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
    assert (late["tailgater"], late["no_open_command"]) == (False, False), "its open command"
    assert follower["no_open_command"] is True
    assert follower["tailgater"] is False, "unread: never a tailgater"


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
    assert (opened["plate_unreadable"], opened["no_open_command"]) == (True, True)
    assert after.flush_pictures() == 4, "and its pictures, from the same file"


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
    stranger, vend, platform = a_lane([SEEN], transient=False, default_action=default_action,
                                      register=a_register())
    decision = stranger.run_once()
    assert (decision.outcome, decision.reason) == (Outcome.DENY, REASON_NOT_REGISTERED)
    assert decision.fallback is None, "a plate read with confidence that no register covers"
    assert vend.vend_count == 0 and platform.opened == []


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


# ---------------------------------------------------------------------------
# B -- its own mark; a tailgater is a car whose plate was read
# ---------------------------------------------------------------------------


def test_no_open_command_is_its_own_mark_and_an_admitted_car_carries_neither():
    controller, _, platform = a_lane([SEEN, UNREAD], crossings=[(OCC, 3.0), (OCC, 3.0)])
    assert controller.run_once().outcome is Outcome.ALLOW
    controller.run_once()
    admitted, unread = platform.opened
    assert (admitted["no_open_command"], admitted["tailgater"]) == (False, False)
    assert (unread["no_open_command"], unread["tailgater"]) == (True, False), (
        "a car nobody could read is never a tailgater: a dirty plate is never ban-listed"
    )


# ---------------------------------------------------------------------------
# A -- what was seen below the confidence is kept, and is not the plate
# ---------------------------------------------------------------------------


def test_what_was_read_below_the_confidence_is_kept_beside_an_unreadable_plate():
    controller, _, platform = a_lane([LOW])
    assert controller.run_once().outcome is Outcome.ALLOW
    [opened] = platform.opened
    assert (opened["plate"], opened["plate_unreadable"]) == (None, True)
    assert (opened["unsure_plate"], opened["unsure_plate_confidence"]) == (STRANGER, 0.42)


@pytest.mark.parametrize(
    "identity",
    [SEEN, ENGINE_DOWN, NO_PLATE,
     VehicleIdentity(plate="P" * 33, confidence=0.42, presence=True)],
    ids=["read", "engine down", "no plate", "longer than the platform takes"],
)
def test_nothing_is_kept_as_unsure_where_nothing_was_read_below_the_confidence(identity):
    controller, _, platform = a_lane([identity])
    controller.run_once()
    [opened] = platform.opened
    assert opened["unsure_plate"] is None, "and the stay is opened all the same"


# ---------------------------------------------------------------------------
# C -- a pass-only garage, until the registration QR
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("identity", "fallback"),
    [(LOW, Fallback.LOW_CONFIDENCE), (HOLDER_UNSURE, Fallback.LOW_CONFIDENCE),
     (NO_PLATE, Fallback.NO_PLATE_READ), (ENGINE_DOWN, Fallback.ENGINE_UNREACHABLE)],
    ids=["below the confidence", "the holder's plate, unsure", "no plate", "engine down"],
)
def test_a_pass_only_car_whose_pass_cannot_be_confirmed_goes_to_a_person(identity, fallback):
    controller, vend, platform = a_lane([identity], transient=False, register=a_register())
    decision = controller.run_once()
    assert (decision.outcome, decision.fallback) == (Outcome.FALLBACK, fallback)
    assert "the pass could not be confirmed" in decision.reason
    assert vend.vend_count == 0, "the gate stays shut until a person answers"
    assert [e for e in platform.events.values() if e["kind"] == "fallback_needs_human"]


def test_a_pass_only_garage_holding_no_register_sends_every_car_to_a_person():
    controller, vend, _ = a_lane([HOLDER_SEEN], transient=False, register=None)
    decision = controller.run_once()
    assert (decision.outcome, decision.fallback) == (Outcome.FALLBACK, Fallback.STALE_RULES)
    assert vend.vend_count == 0


def test_a_pass_only_garage_closed_full_denies_an_uncovered_car():
    stranger, vend, _ = a_lane([SEEN], transient=False, register=a_register(), closing=FULL)
    assert stranger.run_once().outcome is Outcome.DENY, "denied, not sent to a person"
    assert vend.vend_count == 0
    holder, vend, _ = a_lane([HOLDER_SEEN], transient=False, register=a_register(), closing=FULL)
    assert holder.run_once().outcome is Outcome.ALLOW
    unclear, vend, _ = a_lane([LOW], transient=False, register=a_register(), closing=FULL)
    assert unclear.run_once().outcome is Outcome.FALLBACK, "its pass may yet be confirmed"


def test_a_display_code_does_not_open_a_pass_only_garage_and_a_person_does():
    controller, vend, _ = a_lane([LOW], transient=False, register=a_register())
    controller.run_once()
    service = LaneService(controller)
    status, refused = complete(service, controller,
                               authority=VendAuthority.DISPLAY_CODE_CONFIRMED)
    assert (status, refused["code"]) == (409, VendRefusal.NOT_COMPLETABLE.value)
    assert vend.vend_count == 0
    status, _ = complete(service, controller, authority=VendAuthority.HUMAN_OPEN_NOW, key="KEY-002")
    assert status == 202
    settled(service)
    assert vend.vend_count == 1
    # THE CONTROL: at a garage that has not said, a display code still completes.
    unsaid, vend, _ = a_lane([LOW], transient=None, default_action=None)
    unsaid.run_once()
    status, _ = complete(LaneService(unsaid), unsaid,
                         authority=VendAuthority.DISPLAY_CODE_CONFIRMED)
    assert status == 202


# ---------------------------------------------------------------------------
# 4 -- a pass-only garage's registers through an outage
# ---------------------------------------------------------------------------


def a_pass_only_platform():
    platform = FakePlatform()
    platform.entitlements["garage_pass"] = {"consulted": True, "register": a_register()}
    rules = platform.get_rules
    platform.get_rules = lambda: {**rules(), "transient_available": False}
    return platform


def test_a_pass_only_garages_registers_outlive_the_bound_while_the_platform_is_away(tmp_path):
    path = narrow(tmp_path) / "cache.sqlite"
    platform = a_pass_only_platform()
    cache = DecisionCache(max_age_seconds=3600, store=DurableStore(path))
    sync_rules(platform, cache)
    cache._refreshed_at -= 7200
    assert cache.expire_if_past_bound() is True, "the premise: the personal data is wiped"
    assert cache.stays == {} and "garage_pass" in cache.entitlements, "the registers are kept"
    controller, vend, _ = a_lane([HOLDER_SEEN], cache=cache)
    assert controller.run_once().outcome is Outcome.ALLOW, "the monthly still gets in"
    restarted = DecisionCache(max_age_seconds=3600, store=DurableStore(path))
    controller, vend, _ = a_lane([HOLDER_SEEN], cache=restarted)
    assert controller.run_once().outcome is Outcome.ALLOW, "and after a restart"


def test_they_are_wiped_when_the_platform_answers_again(tmp_path):
    path = narrow(tmp_path) / "cache.sqlite"
    platform = a_pass_only_platform()
    cache = DecisionCache(max_age_seconds=3600, store=DurableStore(path))
    sync_rules(platform, cache)
    cache._refreshed_at -= 7200
    cache.expire_if_past_bound()
    # The platform answers -- and could not read the pass module this time.
    platform.entitlements["garage_pass"] = {"consulted": False, "unavailable": "timed out"}
    assert sync_rules(platform, cache) is not None
    assert "garage_pass" not in cache.entitlements, "kept through the outage, not past it"
    controller, vend, _ = a_lane([HOLDER_SEEN], cache=cache)
    assert controller.run_once().outcome is Outcome.FALLBACK, "no register held: a person"
    # THE CONTROL: inside the bound an unreadable module keeps its register, as it always did.
    fresh = DecisionCache(max_age_seconds=3600)
    platform.entitlements["garage_pass"] = {"consulted": True, "register": a_register()}
    sync_rules(platform, fresh)
    platform.entitlements["garage_pass"] = {"consulted": False, "unavailable": "timed out"}
    sync_rules(platform, fresh)
    assert "garage_pass" in fresh.entitlements


def test_the_control_a_transient_garages_registers_go_with_the_bound():
    platform = FakePlatform()
    platform.entitlements["garage_pass"] = {"consulted": True, "register": a_register()}
    rules = platform.get_rules
    platform.get_rules = lambda: {**rules(), "transient_available": True}
    cache = DecisionCache(max_age_seconds=3600)
    sync_rules(platform, cache)
    cache._refreshed_at -= 7200
    cache.expire_if_past_bound()
    assert cache.entitlements == {}


# ---------------------------------------------------------------------------
# F -- the pictures' ceiling, counted; a failed removal holds no flush
# ---------------------------------------------------------------------------


def sized(n, size, start=0):
    return [Frame(image_bytes=bytes([i % 251]) * size, captured_at=1.0 + i, camera_id="c")
            for i in range(start, start + n)]


def test_the_pictures_share_of_the_outbox_file_has_a_ceiling_and_drops_are_counted(tmp_path):
    store = OutboxStore(narrow(tmp_path) / "lane.outbox.sqlite", pictures_max_bytes=350)
    queue = EventQueue(PlatformTransport(FakePlatform(online=False)), store=store)
    queue.record(SESSION_OPEN, "lane-entry", plate="CAP-1", at="2026-10-27T10:00:00+00:00",
                 entry_confirmation=CONFIRMED)
    for car in range(5):
        queue.queue_pictures(f"e-{car}", sized(1, 100, start=car))
        assert queue.wait_written(5)
    assert store.picture_count() == 3, "the ceiling: 350 bytes hold three of 100"
    assert queue.pictures_dropped == 2
    kept = {p.event_id for _, p in store.held_pictures(10)}
    assert kept == {"e-2", "e-3", "e-4"}, "the oldest went first"
    assert len(store.held()) == 1, "and the stay is still on disk"
    queue.queue_pictures("e-big", sized(1, 400))
    assert queue.wait_written(5)
    assert queue.pictures_dropped == 3, "a picture larger than the ceiling is not kept"
    # Published where the lane's other drops are.
    controller, _, _ = a_lane([SEEN])
    controller.events = queue
    assert LaneService(controller).events(0).to_dict()["pictures_dropped"] == 3


def test_serve_wires_the_ceiling_a_site_states(tmp_path):
    from lane_controller import cli

    directory = narrow(tmp_path)
    base = dict(lane_id="l", site_id="s", camera=CameraConfig(camera_id="c", rtsp_url=""),
                gate=GateConfig(), cache_path=str(directory / "cache.sqlite"))
    platform = FakePlatform()
    platform.base_url = "https://platform.test"
    wired = cli.wire_lane(LaneConfig(**base, outbox_pictures_max_bytes=32 << 20), platform=platform)
    assert wired.controller.events._store.pictures_max_bytes == 32 << 20
    with pytest.raises(ValueError, match="outbox_pictures_max_bytes"):
        LaneConfig(**base, outbox_pictures_max_bytes=1024)


def test_a_removal_that_fails_holds_no_flush_and_sends_nothing_twice(tmp_path):
    class StuckDeletes(OutboxStore):
        """The first eight pictures -- a whole page -- cannot be removed."""

        def remove_pictures(self, refs):
            if any(ref in self.stuck for ref in refs):
                raise OSError(5, "I/O error")
            super().remove_pictures(refs)

    class Counting(FakePlatform):
        """Every post taken, and counted per picture."""

        def __init__(self):
            super().__init__()
            self.taken: dict[str, int] = {}

        def post_picture(self, *, picture_ref, **kwargs):
            self.taken[picture_ref] = self.taken.get(picture_ref, 0) + 1
            return super().post_picture(picture_ref=picture_ref, **kwargs)

    platform = Counting()
    store = StuckDeletes(narrow(tmp_path) / "lane.outbox.sqlite")
    queue = EventQueue(PlatformTransport(platform), store=store)
    queue.queue_pictures("e-1", pictures(10))
    assert queue.wait_written(5)
    store.stuck = {p.picture_ref for _, p in store.held_pictures(8)}
    for _ in range(3):
        queue.flush_pictures()
        assert queue.wait_written(5)
    assert len(platform.pictures) == 10, "the pictures behind the stuck page were never sent"
    assert set(platform.taken.values()) == {1}, "a picture already taken was sent again"


# ---------------------------------------------------------------------------
# E -- the three differences the review named, each held
# ---------------------------------------------------------------------------


def an_exit_lane(identity, *, cache=None):
    config = LaneConfig(
        lane_id="lane-exit", site_id="site-1", direction="exit",
        camera=CameraConfig(camera_id="sim-cam-1", rtsp_url="", frames_per_read=1),
        gate=GateConfig(), confidence_threshold=THRESHOLD,
        loops=LoopConfig(arming_loops=1, closing_loops=1, closing_spacing_m=0.0,
                         confirmation_window_seconds=WINDOW),
    )
    return LaneController(
        config, loop=SimulatedLoopInput(arrivals=1), camera=CannedCameraFeed(),
        vend=RecordingVendOutput(), identifier=StubVehicleIdentifier([identity]),
        closing_loops=ScriptedClosingLoops([]),
        # `is not None`, never `or`: an empty cache has no rules and is falsy.
        cache=cache if cache is not None else DecisionCache(),
    )


def test_an_exit_records_its_pending_close_on_the_read_as_it_came():
    """E1: the stay's "unreadable" is an entry's; an exit's pending record is
    what it was before this round."""
    unsure = VehicleIdentity(plate="EX-1", plate_region="FL", confidence=0.42)
    exit_lane = an_exit_lane(unsure)
    exit_lane.begin_transit(unsure, "2026-10-27T10:00:00+00:00")
    [pending] = [e.detail for e in exit_lane.events._queue if e.kind == "exit_pending"]
    assert (pending["identity_kind"], pending["plate_region"]) == ("plate", "FL")


def test_a_closed_exit_stays_closed_past_the_bound_and_its_gate_was_shut_there_before_too():
    """E2: kept, with this reason. The closing names no car, so it outlives the
    personal-data wipe. Before this round the wipe took the closing too -- and
    the empty cache then fell back `stale_rules` on every car at that exit, so
    the gate was shut either way. What changed is only the record: it now says
    the owner closed the lane, which is true, rather than that rules are old."""
    cache = DecisionCache(max_age_seconds=3600)
    cache.load([], default_action="allow", now=time.time() - 7200)
    cache._take_lane({"lane": EVERYONE})
    assert cache.expire_if_past_bound() is True
    exit_lane = an_exit_lane(SEEN, cache=cache)
    decision = exit_lane.run_once()
    assert (decision.outcome, decision.fallback) == (Outcome.FALLBACK, Fallback.LANE_CLOSED)
    assert exit_lane.vend.vend_count == 0


def test_a_stale_owners_rule_is_never_applied():
    """E3: reverted to what it was before this round -- an owner's rule for a
    plate is used only while the cache is trusted."""
    stale, vend, _ = a_lane([SEEN], rules=[Rule(plate=STRANGER, allow=False)], stale=True)
    assert stale.run_once().outcome is Outcome.ALLOW, "a stale deny rule was applied"
    fresh, vend, _ = a_lane([SEEN], rules=[Rule(plate=STRANGER, allow=False)])
    assert fresh.run_once().outcome is Outcome.DENY, "the control: a fresh one is"


# ---------------------------------------------------------------------------
# D -- the arming read of a car a person lets in, or that drives in anyway
# ---------------------------------------------------------------------------


def test_the_arming_read_of_a_car_a_person_lets_in_is_kept_with_its_stay():
    controller, _, platform = a_lane([LOW], transient=False, register=a_register(),
                                     images=IMAGES)
    controller.run_once()
    service = LaneService(controller)
    status, _ = complete(service, controller, authority=VendAuthority.HUMAN_OPEN_NOW)
    assert status == 202
    settled(service)
    [opened] = platform.opened
    kept = {p["image"] for p in pictures_of(platform, opened["event_id"])}
    assert set(IMAGES[:2]) <= kept, "the pictures the car was identified from were lost"


def test_the_arming_read_of_a_refused_car_that_drives_in_anyway_is_kept_with_its_stay():
    controller, _, platform = a_lane([SEEN, UNREAD], rules=[Rule(plate=STRANGER, allow=False)],
                                     images=IMAGES)
    controller.run_once()
    controller.run_once()
    [opened] = platform.opened
    kept = {p["image"] for p in pictures_of(platform, opened["event_id"])}
    assert set(IMAGES[:2]) <= kept


def test_a_crossing_whose_plate_says_it_is_another_car_does_not_take_them():
    controller, _, platform = a_lane([SEEN, TAILGATER], rules=[Rule(plate=STRANGER, allow=False)],
                                     images=IMAGES)
    controller.run_once()
    controller.run_once()
    [opened] = platform.opened
    kept = {p["image"] for p in pictures_of(platform, opened["event_id"])}
    assert not set(IMAGES[:2]) & kept


# ---------------------------------------------------------------------------
# Round 2 -- the registers' two ends, the refused car's pictures, the rule first
# ---------------------------------------------------------------------------


def kept_past_the_bound(path, *, offline_s=7 * 86_400.0):
    """A pass-only garage's lane, synced, then a day past its bound with the
    platform away: its registers kept."""
    platform = a_pass_only_platform()
    cache = DecisionCache(max_age_seconds=3600, store=DurableStore(path),
                          pass_registers_offline_s=offline_s)
    sync_rules(platform, cache)
    cache._refreshed_at -= 7200
    assert cache.expire_if_past_bound() is True
    assert "garage_pass" in cache.entitlements, "the premise: kept through the outage"
    return platform, cache


@pytest.mark.parametrize("status", [401, 403, 404])
def test_a_platform_that_refuses_this_lane_wipes_the_registers_at_once(tmp_path, status):
    """A revoked device token answers 401 for ever: the registers it handed the
    lane are held for nobody, and go now -- memory and disk."""
    path = narrow(tmp_path) / "cache.sqlite"
    platform, cache = kept_past_the_bound(path)

    def refused():
        raise PlatformRejected(status, "this lane is not one the platform knows")

    platform.get_rules = refused
    with pytest.raises(PlatformRejected):
        sync_rules(platform, cache)
    assert cache.entitlements == {}
    assert DecisionCache(max_age_seconds=3600, store=DurableStore(path)).entitlements == {}, (
        "the file still holds them"
    )
    controller, _, _ = a_lane([HOLDER_SEEN], cache=cache)
    assert controller.run_once().outcome is Outcome.FALLBACK, "to a person"


@pytest.mark.parametrize("refusal", ["unreachable", 409])
def test_the_control_an_outage_or_another_refusal_keeps_them(tmp_path, refusal):
    platform, cache = kept_past_the_bound(narrow(tmp_path) / "cache.sqlite")
    if refusal == "unreachable":
        platform.online = False
        assert sync_rules(platform, cache) is None
    else:
        def conflict():
            raise PlatformRejected(refusal, "not about this lane")

        platform.get_rules = conflict
        with pytest.raises(PlatformRejected):
            sync_rules(platform, cache)
    assert "garage_pass" in cache.entitlements


def test_past_the_ceiling_the_registers_go_and_a_car_goes_to_a_person(tmp_path):
    path = narrow(tmp_path) / "cache.sqlite"
    platform, cache = kept_past_the_bound(path)
    confirmed = cache._registers_confirmed_at
    assert cache.expire_if_past_bound(now=confirmed + 7 * 86_400 - 1) is False
    assert "garage_pass" in cache.entitlements, "inside the ceiling: kept"
    assert cache.expire_if_past_bound(now=confirmed + 7 * 86_400 + 1) is True
    assert cache.entitlements == {}
    assert DecisionCache(max_age_seconds=3600, store=DurableStore(path)).entitlements == {}
    controller, _, _ = a_lane([HOLDER_SEEN], cache=cache)
    decision = controller.run_once()
    assert decision.outcome is Outcome.FALLBACK
    assert "the pass could not be confirmed" in decision.reason


def test_a_restart_from_disk_holds_them_inside_the_ceiling_and_not_past_it(tmp_path):
    path = narrow(tmp_path) / "cache.sqlite"
    kept_past_the_bound(path, offline_s=10_000)  # confirmed two hours ago
    inside = DecisionCache(max_age_seconds=3600, store=DurableStore(path),
                           pass_registers_offline_s=10_000)
    assert "garage_pass" in inside.entitlements
    past = DecisionCache(max_age_seconds=3600, store=DurableStore(path),
                         pass_registers_offline_s=3_600)
    assert past.entitlements == {}, "restored past the ceiling"
    assert DecisionCache(max_age_seconds=3600, store=DurableStore(path)).entitlements == {}, (
        "and the file still holds them"
    )


def test_a_restart_after_the_bound_passed_while_the_process_was_down(tmp_path):
    path = narrow(tmp_path) / "cache.sqlite"
    platform = a_pass_only_platform()
    stopped = DecisionCache(max_age_seconds=3600, store=DurableStore(path))
    stopped.load_payload(platform.get_rules(), now=time.time() - 7200)
    restarted = DecisionCache(max_age_seconds=3600, store=DurableStore(path))
    assert "garage_pass" in restarted.entitlements and restarted.stays == {}
    short = DecisionCache(max_age_seconds=3600, store=DurableStore(path),
                          pass_registers_offline_s=3_600)
    assert short.entitlements == {}


def test_serve_wires_the_two_settings():
    from lane_controller import cli

    base = dict(lane_id="l", site_id="s", camera=CameraConfig(camera_id="c", rtsp_url=""),
                gate=GateConfig())
    wired = cli.wire_lane(LaneConfig(**base, pass_registers_offline_days=2), platform=None)
    assert wired.controller.cache._registers_offline_max == 2 * 86_400
    assert LaneConfig(**base).refused_arming_seconds == 120
    for name in ("pass_registers_offline_days", "refused_arming_seconds"):
        with pytest.raises(ValueError, match=name):
            LaneConfig(**base, **{name: 0})


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def test_a_refused_cars_pictures_do_not_go_to_a_crossing_hours_later():
    clock = Clock()
    controller, _, platform = a_lane([SEEN, UNREAD], rules=[Rule(plate=STRANGER, allow=False)],
                                     images=IMAGES, clock=clock)
    controller.run_once()
    clock.now += 6 * 3600
    controller.run_once()
    [opened] = platform.opened
    assert not set(IMAGES[:2]) & {p["image"] for p in pictures_of(platform, opened["event_id"])}
    # THE CONTROL: the same crossing inside the limit takes them.
    clock = Clock()
    controller, _, platform = a_lane([SEEN, UNREAD], rules=[Rule(plate=STRANGER, allow=False)],
                                     images=IMAGES, clock=clock)
    controller.run_once()
    clock.now += 60
    controller.run_once()
    [opened] = platform.opened
    assert set(IMAGES[:2]) <= {p["image"] for p in pictures_of(platform, opened["event_id"])}


def test_two_different_reads_below_the_confidence_are_two_cars():
    refused = VehicleIdentity(plate="AAA111", confidence=0.40, presence=True)
    other = VehicleIdentity(plate="ZZZ999", confidence=0.40, presence=True)
    controller, _, platform = a_lane([refused, other], transient=False, register=a_register(),
                                     images=IMAGES)
    assert controller.run_once().outcome is Outcome.FALLBACK, "the premise: refused, a person"
    controller.run_once()
    [opened] = platform.opened
    assert not set(IMAGES[:2]) & {p["image"] for p in pictures_of(platform, opened["event_id"])}


@pytest.mark.parametrize(
    ("crossing_look", "matcher", "attached"),
    [
        ("opvid-opa-id/2:a", "exact", True),
        ("opvid-opa-id/2:other", "exact", False),
        ("opvid-opa-id/2:a", None, False),
        ("opvid-opa-id/2:a", "broken", False),
    ],
    ids=["same look", "another look", "no comparison wired", "the comparison fails"],
)
def test_the_looks_must_match_and_a_doubt_attaches_nothing(crossing_look, matcher, attached):
    class Broken:
        def match(self, descriptor, banned):
            raise ConnectionError("the identity service is down")

    crossing = VehicleIdentity(plate=None, confidence=0.0, presence=True, descriptor=crossing_look)
    controller, _, platform = a_lane(
        [SEEN, crossing], rules=[Rule(plate=STRANGER, allow=False)], images=IMAGES,
        matcher=Broken() if matcher == "broken" else matcher,
    )
    controller.run_once()
    controller.run_once()
    [opened] = platform.opened
    kept = {p["image"] for p in pictures_of(platform, opened["event_id"])}
    assert (set(IMAGES[:2]) <= kept) is attached


def test_at_a_pass_only_garage_the_owners_rule_comes_before_the_registers():
    denied, vend, _ = a_lane([HOLDER_SEEN], transient=False, register=a_register(),
                             rules=[Rule(plate=HOLDER, allow=False)])
    assert denied.run_once().outcome is Outcome.DENY, "a pass let a plate the owner denied in"
    assert vend.vend_count == 0
    holder, vend, _ = a_lane([HOLDER_SEEN], transient=False, register=a_register())
    assert holder.run_once().outcome is Outcome.ALLOW, "the control: no rule, the pass opens"
