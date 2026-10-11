"""U7f: pictures kept with an entry, the tailgater identified, and banned cars.

His answers, 2026-10-11: pictures at the closing loop and a few between the
vend and it; a tailgater whose identity is read is a stay like any other and
pays at the exit; a car on the garage's banned list is refused at the entry,
the gate shut; the arming loop's pictures stay as they are -- read, never kept.

  1  a confirmed entry's pictures go with its stay: between the vend and the
     closing loop, and at it. A held entry keeps none; nor does an ordinary
     arrival's arming read
  2  a tailgater read at the closing loop is a stay, `tailgater: true`; not
     read, it is `entry_unadmitted` as before, with its pictures. The car this
     lane admitted and held, crossing late, is a stay but not a tailgater
  4  a car whose descriptor matches the banned list: no vend, reason `banned`,
     its pictures with the decision. No match, no comparison wired, or a
     comparison that fails: the car is decided as before

The U7f gate's findings, each pinned here (F1-F4): an exit lets a banned car
out; a different car crossing after a held vend is a tailgater; the list
survives a restart with the platform unreachable; pictures wait out an outage.
The re-gate's G1: a site that states no thresholds refuses nobody as banned.
"""

from __future__ import annotations

import json

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
    cli,
)
from lane_controller.decision import Outcome, Rule
from lane_controller.durable import CACHE_DIR_MODE, DurableStore
from lane_controller.events import EventQueue
from lane_controller.interfaces import ClosingSequence
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
    PlatformTransport,
    sync_rules,
)
from lane_controller.vehicle_id_client import VehicleIdSearch

OCC = ClosingSequence.OCCUPIED
WINDOW = 10.0
BANNED_DESCRIPTOR = "opvid-opa-id/2:banned-car"

ALLOWED = VehicleIdentity(
    plate="SIM-0001", confidence=0.97, presence=True, descriptor="opvid-opa-id/2:sim"
)
REFUSED = VehicleIdentity(plate="BANNED-1", confidence=0.97, presence=True)
TAILGATER = VehicleIdentity(
    plate="TAIL-1", confidence=0.97, presence=True, descriptor="opvid-opa-id/2:tail"
)
UNREAD = VehicleIdentity(plate=None, confidence=0.0)
ON_THE_LIST = VehicleIdentity(
    plate="LISTED-1", confidence=0.97, presence=True, descriptor=BANNED_DESCRIPTOR
)


def build(*, crossings=(), identities=(ALLOWED,), banned=(), matcher="exact", direction="entry"):
    config = LaneConfig(
        lane_id="lane-test",
        site_id="site-test",
        camera=CameraConfig(camera_id="sim-cam-1", rtsp_url="", frames_per_read=1),
        gate=GateConfig(),
        direction=direction,
        confidence_threshold=0.85,
        loops=LoopConfig(
            arming_loops=1,
            closing_loops=1,
            closing_spacing_m=0.0,
            confirmation_window_seconds=WINDOW,
        ),
    )
    cache = DecisionCache()
    cache.load([Rule(plate="BANNED-1", allow=False)], default_action="allow")
    cache.banned = list(banned)
    platform = FakePlatform()
    vend = RecordingVendOutput()
    controller = LaneController(
        config,
        loop=SimulatedLoopInput(arrivals=1),
        camera=CannedCameraFeed(),
        vend=vend,
        identifier=StubVehicleIdentifier(list(identities)),
        closing_loops=ScriptedClosingLoops(list(crossings)),
        cache=cache,
        events=EventQueue(PlatformTransport(platform)),
        banned_matcher=ExactBannedMatcher() if matcher == "exact" else matcher,
    )
    return controller, vend, platform


def kinds(platform: FakePlatform) -> list[str]:
    return [e["kind"] for e in platform.events.values()]


def event_id_of(platform: FakePlatform, kind: str) -> str:
    return next(e["event_id"] for e in platform.events.values() if e["kind"] == kind)


def pictures_of(platform: FakePlatform, event_id: str) -> int:
    return sum(1 for p in platform.pictures.values() if p["event_id"] == event_id)


# --- 1 pictures ----------------------------------------------------------------


