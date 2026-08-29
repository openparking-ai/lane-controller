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
from pathlib import Path

import pytest

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
from lane_controller.contract import (
    FALLBACK_REASONS,
    NEVER_ALARM,
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

    # The outbox, and the other direction: ok until it drops something.
    assert codes()[MalfunctionCode.OUTBOX_DEPTH_GROWING.value]["state"] == "ok"
    service.controller.events.dropped = 1
    assert codes()[MalfunctionCode.OUTBOX_DEPTH_GROWING.value]["state"] == "active"

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

