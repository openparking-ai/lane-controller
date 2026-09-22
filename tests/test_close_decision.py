"""The close carries the decision the barrier moved on (brief 4.9, the lane's
half). The platform's close consumes it -- writes its fee, runs no engine --
so what the screen showed and what the row says are one computation, and the
platform's reconciler (4.9b) re-derives the number from what rides here.

Each claim is paired with the case that falsifies it:

  * the close carries `local_decision`, and it is the whole of the decision
    event's `exit_pricing`, unedited -- for a covered car, a priced one, and
    one the cache could not price;
  * it rides under every confirmation an exit can close with (confirmed,
    held, unconfirmable), because each of those is a close that bills;
  * the settle takes the decision IT IS HANDED, never `last_decision` -- a
    settle on its own thread would otherwise carry the next car's;
  * an exit that decided nothing carries no key, and an entry never does;
  * on the wire the client sends it exactly, and only when there is one.

`scripts/close_decision_fail_control.py` breaks each in turn and requires this
file to go red. The fee + 1 lane is the PLATFORM's control (its reconciler
names it); this file's `fee_edited_in_transit` break is the lane-side mirror:
what the lane sends must be what it showed.
"""

from __future__ import annotations

import json
from pathlib import Path

from lane_controller import CameraConfig, DecisionCache, GateConfig, LaneConfig, LoopConfig
from lane_controller.controller import LaneController
from lane_controller.events import EventQueue
from lane_controller.exit_pricing import COVERED, NO_CACHED_ENTRY, PRICED
from lane_controller.interfaces import ClosingSequence, VehicleIdentity
from lane_controller.platform_client import PlatformClient
from lane_controller.simulated import (
    CannedCameraFeed,
    RecordingVendOutput,
    ScriptedClosingLoops,
    SimulatedLoopInput,
    StubVehicleIdentifier,
)
from lane_controller.sync import SESSION_CLOSE, SESSION_OPEN, PlatformTransport
from test_exit_decision import ENTRY, NOW, a_platform, gp_register, gp_row

PLAN = json.loads((Path(__file__).resolve().parent / "documents" / "flat_plan.json").read_text())
WINDOW = 10.0


def a_config(*, direction="exit", closing_loops=0):
    return LaneConfig(
        lane_id="lane-exit",
        site_id="site-exit",
        camera=CameraConfig(camera_id="sim-cam", rtsp_url="", frames_per_read=1),
        gate=GateConfig(),
        direction=direction,
        loops=LoopConfig(
            arming_loops=1, closing_loops=closing_loops, confirmation_window_seconds=WINDOW
        ),
    )


def a_cache(platform) -> DecisionCache:
    cache = DecisionCache(max_age_seconds=3600)
    cache.load_payload(platform.get_rules())
    return cache


class Spy(PlatformTransport):
    """The platform transport, keeping every event it was handed: the outbox
    empties on delivery, and the assertions below are about what was in it."""

    def __init__(self, platform):
        super().__init__(platform)
        self.sent: list = []

    def send(self, events):
        self.sent.extend(events)
        return super().send(events)


def lane(platform, plate, *, direction="exit", crossings=None, clock=None):
    """An exit lane reporting to `platform`; with `crossings`, two closing loops."""
    identity = (
        VehicleIdentity(plate=plate, plate_region="FL", make=None, model=None, color=None,
                        confidence=0.99)
        if plate
        else VehicleIdentity(plate=None, confidence=0.0, presence=True)
    )
    loops = ScriptedClosingLoops(crossings) if crossings is not None else None
    controller = LaneController(
        a_config(direction=direction, closing_loops=2 if loops else 0),
        loop=SimulatedLoopInput(arrivals=1),
        camera=CannedCameraFeed(),
        vend=RecordingVendOutput(),
        identifier=StubVehicleIdentifier([identity]),
        closing_loops=loops,
        cache=a_cache(platform),
        events=EventQueue(Spy(platform)),
        clock=clock or (lambda: NOW),
    )
    return controller