def test_a_confirmed_entry_keeps_its_pictures_with_its_stay():
    controller, vend, platform = build(crossings=[(OCC, 3.0)])
    controller.run_once()
    assert vend.vend_count == 1
    [opened] = platform.opened
    assert opened["entry_confirmation"] == CONFIRMED
    assert opened["tailgater"] is False
    # Two between the vend and the closing loop, two at it: all with the stay.
    assert pictures_of(platform, opened["event_id"]) == 4
    assert len(platform.pictures) == 4
    assert all(p["image"] for p in platform.pictures.values())


def test_a_held_entry_and_an_ordinary_refusal_keep_no_picture():
    """The control: pictures belong to an entry record. A vend nothing
    confirmed has none, and an arrival's arming read is never kept."""
    held, vend, platform = build(crossings=[])
    held.run_once()
    assert vend.vend_count == 1
    assert platform.opened == []
    assert platform.pictures == {}

    refused, vend, platform = build(identities=(REFUSED,))
    assert refused.run_once().outcome is Outcome.DENY
    assert vend.vend_count == 0
    assert platform.pictures == {}, "the arming read is not kept"


# --- 2 the tailgater -------------------------------------------------------------


def test_a_tailgater_read_at_the_closing_loop_is_a_stay_like_any_other():
    controller, vend, platform = build(identities=(REFUSED, TAILGATER), crossings=[(OCC, 3.0)])
    assert controller.run_once().outcome is Outcome.DENY
    assert controller.run_once() is None, "an idle turn"
    assert vend.vend_count == 0
    assert ENTRY_UNADMITTED in kinds(platform), "still recorded as an entry nothing admitted"
    unadmitted = next(e for e in platform.events.values() if e["kind"] == ENTRY_UNADMITTED)
    assert unadmitted["detail"]["identified"] is True
    [opened] = platform.opened
    assert (opened["plate"], opened["tailgater"], opened["entry_confirmation"]) == (
        "TAIL-1",
        True,
        CONFIRMED,
    )
    assert opened["descriptor"] == TAILGATER.descriptor
    assert pictures_of(platform, opened["event_id"]) == 2


def test_a_tailgater_not_read_stays_an_entry_nothing_admitted_with_its_pictures():
    controller, vend, platform = build(identities=(REFUSED, UNREAD), crossings=[(OCC, 3.0)])
    controller.run_once()
    controller.run_once()
    assert platform.opened == [], "nothing read, no stay"
    unadmitted = event_id_of(platform, ENTRY_UNADMITTED)
    assert platform.events[unadmitted]["detail"]["identified"] is False
    assert pictures_of(platform, unadmitted) == 2


def test_a_read_below_the_lanes_confidence_is_not_a_read():
    unsure = VehicleIdentity(plate="TAIL-2", confidence=0.5, presence=True)
    controller, _, platform = build(identities=(REFUSED, unsure), crossings=[(OCC, 3.0)])
    controller.run_once()
    controller.run_once()
    assert platform.opened == []


def test_the_car_this_lane_admitted_and_held_crossing_late_is_a_stay_but_not_a_tailgater():
    controller, vend, platform = build(
        identities=(ALLOWED, ALLOWED), crossings=[(OCC, WINDOW + 1.0), (OCC, 3.0)]
    )
    controller.run_once()
    assert vend.vend_count == 1
    assert platform.opened == [], "held: the late crossing confirmed nothing"
    controller.run_once()
    [opened] = platform.opened
    assert opened["plate"] == "SIM-0001"
    assert opened["tailgater"] is False, "it was let in: never listed for being slow"


# --- 4 banned at the entry ----------------------------------------------------------

BANNED_LIST = [{"id": "ban-1", "descriptor": BANNED_DESCRIPTOR}]


def test_a_car_on_the_banned_list_is_refused_and_recorded_with_its_pictures():
    controller, vend, platform = build(
        identities=(ON_THE_LIST,), banned=BANNED_LIST, crossings=[(OCC, 3.0)]
    )
    decision = controller.run_once()
    assert decision.outcome is Outcome.DENY
    assert decision.reason == REASON_BANNED
    assert vend.vend_count == 0, "the gate stays shut"
    assert platform.opened == []
    decided = event_id_of(platform, "decision")
    assert platform.events[decided]["detail"]["reason"] == REASON_BANNED
    assert pictures_of(platform, decided) == 2
    # What a consumer of the read contract sees: a refusal, by its reason.
    assert controller.last_decision.reason == REASON_BANNED


