"""The lane contract's guarantees, each with the control that proves it can fail.

Every assertion here is about the PUBLISHED surface: what a consumer receives,
and what it can and cannot be told. The controls live in
`scripts/contract_fail_control.py`, which breaks the thing each guarantee
guards and requires this file to go red.
"""

from __future__ import annotations

import io
import json
import re
import socket
import time
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest
from vehicle_id.contract import ANSWER, SCHEMA_VERSION, Engine, Identity, Read

from lane_consumer import LaneConsumer
from lane_controller import (
    CameraConfig,
    DecisionCache,
    EventQueue,
    Fallback,
    GateConfig,
    LaneConfig,
    LaneController,
    LoopConfig,
    Outcome,
    VehicleIdentity,
)
from lane_controller.cli import build_parser
from lane_controller.config import (
    DEFAULT_IDENTITY_HEALTH_TIMEOUT_S,
    DEFAULT_OUTBOX_DEPTH_THRESHOLD,
)
from lane_controller.contract import (
    CONTRACT_VERSION,
    FALLBACK_REASONS,
    NEVER_ALARM,
    OUTCOMES,
    REQUIRED_CAUSES,
    REQUIRED_REASONS,
    SOURCES,
    HealthEntry,
    HealthState,
    LaneHealth,
    LastDecision,
    MalfunctionCode,
    Source,
    Transit,
    TransitState,
)
from lane_controller.events import DEFAULT_HISTORY, SESSION_KINDS, LaneEvent
from lane_controller.interfaces import ClosingSequence, Unavailable
from lane_controller.platform_client import PlatformClient
from lane_controller.service import (
    ACT_ROUTES,
    READ_ROUTES,
    InsecureBind,
    LaneService,
    _Handler,
    assert_bind_allowed,
    make_server,
)
from lane_controller.simulated import (
    CannedCameraFeed,
    OccupancyLoopInput,
    RecordingVendOutput,
    ScriptedClosingLoops,
    SimulatedLoopInput,
    StubVehicleIdentifier,
)
from lane_controller.sync import SESSION_OPEN as SESSION_OPEN_KIND
from lane_controller.sync import PlatformTransport
from lane_controller.vehicle_id_client import VehicleIdClient
from serving import serving

CONTRACT_DOC = Path(__file__).resolve().parent.parent / "docs" / "CONTRACT.md"


# ---------------------------------------------------------------------------
# Lanes to read
# ---------------------------------------------------------------------------


def full_lane(identities=None, crossings=None, events=None):
    """A lane with the standard installation: two arming loops, two closing."""
    config = LaneConfig(
        lane_id="lane-1",
        site_id="site-1",
        camera=CameraConfig(camera_id="sim-cam-1", rtsp_url="", frames_per_read=3),
        gate=GateConfig(),
        loops=LoopConfig(
            arming_loops=2,
            arming_spacing_m=1.5,
            closing_loops=2,
            closing_spacing_m=1.5,
            confirmation_window_seconds=10.0,
        ),
    )
    cache = DecisionCache()
    cache.load([])
    cache.default_action = "allow"
    return LaneController(
        config,
        loop=SimulatedLoopInput(arrivals=1),
        camera=CannedCameraFeed(),
        vend=RecordingVendOutput(),
        identifier=StubVehicleIdentifier(identities),
        arming_loop_b=OccupancyLoopInput(),
        closing_loops=ScriptedClosingLoops(crossings or []),
        cache=cache,
        events=events or EventQueue(),
    )


def bare_lane():
    """A lane with one arming loop and no closing loops. Confirms nothing."""
    config = LaneConfig(
        lane_id="lane-2",
        site_id="site-1",
        camera=CameraConfig(camera_id="sim-cam-1", rtsp_url="", frames_per_read=1),
        gate=GateConfig(),
    )
    cache = DecisionCache()
    cache.load([])
    cache.default_action = "allow"
    return LaneController(
        config,
        loop=SimulatedLoopInput(arrivals=1),
        camera=CannedCameraFeed(),
        vend=RecordingVendOutput(),
        identifier=StubVehicleIdentifier(),
        cache=cache,
    )


# ---------------------------------------------------------------------------
# GUARANTEE 1 — the document and the code agree
# ---------------------------------------------------------------------------


#: The blocks in the document that are a ROUTE's payload. `sets` is in the same
#: marked-block mechanism and is deliberately not one of them: it publishes the
#: closed sets a third party needs in order to implement this contract at all,
#: and it is compared against the ENUMS rather than against a live response.
ROUTE_PAYLOADS = {"lane", "state", "health", "events"}


def doc_payloads() -> dict[str, dict]:
    """Every `<!--payload:NAME-->` example in `docs/CONTRACT.md`, parsed."""
    text = CONTRACT_DOC.read_text(encoding="utf-8")
    found = re.findall(r"<!--payload:([a-z]+)-->\s*```json\n(.*?)\n```", text, re.S)
    return {name: json.loads(body) for name, body in found}


def shape(value):
    """The KEY structure of a payload, with every leaf value discarded.

    Values move -- a cursor, a timestamp, a lane id. The shape is what a
    consumer writes code against, and it is what this compares. Lists are
    reduced to the shape of their first element, because a payload's list is
    homogeneous by construction and a doc example shows one of them.

    `detail` is not descended into. The contract declares it OPAQUE -- whatever
    the lane recorded with the event -- so its keys are not part of the shape a
    consumer writes code against, and requiring the document to reproduce one
    lane's event detail would make the doc a copy of the event vocabulary.
    """
    if isinstance(value, dict):
        return {
            key: (None if key == "detail" else shape(item)) for key, item in sorted(value.items())
        }
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    return None


def test_the_document_shows_exactly_the_payloads_the_code_builds():
    """`docs/CONTRACT.md` and `contract.py` agree, field for field.

    A document is the thing an integrator reads before they write a line, and
    a hand-written example is a second copy of a shape the code already
    defines. This derives the expectation from the CODE and compares the doc
    against it, so a field added, renamed or dropped in either one goes red.
    """
    doc = doc_payloads()
    assert set(doc) == ROUTE_PAYLOADS | {"sets"}, (
        "every route in the contract has a payload example, every example "
        "belongs to a route, and `sets` is the closed-set block that is not a "
        f"route. Found {sorted(doc)}"
    )

    controller = full_lane()
    controller.run_once()
    service = LaneService(controller)

    live = {
        "lane": service.describe().to_dict(),
        "state": service.state().to_dict(),
        "health": service.health().to_dict(),
        "events": service.events(0).to_dict(),
    }

    # The doc's health example shows ONE entry; the live payload carries every
    # code. Comparing shapes handles that -- a list reduces to its first
    # element -- and completeness is guarantee 3's job, not this one.
    for name in sorted(ROUTE_PAYLOADS):
        example = doc[name]
        assert shape(example) == shape(live[name]), (
            f"docs/CONTRACT.md's `{name}` example does not have the shape "
            f"`contract.py` builds.\n  doc:  {shape(example)}\n  code: {shape(live[name])}"
        )


def test_the_documents_geometry_example_is_the_lanes_own_five_keys():
    """The doc's geometry is not a hand-invented dict.

    `shape()` above compares keys, so this is the same check one level down --
    the geometry example must be exactly what `LoopConfig.as_published()`
    produces, which is what stops the document growing a sixth loop setting
    nobody implemented.
    """
    doc = doc_payloads()["lane"]
    assert set(doc["geometry"]) == set(LoopConfig().as_published())


# ---------------------------------------------------------------------------
# GUARANTEE 2 — the geometry is the lane's own, not a copy
# ---------------------------------------------------------------------------


def test_the_published_geometry_is_byte_for_byte_the_lanes_own():
    """`GET /v1/lane` carries `LoopConfig.as_published()` and not a rendering.

    Serialised on both sides and compared as bytes, so a re-keying, a
    re-ordering or a rounding shows up. The lane already publishes this exact
    dict on every vehicle under `geometry_assumed`; a second copy here would be
    a second thing to go stale, and the first symptom would be a consumer
    reading a geometry the events disagree with.
    """
    controller = full_lane()
    published = controller.config.loops.as_published()
    served = LaneService(controller).describe().to_dict()["geometry"]

    assert json.dumps(served, sort_keys=True) == json.dumps(published, sort_keys=True)


def test_the_geometry_control_moves_when_the_geometry_does():
    """The control for the test above: change one value, see it arrive changed.

    Without this, a service that returned a hard-coded dict identical to the
    default would pass the comparison above forever.
    """
    controller = full_lane()
    other = LaneController(
        LaneConfig(
            lane_id="lane-1",
            site_id="site-1",
            camera=CameraConfig(camera_id="c", rtsp_url="", frames_per_read=1),
            gate=GateConfig(),
            loops=LoopConfig(
                arming_loops=2,
                arming_spacing_m=2.75,
                closing_loops=2,
                closing_spacing_m=1.5,
                confirmation_window_seconds=4.0,
            ),
        ),
        loop=SimulatedLoopInput(arrivals=0),
        camera=CannedCameraFeed(),
        vend=RecordingVendOutput(),
        identifier=StubVehicleIdentifier(),
        arming_loop_b=OccupancyLoopInput(),
        closing_loops=ScriptedClosingLoops(),
    )
    served = LaneService(other).describe().to_dict()["geometry"]
    assert served["arming_spacing_m"] == 2.75
    assert served["confirmation_window_seconds"] == 4.0
    assert served != LaneService(controller).describe().to_dict()["geometry"]


