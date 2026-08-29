"""The lane contract's guarantees, each with the control that proves it can fail.

Every assertion here is about the PUBLISHED surface: what a consumer receives,
and what it can and cannot be told. The controls live in
`scripts/contract_fail_control.py`, which breaks the thing each guarantee
guards and requires this file to go red.
"""

from __future__ import annotations

import json
import re
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest
from vehicle_id.contract import ANSWER, Engine, Identity, Read

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
from lane_controller.config import DEFAULT_OUTBOX_DEPTH_THRESHOLD
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
from lane_controller.events import DEFAULT_HISTORY, SESSION_KINDS
from lane_controller.interfaces import ClosingSequence, Unavailable
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
    assert set(doc) == {"lane", "state", "health", "events"}, (
        "every route in the contract has a payload example, and every example "
        f"belongs to a route. Found {sorted(doc)}"
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
    for name, example in doc.items():
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

    Each of the three carries its own control: the condition it names is
    created, and the state must follow it.
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
    """Every example carries it, and it is the constant, not a number."""
    doc = doc_payloads()
    carried = {name: body["contract_version"] for name in doc for body in [doc[name]]}

    assert set(carried) == set(doc), "an example dropped contract_version"
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