def recorded(controller, kind):
    """Every event of `kind` the lane produced -- delivered or still queued."""
    queued = [*controller.events._transport.sent, *controller.events._queue]
    return [e for e in queued if e.kind == kind]


def with_open_stay(platform, plate, session_id="s-7"):
    """The platform holds `plate`'s open stay, for the close to find."""
    platform.open_session(
        event_id=f"open-{session_id}", plate=plate, entry_at=ENTRY, entry_confirmation="confirmed"
    )
    return platform


# ---------------------------------------------------------------------------
# the close carries the decision, whole
# ---------------------------------------------------------------------------


def test_the_close_carries_the_decision_the_barrier_moved_on_unedited():
    platform = with_open_stay(
        a_platform(gp=gp_register(gp_row("PASS-1")), stays=[("s-7", "TRNS-7", ENTRY)]), "TRNS-7"
    )
    with_open_stay(platform, "PASS-1", "s-8")
    with_open_stay(platform, "NEW-1", "s-9")

    for plate, status in (("PASS-1", COVERED), ("TRNS-7", PRICED), ("NEW-1", NO_CACHED_ENTRY)):
        controller = lane(platform, plate)
        decision = controller.run_once()
        assert decision.should_vend and decision.exit_pricing.status == status
        shown = recorded(controller, "decision")[0].detail["exit_pricing"]
        on_the_close = recorded(controller, SESSION_CLOSE)[0].detail["local_decision"]
        delivered = platform.closed[-1]
        assert delivered["plate"] == plate
        assert on_the_close == shown, "the close does not carry what the decision event showed"
        assert delivered["local_decision"] == shown, "the platform got something else"
        assert delivered["local_decision"]["status"] == status
        assert "synced_at" in delivered["local_decision"]["computed_from"] or (
            delivered["local_decision"]["computed_from"]
        ), "the platform's reconciler needs what the lane decided from"

    priced = [c for c in platform.closed if c["plate"] == "TRNS-7"][0]["local_decision"]
    assert priced["fee_minor"] == 900 and priced["session_id"] == "s-7"
    assert priced["entry_at"] == ENTRY and priced["plan_version"] == PLAN["plan_version"]
    assert priced["currency"] == "USD" and priced["space_class"] == "standard"


def test_the_decision_rides_the_close_under_every_confirmation_an_exit_bills_with():
    platform = with_open_stay(a_platform(gp=gp_register(gp_row("PASS-1"))), "PASS-1")
    with_open_stay(platform, "PASS-1", "s-2")
    with_open_stay(platform, "PASS-1", "s-3")

    # confirmed: the crossing inside the window
    controller = lane(platform, "PASS-1", crossings=[(ClosingSequence.FORWARD, 3.0)])
    controller.run_once()
    close = recorded(controller, SESSION_CLOSE)[0].detail
    assert close["exit_confirmation"] == "confirmed"
    assert close["local_decision"]["status"] == COVERED

    # held: nothing crossed inside the window -- an exit still closes and bills
    controller = lane(platform, "PASS-1", crossings=[])
    controller.run_once()
    close = recorded(controller, SESSION_CLOSE)[0].detail
    assert close["exit_confirmation"] == "held"
    assert close["local_decision"]["status"] == COVERED

    # unconfirmable: a site with no closing loops
    controller = lane(platform, "PASS-1")
    controller.run_once()
    close = recorded(controller, SESSION_CLOSE)[0].detail
    assert close["exit_confirmation"] == "unconfirmable"
    assert close["local_decision"]["status"] == COVERED

    assert all(c["local_decision"]["status"] == COVERED for c in platform.closed)