# ---------------------------------------------------------------------------
# GUARANTEE 3 — every health code ships, with a source
# ---------------------------------------------------------------------------


def test_every_malfunction_code_ships_with_a_source_label():
    """The whole table, every time, each saying where its answer came from."""
    codes = LaneService(full_lane()).health().to_dict()["codes"]

    assert [entry["code"] for entry in codes] == [code.value for code in MalfunctionCode]
    for entry in codes:
        assert entry["source"] in {source.value for source in Source}
        assert entry["state"] in {state.value for state in HealthState}


def test_a_health_payload_missing_a_code_is_refused():
    """The control for the test above, and it fires at construction.

    A code that is absent reads to a consumer exactly like a code that is fine,
    so the payload cannot be built without all of them. Dropping one is the
    positive control, and it must not be possible to publish it.
    """
    every = tuple(
        HealthEntry(code=code.value, state=HealthState.UNKNOWN.value)
        for code in MalfunctionCode
    )
    LaneHealth(entries=every)  # intact, builds

    with pytest.raises(ValueError, match="missing"):
        LaneHealth(entries=every[:-1])
    with pytest.raises(ValueError, match="twice"):
        LaneHealth(entries=every + (every[0],))


def test_a_state_that_is_not_unknown_needs_a_measured_source():
    """`ok` and `active` are claims about a measurement.

    This is the invariant the surface exists to hold. A code nothing produces
    reporting `ok` is a clean bill of health from an instrument that is not
    plugged in, and it is the exact shape of "wrong silently".
    """
    measured = next(c for c, src in SOURCES.items() if src is Source.MEASURED)
    unmeasured = next(c for c, src in SOURCES.items() if src is not Source.MEASURED)

    HealthEntry(code=measured.value, state=HealthState.OK.value)
    HealthEntry(code=unmeasured.value, state=HealthState.UNKNOWN.value)

    with pytest.raises(ValueError, match="Only a code this build derives"):
        HealthEntry(code=unmeasured.value, state=HealthState.OK.value)
    with pytest.raises(ValueError, match="Only a code this build derives"):
        HealthEntry(code=unmeasured.value, state=HealthState.ACTIVE.value)


def test_reference_not_recognised_is_never_an_alarm():
    """SETTLED 3d(i). One reason covers an ordinary car arriving.

    A monitor that pages a technician on this code is the failure the engine's
    caveat exists to prevent, so the code carries the caveat and the flag, from
    one mapping, and `LaneConsumer.actionable` refuses to act on it whatever
    the state says.
    """
    codes = {e["code"]: e for e in LaneService(full_lane()).health().to_dict()["codes"]}
    entry = codes[MalfunctionCode.REFERENCE_NOT_RECOGNISED.value]

    assert entry["never_alarm"] is True
    assert entry["caveat"] == NEVER_ALARM[MalfunctionCode.REFERENCE_NOT_RECOGNISED]
    assert LaneConsumer.actionable({**entry, "state": "active"}) is False

    # The control: every other code is actionable when it is active, so the
    # flag above is not simply never true.
    other = codes[MalfunctionCode.OUTBOX_DEPTH_GROWING.value]
    assert other["never_alarm"] is False
    assert other["caveat"] is None
    assert LaneConsumer.actionable({**other, "state": "active"}) is True


def test_the_measured_codes_actually_move():
    """A `measured` source that never changes state is not a measurement.

    Each carries its own control: the condition it names is created, and the
    state must follow it. The two this round promoted have their own section
    below, because each needs a service or a platform to answer it.
    """
    service = LaneService(full_lane())
    codes = lambda: {e["code"]: e for e in service.health().to_dict()["codes"]}  # noqa: E731

    # Nothing has arrived, so nobody has asked the engine anything.
    assert codes()[MalfunctionCode.IDENTITY_SERVICE_DOWN.value]["state"] == "unknown"

    service.controller.identifier = StubVehicleIdentifier(
        [VehicleIdentity(plate=None, confidence=0.0, unavailable=Unavailable.UNREACHABLE)]
    )
    service.controller.run_once()
    assert codes()[MalfunctionCode.IDENTITY_SERVICE_DOWN.value]["state"] == "active"

    # The outbox, and the other direction: `ok` while the DEPTH is under this
    # site's threshold, `active` once it is over. Not `dropped` -- that counts
    # what has already been lost, so it moves long after this code should have.
    assert codes()[MalfunctionCode.OUTBOX_DEPTH_GROWING.value]["state"] == "ok"
    threshold = service.controller.config.outbox_depth_threshold
    while service.controller.events.pending <= threshold:
        service.controller.events.record("noise", "lane-1")
    assert codes()[MalfunctionCode.OUTBOX_DEPTH_GROWING.value]["state"] == "active"
    assert service.controller.events.dropped == 0

    # No transport wired, so nothing can have been dead-lettered and nobody
    # measured whether anything was. `unknown`, not `ok`.
    assert codes()[MalfunctionCode.SESSION_ACTIONS_DEAD_LETTERED.value]["state"] == "unknown"


# ---------------------------------------------------------------------------
# GUARANTEE 4 — the events cursor reproduces the Vehicle ID semantics
# ---------------------------------------------------------------------------


def test_the_cursor_is_monotonic_and_since_returns_what_follows_it():
    controller = full_lane()
    controller.run_once()
    service = LaneService(controller)

    page = service.events(0).to_dict()
    assert page["cursor"] >= 1
    assert page["reset"] is False
    assert [item["cursor"] for item in page["events"]] == list(range(1, page["cursor"] + 1))

    # From the middle: everything after that cursor, and nothing before it.
    middle = page["cursor"] // 2
    later = service.events(middle).to_dict()
    assert [item["cursor"] for item in later["events"]] == list(
        range(middle + 1, page["cursor"] + 1)
    )


def test_a_cursor_ahead_of_ours_says_reset():
    """The flag that stops a consumer silently missing everything after a restart.

    An empty list without it is indistinguishable from "nothing happened",
    which is the failure this reproduces from the Vehicle ID contract on
    purpose: one consumer, one cursor policy, both surfaces.
    """
    service = LaneService(full_lane())
    service.controller.run_once()
    current = service.events(0).to_dict()["cursor"]

    ahead = service.events(current + 1).to_dict()
    assert ahead["reset"] is True
    assert ahead["events"] == []

    # The control: at the current cursor the list is also empty, and `reset` is
    # false. Without this pair, a `reset` hard-coded either way would pass.
    level = service.events(current).to_dict()
    assert level["reset"] is False
    assert level["events"] == []


def test_skipping_a_cursor_loses_the_events_in_between():
    """The control for the cursor itself: it is a position, not a decoration."""
    service = LaneService(full_lane())
    service.controller.run_once()
    total = service.events(0).to_dict()["cursor"]
    assert total > 2

    skipped = service.events(total - 1).to_dict()
    assert len(skipped["events"]) == 1


def test_dropped_is_exposed_and_the_log_stays_bounded():
    """A gap nobody knows about is worse than one that is counted."""
    queue = EventQueue(max_events=2)
    for index in range(5):
        queue.record("noise", "lane-1", index=index)

    controller = full_lane(events=queue)
    page = LaneService(controller).events(0).to_dict()

    assert queue.dropped == 3
    assert page["dropped"] == 3
    # The outbox is bounded; the read window is separate and still holds them.
    assert queue.pending == 2
    assert len(page["events"]) == 5


def test_flushing_the_outbox_does_not_empty_the_read_window():
    """The two are different things and the contract depends on it.

    `flush()` clears items the platform has taken. A consumer reading
    `GET /v1/lane/events` has not taken them, and a cursor that emptied
    whenever the platform happened to be reachable would mean nothing.
    """
    queue = EventQueue()
    queue.record("armed", "lane-1")
    before = LaneService(full_lane(events=queue)).events(0).to_dict()
    queue.flush()
    after = LaneService(full_lane(events=queue)).events(0).to_dict()

    assert before["events"] == after["events"]
    assert queue.pending == 1  # no transport, so nothing was delivered


# ---------------------------------------------------------------------------
# GUARANTEE 5 — nothing on this contract changes anything
# ---------------------------------------------------------------------------


def test_no_route_mutates_anything():
    """A sweep of the handler, not a list of routes somebody remembered.

    Every `do_*` method other than `do_GET` must BE the one shared refusal.
    A route that did something would have to stop being it, and this goes red
    in the same commit -- which is the point, because the act surface is a
    later round and this is the guard that keeps it one.
    """
    methods = {
        name
        for name in dir(_Handler)
        if name.startswith("do_") and callable(getattr(_Handler, name))
    }
    assert "do_GET" in methods, "the control: this sweep can see methods at all"

    for name in methods - {"do_GET"}:
        assert getattr(_Handler, name) is _Handler._method_not_allowed, (
            f"{name} is not the shared refusal. This contract version is read-only: "
            "a method that does anything is a new route to a vend."
        )