@pytest.mark.parametrize(
    ("banned", "matcher", "why"),
    [
        ([{"id": "ban-2", "descriptor": "opvid-opa-id/2:someone-else"}], "exact", "no match"),
        ([], "exact", "an empty list"),
        (BANNED_LIST, None, "no comparison wired"),
    ],
)
def test_a_car_not_matched_is_decided_as_before(banned, matcher, why):
    controller, vend, _ = build(
        identities=(ON_THE_LIST,), banned=banned, matcher=matcher, crossings=[(OCC, 3.0)]
    )
    assert controller.run_once().outcome is Outcome.ALLOW, why
    assert vend.vend_count == 1


def test_a_comparison_that_fails_refuses_nobody():
    class Broken:
        def match(self, descriptor, banned):
            raise ConnectionError("the identity service is down")

    controller, vend, _ = build(
        identities=(ON_THE_LIST,), banned=BANNED_LIST, matcher=Broken(), crossings=[(OCC, 3.0)]
    )
    assert controller.run_once().outcome is Outcome.ALLOW
    assert vend.vend_count == 1


def test_the_list_arrives_on_the_rules_payload_and_a_malformed_entry_is_left_out():
    cache = DecisionCache()
    cache.load_payload(
        {
            "default_action": "allow",
            "banned": [
                {"id": "ban-1", "descriptor": BANNED_DESCRIPTOR},
                {"id": "", "descriptor": "x"},
                {"id": "ban-3"},
                "not an entry",
            ],
        }
    )
    assert cache.banned == [{"id": "ban-1", "descriptor": BANNED_DESCRIPTOR}]
    cache.load_payload({"default_action": "allow"})
    assert cache.banned == [], "a platform with no list: none"


# --- the comparison: the identity service's search, as the platform asks it ----------


def test_the_search_is_the_platforms_body_and_reads_its_answer():
    asked = []

    def answer(outcome, matched):
        def opener(url, payload, timeout):
            asked.append((url, payload))
            return {"search": {"outcome": outcome, "matched": matched}}

        return opener

    search = VehicleIdSearch(
        "http://127.0.0.1:8090/", structure=0.4, colour=0.3, opener=answer("match", ["ban-1"])
    )
    assert search.match("opvid-opa-id/2:car", BANNED_LIST) == "ban-1"
    url, body = asked[0]
    assert url == "http://127.0.0.1:8090/v1/searches"
    assert body == {
        "descriptor": "opvid-opa-id/2:car",
        "candidates": [{"id": "ban-1", "descriptor": BANNED_DESCRIPTOR}],
        "thresholds": {"structure": 0.4, "colour": 0.3},
    }
    assert (
        VehicleIdSearch(
            "http://x", structure=0.4, colour=0.3, opener=answer("tie", ["ban-1"])
        ).match("d", BANNED_LIST)
        == "ban-1"
    )
    assert (
        VehicleIdSearch("http://x", structure=0.4, colour=0.3, opener=answer("no_match", [])).match(
            "d", BANNED_LIST
        )
        is None
    )
    with pytest.raises(ValueError):
        VehicleIdSearch("http://x", structure=0.4, colour=0.3, opener=answer("maybe", [])).match(
            "d", BANNED_LIST
        )
    with pytest.raises(ValueError):
        VehicleIdSearch(
            "http://x", structure=0.4, colour=0.3, opener=answer("match", ["not-listed"])
        ).match("d", BANNED_LIST)
    with pytest.raises(ValueError):
        VehicleIdSearch("http://x", structure=1.5, colour=0.3)


def test_the_thresholds_are_stated_together_or_not_at_all():
    base = dict(
        lane_id="l", site_id="s", camera=CameraConfig(camera_id="c", rtsp_url=""), gate=GateConfig()
    )
    LaneConfig(**base)
    LaneConfig(**base, banned_match_structure=0.4, banned_match_colour=0.3)
    with pytest.raises(ValueError, match="together"):
        LaneConfig(**base, banned_match_structure=0.4)
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        LaneConfig(**base, banned_match_structure=0.4, banned_match_colour=2.0)