def test_the_settle_carries_the_decision_it_is_handed_not_the_last_one():
    """A settle can run on a thread of its own (the assisted vend's), after the
    lane thread has moved on to the next car. The close it records must carry
    THIS car's decision, and the only way to be sure is to hand it down."""
    platform = with_open_stay(
        a_platform(gp=gp_register(gp_row("PASS-1")), stays=[("s-7", "TRNS-7", ENTRY)]), "TRNS-7"
    )
    with_open_stay(platform, "PASS-1", "s-8")
    controller = lane(platform, "TRNS-7")
    first = controller.run_once()
    assert first.exit_pricing.status == PRICED
    # The lane has moved on: the last decision is now the next car's.
    next_car = lane(platform, "PASS-1")
    second = next_car.run_once()
    assert second.exit_pricing.status == COVERED
    controller.last_decision = second

    identity = VehicleIdentity(plate="TRNS-7", plate_region="FL", make=None, model=None,
                               color=None, confidence=0.99)
    controller.resolve_transit(identity, "2026-06-10T18:00:01+00:00",
                               exit_pricing=first.exit_pricing)
    closes = recorded(controller, SESSION_CLOSE)
    assert closes[-1].detail["local_decision"]["status"] == PRICED, (
        "the settle recorded the LAST decision, not the one it was handed"
    )
    assert closes[-1].detail["local_decision"]["fee_minor"] == 900


# ---------------------------------------------------------------------------
# no decision, no key
# ---------------------------------------------------------------------------


def test_a_settle_handed_no_decision_and_an_entry_carry_no_key():
    """Every exit that vends today has a plate and a local decision, so a close
    with none is not reachable through `run_once`; the seam is reachable, and
    a caller that hands no decision (the assisted vend's settle, entry-only
    today) must produce a close the platform prices as it always has -- no key,
    not a null."""
    platform = with_open_stay(a_platform(gp=gp_register(gp_row("PASS-1"))), "PASS-1")
    controller = lane(platform, "PASS-1")
    identity = VehicleIdentity(plate="PASS-1", plate_region="FL", make=None, model=None,
                               color=None, confidence=0.99)
    controller.resolve_transit(identity, "2026-06-10T18:00:01+00:00")
    close = recorded(controller, SESSION_CLOSE)[0].detail
    assert close["exit_confirmation"] == "unconfirmable"
    assert "local_decision" not in close
    controller.events.flush()
    assert platform.closed[-1]["local_decision"] is None
    # an entry opens; the open has no such field
    controller = lane(platform, "PASS-1", direction="entry")
    controller.run_once()
    assert "local_decision" not in recorded(controller, SESSION_OPEN)[0].detail
    assert not recorded(controller, SESSION_CLOSE)


# ---------------------------------------------------------------------------
# on the wire
# ---------------------------------------------------------------------------


def test_the_client_sends_the_decision_exactly_and_only_when_there_is_one():
    client = PlatformClient("http://127.0.0.1:1", "token")
    bodies = []
    client._request = lambda method, path, body=None: bodies.append(body) or {"session": {}}
    closes = dict(event_id="e2", plate="TRNS-7", exit_at="2026-06-10T18:00:00+00:00",
                  exit_confirmation="confirmed", session_id="s-7")
    decision = {
        "status": "priced", "covered_by": [], "matched": [], "fee_minor": 900, "currency": "USD",
        "plan_version": PLAN["plan_version"], "entry_at": ENTRY,
        "exit_at": "2026-06-10T18:00:00+00:00", "session_id": "s-7", "space_class": "standard",
        "breakdown": [{"periods": 3}],
        "computed_from": {"rules_synced_at": 1.0, "stays_synced_at": 2.0, "stays_cursor": "9"},
    }
    client.close_session(**closes)
    client.close_session(**closes, local_decision=decision)
    assert "local_decision" not in bodies[0]
    assert bodies[1]["local_decision"] == decision, "edited on the way out"
    assert {k: v for k, v in bodies[1].items() if k != "local_decision"} == bodies[0]


def test_the_transport_hands_the_close_route_what_the_event_carries():
    platform = with_open_stay(
        a_platform(stays=[("s-7", "TRNS-7", ENTRY)]), "TRNS-7"
    )
    controller = lane(platform, "TRNS-7")
    controller.run_once()
    event = recorded(controller, SESSION_CLOSE)[0].detail["local_decision"]
    assert platform.closed[-1]["local_decision"] == event
    assert platform.closed[-1]["local_decision"]["fee_minor"] == 900