def test_can_vend_is_derived_from_the_route_table():
    """The capability and the routes are one fact, so they cannot disagree."""
    assert ACT_ROUTES == ()
    assert LaneService(full_lane()).capabilities().can_vend is False
    assert LaneService(full_lane()).describe().to_dict()["capabilities"]["can_vend"] is False


def test_a_consumer_that_posts_to_a_vend_path_is_refused():
    """Asked the way a consumer would ask it: over the socket."""
    service = LaneService(full_lane())
    with serving(make_server(service, port=0)) as base:
        consumer = LaneConsumer(base)
        assert consumer.lane()["capabilities"]["can_vend"] is False
        for path in ("/v1/lane/vend", "/v1/lane/transit/abc/resolve", "/v1/lane"):
            assert consumer.post(path) in (404, 405)


def test_the_read_routes_are_the_ones_that_answer():
    """`READ_ROUTES` is not a comment: every one of them serves, and a route
    outside it does not."""
    service = LaneService(full_lane())
    with serving(make_server(service, port=0)) as base:
        consumer = LaneConsumer(base)
        for route in READ_ROUTES:
            assert isinstance(consumer._get(route), dict)
        with pytest.raises(urllib.error.HTTPError) as refused:
            consumer._get("/v1/lane/vend")
        assert refused.value.code == 404


# ---------------------------------------------------------------------------
# The state surface, and the honesty of it
# ---------------------------------------------------------------------------


def test_a_lane_that_has_decided_nothing_says_so():
    """No state store, and the contract does not pretend otherwise."""
    payload = LaneService(full_lane()).state().to_dict()

    assert payload["decision"] is None
    assert payload["transit"] == {"state": "none", "since": None}


def test_a_transit_state_that_is_not_none_must_carry_a_moment():
    Transit(state=TransitState.NONE.value, since=None)
    Transit(state=TransitState.PENDING.value, since="2026-08-30T14:03:11+00:00")

    with pytest.raises(ValueError, match="carries no `since`"):
        Transit(state=TransitState.PENDING.value, since=None)
    with pytest.raises(ValueError, match="no moment"):
        Transit(state=TransitState.NONE.value, since="2026-08-30T14:03:11+00:00")
    with pytest.raises(ValueError, match="no UTC offset"):
        Transit(state=TransitState.HELD.value, since="2026-08-30T14:03:11")


@pytest.mark.parametrize(
    ("crossings", "expected"),
    [
        ([(ClosingSequence.FORWARD, 1.0)], TransitState.CONFIRMED.value),
        ([(ClosingSequence.REVERSE, 1.0)], TransitState.BACKED_OUT.value),
        ([(ClosingSequence.NONE, 1.0)], TransitState.HELD.value),
    ],
)
def test_the_transit_follows_what_the_loops_saw(crossings, expected):
    """Each of the three outcomes reaches the contract as its own state.

    Both sides of every branch, so a transit pinned to one value could not pass.
    """
    controller = full_lane(crossings=crossings)
    controller.run_once()
    payload = LaneService(controller).state().to_dict()

    assert payload["transit"]["state"] == expected
    assert payload["transit"]["since"] is not None


def test_a_lane_with_no_closing_loops_reports_unconfirmable():
    controller = bare_lane()
    controller.run_once()
    payload = LaneService(controller).state().to_dict()

    assert payload["transit"]["state"] == TransitState.UNCONFIRMABLE.value
    assert LaneService(controller).capabilities().confirms_entry is False


def test_the_reason_is_our_code_and_the_fallback_is_derived_from_it():
    """`fallback` is not stored beside `reason`; it is computed from it.

    So the pair cannot come to disagree -- which is what would happen the first
    time one was set and the other was not.
    """
    controller = full_lane(
        identities=[VehicleIdentity(plate=None, confidence=0.0, unavailable=Unavailable.TIMEOUT)],
        crossings=[(ClosingSequence.FORWARD, 1.0)],
    )
    controller.run_once()
    decision = LaneService(controller).state().to_dict()["decision"]

    assert decision["outcome"] == Outcome.FALLBACK.value
    assert decision["reason"] == Fallback.ENGINE_UNREACHABLE.value
    assert decision["fallback"] == Fallback.ENGINE_UNREACHABLE.value
    assert decision["cause"] == Unavailable.TIMEOUT.value
    assert decision["reason"] in REQUIRED_REASONS
    assert decision["cause"] in REQUIRED_CAUSES


def test_a_reason_outside_our_set_leaves_the_fallback_null():
    """The signal that tells a consumer to escalate.

    A third-party lane emits its own vocabulary; `fallback` is `null` there and
    a consumer must not map the reason onto the nearest code it knows.
    """
    ours = LastDecision(
        outcome="fallback", reason=Fallback.LOW_CONFIDENCE.value, at="2026-08-30T00:00:00+00:00"
    )
    theirs = LastDecision(
        outcome="fallback", reason="barrier_operator_intervened", at="2026-08-30T00:00:00+00:00"
    )

    assert ours.fallback == Fallback.LOW_CONFIDENCE.value
    assert theirs.fallback is None


def test_the_required_reason_subset_is_derived_from_the_enum():
    """Never a hand-written list of a set the code defines.

    The round-1 rule, applied to the contract: this is the check that goes red
    when a `Fallback` member is added and a document is not updated, because
    there is no document to update.
    """
    assert FALLBACK_REASONS == tuple(code.value for code in Fallback)
    assert set(REQUIRED_REASONS) == set(FALLBACK_REASONS) | {
        outcome.value for outcome in Outcome if outcome is not Outcome.FALLBACK
    }
    assert REQUIRED_CAUSES == tuple(cause.value for cause in Unavailable)


def test_presence_not_measured_stays_null_all_the_way_out():
    """`null` is not `false`, at every layer including this one."""
    controller = full_lane(
        identities=[VehicleIdentity(plate="SIM-1", confidence=0.99, presence=None)],
        crossings=[(ClosingSequence.FORWARD, 1.0)],
    )
    controller.run_once()
    assert LaneService(controller).state().to_dict()["decision"]["presence"] is None

    refused = full_lane(
        identities=[VehicleIdentity(plate=None, confidence=0.0, presence=False)],
    )
    refused.run_once()
    decision = LaneService(refused).state().to_dict()["decision"]
    assert decision["presence"] is False
    assert decision["outcome"] == Outcome.NO_VEHICLE.value


# ---------------------------------------------------------------------------
# The bind rule
# ---------------------------------------------------------------------------


def test_a_non_loopback_bind_without_a_token_is_refused():
    """Off loopback this publishes where a vehicle was, and when."""
    assert_bind_allowed("127.0.0.1", 8090, None)
    assert_bind_allowed("localhost", 8090, None)
    assert_bind_allowed("0.0.0.0", 8090, "a-token")  # noqa: S104

    for host in ("0.0.0.0", "", "192.0.2.10", "lane-1.local"):  # noqa: S104
        with pytest.raises(InsecureBind):
            assert_bind_allowed(host, 8090, None)


def test_with_a_token_every_route_requires_it():
    service = LaneService(full_lane())
    with serving(make_server(service, port=0, token="s3cret")) as base:
        for route in READ_ROUTES:
            with pytest.raises(urllib.error.HTTPError) as refused:
                LaneConsumer(base)._get(route)
            assert refused.value.code == 401
            assert isinstance(LaneConsumer(base, token="s3cret")._get(route), dict)



# ---------------------------------------------------------------------------
# GUARANTEE 8 — NO ROUTE ON THIS CONTRACT PUBLISHES IDENTITY TEXT
#
# Round 1 stopped the event LOG carrying plate text, and stated the premise
# that made leaving it on the session actions safe: those become
# POST /lane/sessions/open and /close, and nothing they carry is written to
# `events.detail`. Round 2's first draft put every event on a read route, which
# made that sentence false -- a plate the retention purge cannot reach,
# published to every consumer of a READ contract, undeclared.
#
# `tests/test_loops.py` sweeps the event QUEUE, kind by kind, and derives its
# kind list from SESSION_KINDS -- so by construction it cannot see a session
# action reaching a route. This is the same question asked of the SURFACE: what
# a consumer receives, on all four routes, serialised.
# ---------------------------------------------------------------------------

PLATE_ON_THE_WIRE = "PURGEME9"


def a_lane_that_saw_a_car(events=None):
    """The standard installation, a real plate, a CONFIRMED entry.

    Confirmed on purpose: it is the path that records a session action, which
    is the one event in this package that has ever carried a plate.
    """
    controller = full_lane(
        identities=[
            VehicleIdentity(
                plate=PLATE_ON_THE_WIRE, plate_region="TR", confidence=0.97, presence=True
            )
        ],
        crossings=[(ClosingSequence.FORWARD, 3.0)],
        events=events,
    )
    controller.run_once()
    return controller


def every_route_serialised(service) -> dict[str, str]:
    """What a consumer receives from all four routes, as it receives it: text.

    Serialised rather than walked, so a plate reaches this whether it is a
    value, a key, or interpolated into a sentence -- and so nothing has to
    remember which fields exist.
    """
    return {
        "/v1/lane": json.dumps(service.describe().to_dict()),
        "/v1/lane/state": json.dumps(service.state().to_dict()),
        "/v1/lane/health": json.dumps(service.health().to_dict()),
        "/v1/lane/events": json.dumps(service.events(0).to_dict()),
    }