def test_a_picture_leaves_on_its_own_route_and_never_on_the_read_window():
    controller, _, platform = build(crossings=[(OCC, 3.0)])
    controller.run_once()
    assert len(platform.pictures) == 4
    window = json.dumps([e.as_dict() for _, e in controller.events._history], default=str)
    assert "image" not in window and "picture" not in window


# --- the gate's findings (U7f fix 1) ---------------------------------------------------


def test_an_exit_lets_a_banned_car_out():
    """F1. The exit lanes receive the list too; a banned car is never kept in."""
    controller, vend, _ = build(
        identities=(ON_THE_LIST,), banned=BANNED_LIST, crossings=[(OCC, 3.0)], direction="exit"
    )
    assert controller.run_once().outcome is Outcome.ALLOW
    assert vend.vend_count == 1, "the barrier opens for it"


def test_a_different_car_crossing_after_a_held_vend_is_a_tailgater():
    """F2. Only the held car itself, by its plate, is let off as late."""
    controller, vend, platform = build(
        identities=(ALLOWED, TAILGATER), crossings=[(OCC, WINDOW + 1.0), (OCC, 3.0)]
    )
    controller.run_once()
    assert vend.vend_count == 1 and platform.opened == [], "held"
    controller.run_once()
    [opened] = platform.opened
    assert opened["plate"] == "TAIL-1"
    assert opened["tailgater"] is True


def test_the_list_survives_a_restart_with_the_platform_unreachable(tmp_path):
    """F3. A lane that restarts offline still refuses the cars it was told to."""
    directory = tmp_path / "lane-cache"
    directory.mkdir(mode=CACHE_DIR_MODE)
    directory.chmod(CACHE_DIR_MODE)
    path = directory / "cache.sqlite"
    platform = FakePlatform()
    platform.banned = list(BANNED_LIST)
    first = DecisionCache(max_age_seconds=3600, store=DurableStore(path))
    sync_rules(platform, first)
    assert first.banned == BANNED_LIST, "the premise: the list arrived"
    platform.online = False
    second = DecisionCache(max_age_seconds=3600, store=DurableStore(path))
    assert sync_rules(platform, second) is None, "the premise: the platform is unreachable"
    assert second.banned == BANNED_LIST


def test_pictures_wait_out_an_outage_and_go_when_it_ends():
    """F4. The platform unreachable: the pictures stay queued, then are sent."""
    controller, _, platform = build(crossings=[(OCC, 3.0)])
    platform.online = False
    controller.run_once()
    assert platform.pictures == {}
    assert controller.events.pending_pictures == 4
    platform.online = True
    controller.events.flush()
    controller.events.flush_pictures()
    assert len(platform.pictures) == 4
    assert controller.events.pending_pictures == 0


# --- the re-gate's finding (U7f fix 2) -------------------------------------------------


def a_wired_entry(**thresholds):
    config = LaneConfig(
        lane_id="l",
        site_id="s",
        camera=CameraConfig(camera_id="c", rtsp_url=""),
        gate=GateConfig(),
        direction="entry",
        vehicle_id_url="http://127.0.0.1:9",
        **thresholds,
    )
    return cli.wire_lane(config, platform=None)


def test_a_site_that_states_no_thresholds_refuses_nobody_as_banned():
    """G1. No operating point is measured, so there is no default: the lane
    `serve` builds has no banned comparison until the site states both."""
    wired = a_wired_entry()
    assert wired.controller.banned_matcher is None
    assert wired.seams["banned"].startswith("NONE")


def test_the_thresholds_a_site_states_are_the_ones_the_search_sends():
    """G1's control: stated, the comparison is wired with exactly those."""
    wired = a_wired_entry(banned_match_structure=0.4, banned_match_colour=0.3)
    assert wired.controller.banned_matcher is not None
    assert wired.controller.banned_matcher.thresholds == {"structure": 0.4, "colour": 0.3}
    assert wired.seams["banned"].startswith("REAL")