def test_no_route_publishes_identity_text():
    """The plate is on none of the four routes, and the sweep can see one.

    The control is the second half and it is not optional: this is an ABSENCE
    claim about a search, so the search is run against something known to be
    there and required to find it. Without that, a sweep looking at the wrong
    object passes for the wrong reason.
    """
    service = LaneService(a_lane_that_saw_a_car())
    served = every_route_serialised(service)

    for route, body in served.items():
        assert PLATE_ON_THE_WIRE not in body, f"{route} published plate text: {body}"

    # THE CONTROL, per route: the same sweep run over the same payload with a
    # plate planted in it must find one. Run route by route rather than once,
    # because a control on the events page says nothing about whether the
    # health page was ever actually searched.
    for route, payload in (
        ("/v1/lane", service.describe().to_dict()),
        ("/v1/lane/state", service.state().to_dict()),
        ("/v1/lane/health", service.health().to_dict()),
        ("/v1/lane/events", service.events(0).to_dict()),
    ):
        planted = json.dumps({**payload, "planted": PLATE_ON_THE_WIRE})
        assert PLATE_ON_THE_WIRE in planted, (
            f"the sweep cannot see a plate planted in {route}'s payload, so its "
            "absence there says nothing"
        )

    # And the lane really did handle that vehicle, so the run is not vacuous:
    # a lane that decided nothing publishes no identity either.
    assert service.controller.last_decision is not None
    assert service.controller.events.pending_sessions == 1


def test_the_read_window_holds_no_session_actions():
    """Session actions are the ledger's. They are not on this surface.

    The platform is where they go and where they are durable. Serving them here
    would publish the plate on a read contract, and `detail` is declared opaque
    -- so the one identity field on the surface would be the field nothing
    describes and nothing checks.
    """
    controller = a_lane_that_saw_a_car()
    kinds_on_the_wire = {
        item["kind"] for item in LaneService(controller).events(0).to_dict()["events"]
    }

    assert not (kinds_on_the_wire & SESSION_KINDS), (
        f"session action(s) on the read contract: {sorted(kinds_on_the_wire & SESSION_KINDS)}"
    )
    # The control, and the point: the lane DID open a session, and it is in the
    # outbox where it belongs. Without this the test would pass on a lane that
    # simply never transacted.
    outbox_kinds = {event.kind for event in controller.events._queue}
    assert SESSION_OPEN_KIND in outbox_kinds, "no session was opened, so this asserts nothing"
    assert controller.events.pending_sessions == 1


def test_the_cursor_counts_only_what_the_window_serves():
    """A session action takes no cursor number, so no gap hints at one.

    If session actions consumed cursors while being withheld, a consumer could
    read the gaps and know exactly how many happened and when -- which is
    publishing the fact while pretending not to.
    """
    page = LaneService(a_lane_that_saw_a_car()).events(0).to_dict()
    served = [item["cursor"] for item in page["events"]]

    assert served == list(range(1, page["cursor"] + 1)), (
        f"the cursor is not contiguous over what is served: {served}"
    )


# ---------------------------------------------------------------------------
# GUARANTEE 9 — the read window reports its own eviction
#
# The window is 256 deep. A consumer further behind than that receives the
# events that survived, with the evicted ones simply absent -- which is
# indistinguishable from a complete page. `dropped` does not cover it: that
# counts what the OUTBOX discarded, a different queue with a different bound.
#
# The Vehicle ID service has the same window and does not report this, because
# its contract tells a consumer that needs guaranteed delivery to use push, and
# it HAS push. This contract has neither.
# ---------------------------------------------------------------------------


def test_a_cursor_behind_the_window_says_reset():
    """306 recorded, 256 served: the stale cursor is told, the fresh one is not.

    Both directions in one test on purpose. A `reset` hard-coded true would
    satisfy the first assertion and a hard-coded false the second, and the pair
    is what makes either of them evidence.
    """
    queue = EventQueue(history=256)
    for index in range(306):
        queue.record("noise", "lane-1", index=index)
    service = LaneService(full_lane(events=queue))

    assert queue.oldest == 51, "the window did not evict, so this measures nothing"

    stale = service.events(0).to_dict()
    assert stale["reset"] is True, "a consumer at 0 missed events 1-50 and was not told"
    assert len(stale["events"]) == 256
    # It is still SERVED what survived -- `reset` is a warning, not a refusal.
    assert stale["events"][0]["cursor"] == 51

    # Inside the window: nothing was missed, so nothing is claimed.
    fresh = service.events(200).to_dict()
    assert fresh["reset"] is False
    assert [item["cursor"] for item in fresh["events"]] == list(range(201, 307))

    # The exact boundary, which is where an off-by-one lives: a consumer whose
    # cursor is 50 has event 51 next, and 51 is still held. Nothing was missed.
    assert service.events(50).to_dict()["reset"] is False
    assert service.events(49).to_dict()["reset"] is True


def test_an_unevicted_window_never_claims_a_reset():
    """The other control: `reset` stays false for a window that lost nothing."""
    service = LaneService(a_lane_that_saw_a_car())
    page = service.events(0).to_dict()

    assert page["reset"] is False
    assert page["dropped"] == 0
    assert len(page["events"]) == page["cursor"]


def test_the_window_depth_is_published_and_is_the_windows_own():
    """`event_window_depth` is read off the deque, not typed beside it.

    A lane built with a different window publishes the different number. That
    is the whole reason it is on the payload rather than in the document: a
    document can only describe one lane's.
    """
    default = LaneService(full_lane()).describe().to_dict()
    assert default["event_window_depth"] == DEFAULT_HISTORY

    narrow = LaneService(full_lane(events=EventQueue(history=8))).describe().to_dict()
    assert narrow["event_window_depth"] == 8, (
        "the published depth did not follow the window, so it is a constant"
    )


# ---------------------------------------------------------------------------
# GUARANTEE 10 — `outbox_depth_growing` reads the DEPTH
#
# It used to read `dropped`: the count of events ALREADY LOST. So a lane with
# nine thousand undelivered events behind a dead platform answered `ok`, with
# `source: measured`, right up to the moment the bounded log began throwing
# entries away. `measured` was true -- of something else -- so the
# `HealthEntry` guard could not catch it.
# ---------------------------------------------------------------------------


def test_the_outbox_depth_is_what_is_measured_not_the_drop_count():
    """Past the threshold with delivery blocked is `active`; under it is `ok`.

    `dropped` stays 0 throughout, so this cannot pass by reading the counter it
    replaced. That is the positive control, and it is the whole test: the old
    code answers `ok` at every line below.
    """
    threshold = 5
    controller = full_lane()
    controller.config = replace(controller.config, outbox_depth_threshold=threshold)
    service = LaneService(controller)
    state = lambda: {  # noqa: E731
        entry["code"]: entry for entry in service.health().to_dict()["codes"]
    }[MalfunctionCode.OUTBOX_DEPTH_GROWING.value]

    # No transport is wired, so nothing drains: everything recorded stays
    # pending. This is a lane that cannot reach its platform.
    for index in range(threshold):
        controller.events.record("noise", "lane-1", index=index)
    assert controller.events.pending == threshold
    assert state()["state"] == "ok"
    assert state()["source"] == "measured"

    controller.events.record("noise", "lane-1", index=threshold)
    assert controller.events.pending == threshold + 1
    assert state()["state"] == "active", "the depth grew past the threshold and nothing said so"

    # The counter the code used to read never moved. A build reading `dropped`
    # reports `ok` on both lines above and on this one.
    assert controller.events.dropped == 0


def test_nine_thousand_undelivered_events_are_not_ok():
    """The L3's probe, at the size it was found at.

    A lane holding nine thousand events it could not deliver is a lane that has
    been unable to reach its platform for a long time, and the code whose name
    is `outbox_depth_growing` is the one an operator would use to find that
    out. It answered `ok`.
    """
    controller = full_lane()
    for index in range(9_000):
        controller.events.record("noise", "lane-1", index=index)

    entry = {
        e["code"]: e for e in LaneService(controller).health().to_dict()["codes"]
    }[MalfunctionCode.OUTBOX_DEPTH_GROWING.value]

    assert controller.events.pending == 9_000
    assert controller.events.dropped == 0
    assert entry["state"] == "active"
    assert entry["source"] == "measured"


def test_the_threshold_is_a_site_setting_with_a_published_default():
    """It comes from the configuration, and a file may say otherwise."""
    assert LaneConfig(
        lane_id="l", site_id="s", camera=CameraConfig(camera_id="c", rtsp_url="")
        , gate=GateConfig()
    ).outbox_depth_threshold == DEFAULT_OUTBOX_DEPTH_THRESHOLD

    # A depth of zero would report the fault permanently, which says as little
    # as reporting nothing.
    with pytest.raises(ValueError, match="positive integer"):
        LaneConfig(
            lane_id="l", site_id="s", camera=CameraConfig(camera_id="c", rtsp_url=""),
            gate=GateConfig(), outbox_depth_threshold=0,
        )


# ---------------------------------------------------------------------------
# GUARANTEE 11 — the document publishes the same VALUES the code holds
#
# Guarantee 1 compares the SHAPE: `shape()` discards every leaf. That is the
# right comparison for a cursor, a timestamp or a lane id, which move. It is
# the wrong one for a value that is a CONSTANT OF THE CODE -- and those are
# precisely the claims this contract exists to make.
#
# With the shape check alone, `docs/CONTRACT.md` could publish `can_vend: true`
# on a lane that cannot vend, `contract_version: 99`, and a
# `reference_not_recognised` row saying it is a measured, healthy, alarmable
# code a technician should be paged on -- with the whole suite green. Those are
# the two claims of the round, hand-written into the page an integrator reads
# before writing a line.
#
# The expectation below is DERIVED from the live payload, never from a second
# copy of the assertion.
# ---------------------------------------------------------------------------


def live_payloads() -> dict[str, dict]:
    """The four payloads, built by a real lane. The source of every expectation."""
    controller = full_lane()
    controller.run_once()
    service = LaneService(controller)
    return {
        "lane": service.describe().to_dict(),
        "state": service.state().to_dict(),
        "health": service.health().to_dict(),
        "events": service.events(0).to_dict(),
    }


def test_the_documents_contract_version_is_the_codes():
    """Every ROUTE example carries it, and it is the constant, not a number.

    The closed-set block is not a route's payload and carries no version: it
    publishes the sets the routes are made of, and stamping a version on it
    would be a second copy of one.
    """
    doc = doc_payloads()
    carried = {name: doc[name]["contract_version"] for name in sorted(ROUTE_PAYLOADS)}

    assert set(carried) == ROUTE_PAYLOADS, "an example dropped contract_version"
    assert len(carried) == 4, f"the sweep found {len(carried)} payloads, not four"
    for name, version in carried.items():
        assert version == CONTRACT_VERSION, (
            f"docs/CONTRACT.md's `{name}` example publishes contract_version "
            f"{version!r}; the code's is {CONTRACT_VERSION!r}"
        )


def test_the_documents_can_vend_is_the_route_tables_answer():
    """`can_vend` is derived from `ACT_ROUTES`. The document may not disagree.

    This is the claim the round is FOR -- that the read contract cannot open a
    barrier -- and until now the document could have published the opposite of
    what the code enforces, with CI green.
    """
    doc = doc_payloads()["lane"]["capabilities"]
    live = live_payloads()["lane"]["capabilities"]

    assert doc["can_vend"] == live["can_vend"] == bool(ACT_ROUTES) is False


def test_the_documents_health_row_publishes_the_codes_own_constants():
    """`source`, `never_alarm` and `caveat` are constants of the code.

    They come from `SOURCES` and `NEVER_ALARM`, one mapping each. The document
    shows a row; that row's constant fields are compared against the LIVE entry
    for the same code, so a rewritten caveat, a flipped flag or a moved source
    label goes red.

    `state` is deliberately NOT compared: it is derived per run and is not a
    constant. Comparing it would be comparing a measurement to a fixed string.
    """
    doc_rows = doc_payloads()["health"]["codes"]
    live_rows = {row["code"]: row for row in live_payloads()["health"]["codes"]}

    assert doc_rows, "the health example shows no rows, so this asserts nothing"
    for row in doc_rows:
        code = row["code"]
        assert code in live_rows, (
            f"docs/CONTRACT.md shows `{code}`, which is not a code this build ships"
        )
        live = live_rows[code]
        for field_name in ("source", "never_alarm", "caveat"):
            assert row[field_name] == live[field_name], (
                f"docs/CONTRACT.md's `{code}` row publishes {field_name}="
                f"{row[field_name]!r}; the code holds {live[field_name]!r}"
            )

    # The control on the loop above: at least one row shown is one whose
    # constants are not all the boring default, so a comparison that only ever
    # met `never_alarm: false, caveat: null` would not count as exercised.
    assert any(row["never_alarm"] for row in doc_rows), (
        "the health example shows no never_alarm code, so the flag and the "
        "caveat are never actually compared"
    )


#: Where the document quotes a value out of a CLOSED SET the code defines. The
#: sets themselves are derived from the code; this maps which field carries
#: which. A path that stops resolving fails rather than silently skipping --
#: see the control in the test below.
CLOSED_SET_FIELDS = {
    ("lane", "direction"): ("entry", "exit"),
    ("state", "decision", "outcome"): OUTCOMES,
    ("state", "decision", "reason"): REQUIRED_REASONS,
    ("state", "decision", "fallback"): FALLBACK_REASONS,
    ("state", "decision", "cause"): REQUIRED_CAUSES,
    ("state", "transit", "state"): tuple(state.value for state in TransitState),
    ("health", "codes", "code"): tuple(code.value for code in MalfunctionCode),
    ("health", "codes", "state"): tuple(state.value for state in HealthState),
    ("health", "codes", "source"): tuple(source.value for source in Source),
}


def _at(payload, path):
    """Every value at `path`, descending into lists. `KeyError` if it is gone."""
    if not path:
        return [payload]
    key, rest = path[0], path[1:]
    if isinstance(payload, list):
        return [value for item in payload for value in _at(item, path)]
    return _at(payload[key], rest)


def test_every_closed_set_value_in_the_document_is_a_member_of_that_set():
    """A doc example may not quote a value the code would refuse.

    The shape check compares keys, so `"outcome": "explode"` and
    `"source": "probably_fine"` both passed. These sets are what a consumer
    branches on; a document showing a value outside one teaches an integrator a
    case that will never arrive, or worse, one the lane would reject.
    """
    doc = doc_payloads()
    for (name, *path), allowed in CLOSED_SET_FIELDS.items():
        values = _at(doc[name], path)
        # The control for this path: if the field were renamed or dropped, the
        # loop would iterate over nothing and the check would evaporate.
        assert values, f"docs/CONTRACT.md's `{name}` example has no {'.'.join(path)}"
        for value in values:
            assert value is None or value in allowed, (
                f"docs/CONTRACT.md's `{name}` example publishes "
                f"{'.'.join(path)}={value!r}, which is not in {allowed}"
            )


# ---------------------------------------------------------------------------
# `read_ref` RESOLVES. Attached from the L3's note: nothing in the suite has
# ever seen a non-null one -- the stub identifier does not set it, the doc
# example is `null`, the third-party lane's is `null`. A field whose only
# tested value is null is a field nobody has proven carries anything.
# ---------------------------------------------------------------------------


def test_read_ref_on_the_state_route_is_the_engines_own_read_id():
    """Driven against a real `vehicle_id.contract.Read`, through a real client.

    "Resolves" means what the Vehicle ID contract can support: the value equals
    the `read_id` of a record the consumer has already received. That engine
    publishes no lookup-by-id route -- `GET /v1/reads/last` and
    `GET /v1/reads?since=N` are the ways to meet one -- so this does not claim
    the reference can be fetched, only that it identifies the read the decision
    was made from.
    """
    read = Read(
        read_id="9bd13905ac6840d499652c156a95b1c9",
        captured_at="2026-08-30T14:03:11.482913+00:00",
        camera_id="lane-1",
        identity=Identity(plate="SIM-0001"),
        confidence=0.995,
        engine=Engine(name="test", version="0.1.0", weights_id="sha256:abc"),
        threshold_applied=0.99,
        outcome=ANSWER,
    )
    controller = full_lane()
    controller.identifier = VehicleIdClient(
        opener=lambda url, body, timeout: {"cursor": 1, "read": read.to_dict()}
    )
    controller.run_once()

    decision = LaneService(controller).state().to_dict()["decision"]

    assert decision["read_ref"] == read.read_id
    assert decision["read_ref"] is not None
    # And it is provenance, not identity: the plate the engine read is not on
    # this route, and `read_ref` is not a rendering of it.
    assert "SIM-0001" not in json.dumps(decision)


def test_an_identifier_that_names_no_read_publishes_a_null_read_ref():
    """The control for the test above: the field is not unconditionally set.

    A lane identifying some other way has no `read_id` to publish, and `null`
    is the honest answer. Without this pair, a `read_ref` hard-wired to a
    constant would satisfy the assertion above.
    """
    controller = full_lane(
        identities=[VehicleIdentity(plate="SIM-0001", confidence=0.97, presence=True)]
    )
    controller.run_once()

    assert LaneService(controller).state().to_dict()["decision"]["read_ref"] is None


# ---------------------------------------------------------------------------
# The CLI's credential flag is the engine's, spelt the same way.
# ---------------------------------------------------------------------------


def test_the_token_flag_is_spelt_the_way_vehicle_id_spells_it():
    """`--auth-token-file`, not `--token-file`.

    One concept, two services on one Jetson, and an installer or a supervisor
    unit configuring both had to use two different flag names for it. The
    divergence was drift, not a decision -- it failed loudly (argparse rejects
    an unknown flag), which is why it is a reconciliation and not a defect.
    """
    parser = build_parser()
    args = parser.parse_args(
        ["serve", "--config", "lane.toml", "--auth-token-file", "tok"]
    )
    assert args.auth_token_file == Path("tok")

    with pytest.raises(SystemExit):
        parser.parse_args(["serve", "--config", "lane.toml", "--token-file", "tok"])


def test_the_plate_region_is_still_on_the_events_route_and_this_round_did_not_decide_it():
    """A FACT, pinned so it goes red the day it changes. Not a guarantee.

    `entry_pending` carries `plate_region` in its detail, so an attribute of a
    vehicle's identity -- not the plate -- is on `GET /v1/lane/events`. Three
    things about it, all checked or checkable rather than asserted:

      * it is PRE-EXISTING. `git grep plate_region 229174f -- src` finds it at
        the merge-base of this branch, at `controller.py:318`. This round did
        not put it on the surface; this round put a surface under it.
      * it is LOAD-BEARING. `sync.py` reads `event.detail["plate_region"]` off
        exactly this event to compose the platform's session open, so deleting
        it is a mechanism change with a dependent, not a deletion.
      * nothing decided it. The round-2 brief named the PLATE and this test
        file's guarantee 8 covers the plate. Whether an identity ATTRIBUTE
        belongs on a read contract is a live question and this round is not
        where it gets answered.

    So this is the deferral in its only honest form: not a note in a receipt
    that the next touch is expected to remember, but a check that fails the
    moment the state it describes stops holding -- which is precisely when
    somebody should be looking at it.
    """
    controller = a_lane_that_saw_a_car()
    page = LaneService(controller).events(0).to_dict()
    regions = {
        item["kind"]: item["detail"]["plate_region"]
        for item in page["events"]
        if "plate_region" in item["detail"]
    }

    assert regions == {"entry_pending": "TR"}, (
        "the plate region's place on the read surface changed. That is a "
        "decision about identity on a read contract -- make it deliberately, "
        "and update this test with the reasoning, rather than letting it move"
    )
    # And the plate itself is still absent, which is what guarantee 8 holds.
    assert PLATE_ON_THE_WIRE not in json.dumps(page)


# ---------------------------------------------------------------------------
# GUARANTEE 11 — the two codes this round CLOSED, and the ones it did not
#
# Closing a `not_measured` code is reading something that already exists.
# `identity_service_degraded` is the identification service's own `status`;
# `clock_skew_rejected` is the platform's own name for the refusal it already
# sends. Neither invents a signal, and the codes this round could NOT close are
# fenced by the test at the end of this section rather than left to a sentence.
# ---------------------------------------------------------------------------


class _AnIdentifierWithHealth:
    """An identifier that publishes a health route, as the Vehicle ID one does.

    Not a `VehicleIdClient`: the service asks by CAPABILITY, so a third party's
    identifier publishing the same route is read exactly the way ours is. If
    this had to be our class, the derivation would be a special case for us.
    """

    def __init__(self, body):
        self.body = body

    def identify(self, frames):
        return VehicleIdentity(plate=None, confidence=0.0)

    def identity_health(self):
        return self.body


def _health_of(controller):
    return {e["code"]: e for e in LaneService(controller).health().to_dict()["codes"]}


def test_the_derived_codes_are_exactly_the_ones_sources_calls_measured():
    """The half of the invariant `HealthEntry` cannot see.

    `HealthEntry` refuses a code that claims `ok` without being `measured`. The
    reverse has no guard: a code promoted in `SOURCES` that nothing derives
    answers `unknown` for ever, and `unknown` from a code labelled `measured`
    reads as "asked, and could not tell" rather than as "never wired up".

    Both sides are derived here -- the label from `SOURCES`, the derivation from
    the service -- so neither can be a hand-written list of the other.
    """
    derived = set(LaneService(full_lane()).derived_states())
    labelled = {code for code, source in SOURCES.items() if source is Source.MEASURED}

    assert derived == labelled, (
        f"derived but not labelled measured: {sorted(c.value for c in derived - labelled)}; "
        f"labelled measured but not derived: {sorted(c.value for c in labelled - derived)}"
    )
    # The control: the comparison is not two empty sets agreeing.
    assert derived


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"status": "degraded", "schema_version": SCHEMA_VERSION}, "active"),
        ({"status": "ok", "schema_version": SCHEMA_VERSION}, "ok"),
        # Unreadable, and unreadable is NOT a clean bill of health. Whether the
        # service is unreachable is a different code from a different signal.
        (None, "unknown"),
        # It answered, and said nothing this build recognises.
        ({"schema_version": SCHEMA_VERSION}, "unknown"),
        ({"status": "probably fine", "schema_version": SCHEMA_VERSION}, "unknown"),
        # A body that is not an object at all.
        ("degraded", "unknown"),
    ],
)
def test_identity_service_degraded_is_the_engines_own_status(body, expected):
    """A straight read of the field that service already publishes.

    `degraded` is set when a read was LOST -- it could not be written to the
    push queue at all -- or when the queue held a line the engine could not
    read. Both are records that were answered and then existed nowhere, and
    until this round nothing on this side listened.
    """
    controller = full_lane()
    controller.identifier = _AnIdentifierWithHealth(body)

    entry = _health_of(controller)[MalfunctionCode.IDENTITY_SERVICE_DEGRADED.value]
    assert entry["state"] == expected
    assert entry["source"] == Source.MEASURED.value


def test_an_identifier_with_no_health_route_leaves_the_code_unknown():
    """The control for the case set above: the answer is not unconditional.

    A lane identifying some other way has no such route to ask, and `unknown` is
    the honest answer. Without this, a derivation hard-wired to `ok` would
    satisfy every row of the table above that expects `ok`.
    """
    controller = full_lane()
    assert not hasattr(controller.identifier, "identity_health")

    entry = _health_of(controller)[MalfunctionCode.IDENTITY_SERVICE_DEGRADED.value]
    assert entry["state"] == "unknown"


# --- the platform's named refusal ------------------------------------------


def _refusing_transport(status=409, body='{"error": "no", "code": "stale_exit"}'):
    """A transport whose platform refuses everything, with a body we choose.

    Built on the REAL `PlatformClient` and the real `PlatformTransport`, so the
    refusal travels the path a refusal travels: `urllib` raises `HTTPError`, the
    client classifies it and parses the body, and `_guarded` counts it. A fake
    that raised `PlatformRejected` directly would skip the parsing, which is the
    part this round added.
    """

    def opener(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, status, "refused", {}, io.BytesIO(body.encode("utf-8"))
        )

    return PlatformTransport(PlatformClient("http://platform.invalid", "t", opener=opener))


def _a_lane_that_talked_to_a_platform(transport):
    """A lane that has SENT something, so its outbox has attempted a delivery."""
    controller = a_lane_that_saw_a_car(events=EventQueue(transport=transport))
    controller.events.flush()
    return controller


def test_clock_skew_rejected_reads_the_platforms_own_name_for_the_refusal():
    """`active` on the named skew, `ok` on a named refusal that is not one.

    A 409 is the platform's terminal refusal and seven conditions produce one.
    Six are ordinary. The seventh dead-letters every session open and close this
    lane sends -- the barrier still works, the driver still gets in, and the
    money record silently loses the stay.
    """
    skew = _a_lane_that_talked_to_a_platform(
        _refusing_transport(body='{"error": "ahead of the clock", "code": "clock_skew"}')
    )
    assert _health_of(skew)[MalfunctionCode.CLOCK_SKEW_REJECTED.value]["state"] == "active"
    # One vehicle produces more than one platform call -- a session open and a
    # batched log post -- and this refusal answers both, so the count is
    # "at least one" rather than a number that depends on how many events a
    # lane happened to have queued.
    assert skew.events.transport.skew_rejected > 0

    # The control, and the whole point of the field: another 409 is not this
    # one. Without this row a code hard-wired to `active` on any refusal would
    # pass the assertion above.
    other = _a_lane_that_talked_to_a_platform(
        _refusing_transport(body='{"error": "stale exit", "code": "stale_exit"}')
    )
    assert _health_of(other)[MalfunctionCode.CLOCK_SKEW_REJECTED.value]["state"] == "ok"
    assert other.events.transport.skew_rejected == 0
    # It was still dead-lettered. `ok` here is about the CLOCK, not about the
    # item, and the code that counts the loss is a different one.
    assert other.events.transport.rejected > 0


def test_a_conflict_the_platform_did_not_name_is_unknown_and_never_ok():
    """A platform too old to name its refusals refuses a skew like everything else.

    So the absence of a name may not be read as "not a skew". This is the row
    that decides whether the code is honest on the deployment where the failure
    is invisible, and `ok` there would be a healthy clock reported by something
    that cannot see the clock.
    """
    unnamed = _a_lane_that_talked_to_a_platform(_refusing_transport(body='{"error": "no"}'))

    assert _health_of(unnamed)[MalfunctionCode.CLOCK_SKEW_REJECTED.value]["state"] == "unknown"
    assert unnamed.events.transport.conflicts_unnamed > 0
    assert unnamed.events.transport.skew_rejected == 0


def test_a_refusal_that_is_not_a_conflict_does_not_muddy_the_clock():
    """A 400 is a malformed request. It cannot be a skew and is not counted.

    Without the status check, an ordinary bad request would be an unnamed
    conflict, and the code would answer `unknown` -- a clock reported as
    unmeasurable because a plate was missing from one call.
    """
    bad_request = _a_lane_that_talked_to_a_platform(
        _refusing_transport(status=400, body='{"error": "plate is required"}')
    )

    assert _health_of(bad_request)[MalfunctionCode.CLOCK_SKEW_REJECTED.value]["state"] == "ok"
    assert bad_request.events.transport.conflicts_unnamed == 0
    assert bad_request.events.transport.rejected > 0


def test_the_code_comes_from_the_field_and_not_from_the_message():
    """The message says `clock_skew` and the field says otherwise. The field wins.

    Every failure classification in this package is decided from a structure
    rather than from message text, because a message gets reworded and a check
    keyed on its words goes quietly wrong. This is that rule, asserted where it
    is newest.
    """
    lying = _a_lane_that_talked_to_a_platform(
        _refusing_transport(body='{"error": "clock_skew clock skew!", "code": "stale_exit"}')
    )

    assert _health_of(lying)[MalfunctionCode.CLOCK_SKEW_REJECTED.value]["state"] == "ok"
    assert lying.events.transport.skew_rejected == 0


def test_a_body_that_is_not_json_is_an_unnamed_conflict_and_never_an_exception():
    """This parse runs on the failure path of every platform call.

    A parse error here would replace a refusal the lane knows how to survive
    with an exception it does not -- on the path that runs after the barrier has
    already opened.
    """
    for body in ("<html>502 nope</html>", "", "[1, 2, 3]", '{"code": 7}'):
        lane = _a_lane_that_talked_to_a_platform(_refusing_transport(body=body))
        assert _health_of(lane)[MalfunctionCode.CLOCK_SKEW_REJECTED.value]["state"] == "unknown"
        assert lane.events.transport.conflicts_unnamed > 0


def test_a_lane_that_has_attempted_nothing_has_not_measured_its_clock():
    """Two lanes that have never been refused, and they are not the same lane.

    One has sent nothing at all: nothing could have been refused, so nothing was
    measured. `ok` there is a confident negative about a question nobody asked,
    and it is the answer this code would have given without the attempt counter.
    """
    # Built and never run, so nothing has been handed to the platform at all.
    # `a_lane_that_saw_a_car` flushes as part of handling the vehicle, which is
    # the other lane in this test.
    silent = full_lane(events=EventQueue(transport=_refusing_transport()))
    assert silent.events.transport.attempted == 0
    assert _health_of(silent)[MalfunctionCode.CLOCK_SKEW_REJECTED.value]["state"] == "unknown"

    # And a lane with no platform at all: nothing to be refused by.
    standalone = full_lane()
    assert standalone.events.transport is None
    assert _health_of(standalone)[MalfunctionCode.CLOCK_SKEW_REJECTED.value]["state"] == "unknown"


# --- the deletion, proven ---------------------------------------------------


def _not_measured_paragraph() -> str:
    """The document's list of where the `not_measured` signals live.

    The heading sentence and the bullet list under it -- two blocks, because a
    Markdown list is separated from its own lead-in by a blank line and taking
    only as far as the first one would return the heading and nothing else.
    """
    text = CONTRACT_DOC.read_text(encoding="utf-8")
    marker = "Where the `not_measured` signals live today"
    blocks = text[text.index(marker) :].split("\n\n")
    return "\n\n".join(blocks[:2])


def test_the_documents_not_measured_list_is_exactly_what_sources_says():
    """Both directions, derived from `SOURCES`, against the list a reader uses.

    The list exists to tell a reader what is waiting to be READ rather than
    built. A code still on it after the read has been written sends somebody to
    write code that is already there; a code missing from it is a signal nobody
    can find. Both are the kind of stale sentence that survives every review,
    because each was true when it was written.

    Naming every code in the list rather than describing them in groups is what
    makes this mechanical -- a group heading cannot be compared to an enum.
    """
    paragraph = _not_measured_paragraph()
    named = {code for code in MalfunctionCode if code.value in paragraph}
    listed = {code for code, source in SOURCES.items() if source is Source.NOT_MEASURED}

    assert named == listed, (
        f"named in docs/CONTRACT.md but not `not_measured`: "
        f"{sorted(c.value for c in named - listed)}; "
        f"`not_measured` but not named: {sorted(c.value for c in listed - named)}"
    )
    # The control: neither set is empty, so this is not two blanks agreeing.
    assert named and listed


def test_the_codes_this_round_did_not_close_are_still_unmeasured_and_say_why():
    """A FACT, pinned so it goes red the day it changes. Not a guarantee.

    Four codes were candidates this round and none of them could be closed by a
    READ, each for a reason that is in the document beside it:

      * `camera_feed_lost` and `lens_obstructed_or_dark` -- the engine publishes
        `camera_faults`, which is a COUNT SINCE START. A count is not a state: a
        camera that failed at 3am and was fixed at 4am reports the same number
        for ever. Deriving `active` from it would need a rate over a window, and
        nobody has measured what rate is a fault.
      * `arming_loops_disagree` and `closing_loops_never_firing` -- the lane
        writes an event per vehicle already; the fault is a RUN of them, and
        nobody has measured how many in a row a run is.

    If one of these is closed, this test is what tells you to come and say so.
    """
    still_open = {
        MalfunctionCode.CAMERA_FEED_LOST,
        MalfunctionCode.LENS_OBSTRUCTED_OR_DARK,
        MalfunctionCode.ARMING_LOOPS_DISAGREE,
        MalfunctionCode.CLOSING_LOOPS_NEVER_FIRING,
    }
    for code in still_open:
        assert SOURCES[code] is Source.NOT_MEASURED, (
            f"{code.value} changed source. That is a decision -- record why it could be read, "
            "and update this test with it, rather than letting it move"
        )


# ---------------------------------------------------------------------------
# GUARANTEE 12 — THE CONTRACT IS IMPLEMENTABLE FROM THE DOCUMENT
#
# The document used to say it did not list the codes, on the reasoning that a
# hand-written copy of a set the code defines is the copy that goes wrong. The
# reasoning is right; the conclusion moved the copy into every implementer's
# guess instead. Five of twenty-one codes appeared anywhere in it, none of the
# four outcomes a consumer is told to branch on, and the stub in this repository
# that exists to prove a stranger can take this seat imported our Python package
# for exactly the sets the document withheld.
#
# So the sets are published, and the copy is held to the code HERE: every member
# compared against its enum, in both directions. Dropping one from the document
# goes red, and so does adding one to an enum without adding it to the document.
# ---------------------------------------------------------------------------


#: Every closed set the document publishes in full, keyed to where it comes
#: from. Derived from the enums, never typed: a hand-written expectation here
#: would be a third copy, and the third copy lies too.
PUBLISHED_SETS = {
    "malfunction_codes": lambda: [code.value for code in MalfunctionCode],
    "outcomes": lambda: list(OUTCOMES),
    "transit_states": lambda: [state.value for state in TransitState],
    "never_alarm": lambda: [code.value for code in NEVER_ALARM],
}


def test_the_document_publishes_every_member_of_every_closed_set():
    """Both directions, per set, against the enum the set comes from.

    A missing member is a lane an implementer cannot write; an extra one is a
    value the code would refuse, taught to somebody as though it arrived.
    Compared as ORDERED lists, because the document is what a person reads and a
    set that arrives shuffled every time it is regenerated produces a diff on
    every touch.
    """
    published = doc_payloads()["sets"]

    assert set(published) == set(PUBLISHED_SETS), (
        "the document's closed-set block and the sets this test knows about have "
        f"diverged: {sorted(published)} vs {sorted(PUBLISHED_SETS)}"
    )
    for name, from_the_code in PUBLISHED_SETS.items():
        expected = from_the_code()
        assert published[name] == expected, (
            f"docs/CONTRACT.md publishes {name}={published[name]}; the code holds {expected}"
        )
        # The control for this row: the comparison is not two empty lists
        # agreeing, which is what a renamed key would reduce it to.
        assert expected, f"{name} is empty, so comparing it proves nothing"


def test_the_published_code_set_is_the_one_every_health_payload_ships():
    """The set in the document and the set on the wire are the same set.

    The test above compares the document to an ENUM. This compares it to a
    served payload, so a document that agreed with an enum nothing shipped would
    still go red.
    """
    published = doc_payloads()["sets"]["malfunction_codes"]
    served = [entry["code"] for entry in live_payloads()["health"]["codes"]]

    assert sorted(published) == sorted(served)
    assert len(served) == len(set(served)), "a code ships twice"


def test_the_document_no_longer_withholds_the_code_list():
    """The deletion, proven, with the control that the search can see this file.

    An absence claim is a claim about a SEARCH. So the sentence that used to
    withhold the codes is searched for and must be gone, and a sentence that is
    still there is searched for with the same reader and must be found.
    """
    text = CONTRACT_DOC.read_text(encoding="utf-8")

    assert "This document does not list the codes" not in text
    # THE CONTROL: the same read of the same file finds text that IS there, so
    # "not in text" is a fact about the document rather than about the read.
    assert "One entry per member of `contract.MalfunctionCode`" in text
    assert "<!--payload:sets-->" in text


def test_never_alarm_is_a_boolean_on_every_entry_of_every_response():
    """The field the monitor is now required to read as a boolean, from OUR lane.

    The reader refuses a payload whose `never_alarm` is absent or is not a
    boolean -- absent could be a lane with nothing to say or a serialiser that
    dropped it, and the two point in opposite directions. This is the half of
    that agreement that belongs here: our own lane emits one, of the right type,
    on every entry of every response.
    """
    served = live_payloads()["health"]["codes"]
    assert served, "the health payload carried no codes, so this asserts nothing"
    for entry in served:
        assert "never_alarm" in entry, f"{entry['code']} ships without never_alarm"
        assert isinstance(entry["never_alarm"], bool), (
            f"{entry['code']} ships never_alarm={entry['never_alarm']!r}, which is not a boolean"
        )
        assert isinstance(entry["state"], str)
    # The control: both values of the flag really occur, so a payload that
    # hard-coded one of them would not satisfy this.
    assert {entry["never_alarm"] for entry in served} == {True, False}


# ---------------------------------------------------------------------------
# GUARANTEE 13 — THE HEALTH ROUTE'S OWN BOUND
#
# `identity_service_degraded` is read on the request, from a process that is
# usually on another machine. Until this round the route waited for that
# client's own timeout -- five seconds, the same number the monitor watching
# this lane waits -- so a HUNG identification service was published as a DEAD
# LANE, and every real signal this lane publishes was retired at the same
# moment. A slow third machine, reported as a fault on this one.
# ---------------------------------------------------------------------------


def _a_socket_that_never_answers():
    """A listener that accepts the connection and then says nothing, ever.

    A HUNG service, which is a different fault from a refused one: a refused
    connection returns immediately and this does not return at all. It is the
    case a timeout exists for, and the one no test in this suite had.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock


def _lane_reading_a_hung_identity_service(sock, timeout_s=None):
    controller = full_lane()
    controller.identifier = VehicleIdClient(
        endpoint=f"http://127.0.0.1:{sock.getsockname()[1]}"
    )
    if timeout_s is not None:
        controller.config = replace(controller.config, identity_health_timeout_s=timeout_s)
    return controller


def test_the_health_route_does_not_wait_on_a_hung_service_for_longer_than_its_own_bound():
    """The lane answers in about a second, and says `unknown` about that code.

    `unknown` and not `ok`: nobody measured. `unknown` and not `active` either
    -- a service that has not answered has not been found degraded, and folding
    a hang into a degradation would page a human about the wrong machine.

    The upper bound is what makes this a measurement rather than a restatement:
    the identification client's own timeout is five seconds, so an answer inside
    four proves the bound applied is THIS LANE'S and not that client's.
    """
    sock = _a_socket_that_never_answers()
    try:
        controller = _lane_reading_a_hung_identity_service(sock)
        assert controller.config.identity_health_timeout_s == DEFAULT_IDENTITY_HEALTH_TIMEOUT_S

        started = time.monotonic()
        entry = _health_of(controller)[MalfunctionCode.IDENTITY_SERVICE_DEGRADED.value]
        elapsed = time.monotonic() - started

        assert entry["state"] == "unknown"
        assert entry["source"] == Source.MEASURED.value
        assert elapsed >= DEFAULT_IDENTITY_HEALTH_TIMEOUT_S, (
            f"the route answered in {elapsed:.3f}s, which is less than the bound it claims to "
            "apply -- it did not wait for the service at all"
        )
        assert elapsed < 4.0, (
            f"the route waited {elapsed:.3f}s on a hung service. The bound applied is not this "
            "lane's; it is whatever the identification client happens to use"
        )
        # And the rest of the payload is still there. A hung third machine costs
        # this one field, not the response: every other code still ships, which
        # is the whole difference between this and a lane that stops answering.
        assert len(_health_of(controller)) == len(MalfunctionCode)
    finally:
        sock.close()


def test_the_bound_is_the_sites_setting_and_moving_it_moves_the_answer():
    """The control for the test above: it is a SETTING, not a constant.

    Without this, a hard-coded one-second sleep would satisfy every assertion
    there. A site that lowers it waits less, measurably, on the same hung
    socket.
    """
    sock = _a_socket_that_never_answers()
    try:
        controller = _lane_reading_a_hung_identity_service(sock, timeout_s=0.2)

        started = time.monotonic()
        entry = _health_of(controller)[MalfunctionCode.IDENTITY_SERVICE_DEGRADED.value]
        elapsed = time.monotonic() - started

        assert entry["state"] == "unknown"
        assert 0.2 <= elapsed < 1.0, (
            f"the route waited {elapsed:.3f}s with the setting at 0.2s, so the number it honours "
            "is not the site's"
        )
    finally:
        sock.close()


def test_a_timeout_that_is_not_a_positive_number_of_seconds_is_refused():
    """Zero would be a lane that never asks, reporting `unknown` for ever."""
    for bad in (0, -1.0, "1.0", True, None):
        with pytest.raises(ValueError, match="identity_health_timeout_s"):
            replace(full_lane().config, identity_health_timeout_s=bad)
    # The control: the same construction with a real value builds.
    assert replace(full_lane().config, identity_health_timeout_s=2.5)


# ---------------------------------------------------------------------------
# GUARANTEE 14 — THE HEALTH READ CHECKS THE VERSION IT IS READING
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        # The version this build understands -- taken from the ENGINE's own
        # contract, so a lane that pinned a number of its own goes red here.
        (SCHEMA_VERSION, "active"),
        # A version it does not: refused, not partially read. Both contracts
        # state that policy in the same words, and the read path already obeys
        # it -- this is the same payload from the same service.
        (99, "unknown"),
        (0, "unknown"),
        # `True == 1`, so a bool would otherwise read as version 1.
        (True, "unknown"),
        ("1", "unknown"),
        # A payload that does not say which contract it is is not one this
        # build can place.
        (None, "unknown"),
    ],
)
def test_the_health_read_refuses_a_schema_version_this_build_does_not_know(version, expected):
    body = {"status": "degraded"}
    if version is not None:
        body["schema_version"] = version

    controller = full_lane()
    controller.identifier = _AnIdentifierWithHealth(body)

    entry = _health_of(controller)[MalfunctionCode.IDENTITY_SERVICE_DEGRADED.value]
    assert entry["state"] == expected
    assert entry["source"] == Source.MEASURED.value


# ---------------------------------------------------------------------------
# GUARANTEE 15 — `clock_skew_rejected` COMES BACK
#
# It was a latch that read like a state: one skew and the code was `active`
# until the process restarted, however long ago the clock was fixed. At a
# monitor that means `recovered` could never fire for it -- the operator repairs
# the clock, the money record starts recording again, and the surface stays red,
# which is how a surface trains its reader to skim.
# ---------------------------------------------------------------------------


class _RefusesUntilTold:
    """A platform that refuses every write, until a test says it accepts one."""

    def __init__(self, body: str) -> None:
        self.refusing = True
        self.body = body

    def __call__(self, request, timeout=None):
        if self.refusing:
            raise urllib.error.HTTPError(
                request.full_url, 409, "refused", {}, io.BytesIO(self.body.encode("utf-8"))
            )
        return io.BytesIO(b"{}")


def _a_lane_talking_to(opener):
    transport = PlatformTransport(PlatformClient("http://platform.invalid", "t", opener=opener))
    return full_lane(events=EventQueue(transport=transport)), transport


def _one_log_event(controller):
    """One ordinary event, delivered through the real transport and `_guarded`."""
    return LaneEvent(kind="decision", lane_id=controller.config.lane_id, at=0.0, detail={})


@pytest.mark.parametrize(
    ("refusal", "raised_state"),
    [
        ('{"error": "ahead of the clock", "code": "clock_skew"}', "active"),
        # The other half of the code: a platform too old to name its refusals.
        # It leaves `unknown`, and it has to be able to leave that too.
        ('{"error": "no"}', "unknown"),
    ],
)
def test_clock_skew_rejected_recovers_on_the_next_write_the_platform_accepts(
    refusal, raised_state
):
    opener = _RefusesUntilTold(refusal)
    controller, transport = _a_lane_talking_to(opener)

    transport.send([_one_log_event(controller)])
    assert _health_of(controller)[MalfunctionCode.CLOCK_SKEW_REJECTED.value]["state"] == (
        raised_state
    )

    # The clock is fixed, or the old platform is upgraded, and the next write
    # goes through. That is the platform taking this lane's time.
    opener.refusing = False
    transport.send([_one_log_event(controller)])

    assert _health_of(controller)[MalfunctionCode.CLOCK_SKEW_REJECTED.value]["state"] == "ok"
    assert transport.skew_rejected == 0
    assert transport.conflicts_unnamed == 0
    # And the refusal is not forgotten where it belongs: `rejected` counts every
    # terminal refusal for the life of the process and is NOT cleared, because
    # a dropped item is a gap in the money record whatever happened afterwards.
    assert transport.rejected > 0


def test_a_refusal_after_a_recovery_raises_the_code_again():
    """The control: the reset is a reset, not a permanent silence.

    A `skew_rejected = 0` written once and never reachable again would satisfy
    the test above and would make this code answer `ok` through every skew that
    followed -- the reassuring direction, on the code that says the money record
    is being lost.
    """
    opener = _RefusesUntilTold('{"error": "ahead", "code": "clock_skew"}')
    controller, transport = _a_lane_talking_to(opener)

    transport.send([_one_log_event(controller)])
    opener.refusing = False
    transport.send([_one_log_event(controller)])
    assert _health_of(controller)[MalfunctionCode.CLOCK_SKEW_REJECTED.value]["state"] == "ok"

    opener.refusing = True
    transport.send([_one_log_event(controller)])
    assert _health_of(controller)[MalfunctionCode.CLOCK_SKEW_REJECTED.value]["state"] == "active"
