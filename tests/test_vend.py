"""The assisted vend, from the attacker's side.

`POST /v1/lane/vend` is the first route in this project that can open a
barrier, so every assertion here is about what it REFUSES and about the order
in which the one thing it accepts happens. The controls live in
`scripts/vend_fail_control.py`, which breaks the thing each guarantee guards
and requires this file to go red.

The question behind all of it: **could a caller with a microphone open this
barrier by asserting something?** Presence is read off the loop, the
malfunction table is the lane's own, the decision must be this lane's own and
recent, and the identity is written before the relay moves. Every one of those
is a line here, with a positive control beside it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from lane_controller import (
    CameraConfig,
    DecisionCache,
    EventQueue,
    GateConfig,
    LaneConfig,
    LaneController,
    LoopConfig,
    VehicleIdentity,
)
from lane_controller.contract import (
    CONTRACT_VERSION,
    MalfunctionCode,
    TransitState,
    VendAuthority,
    VendRefusal,
)
from lane_controller.interfaces import ClosingSequence, Unavailable
from lane_controller.service import LaneService, make_server
from lane_controller.simulated import (
    CannedCameraFeed,
    OccupancyLoopInput,
    RecordingVendOutput,
    ScriptedClosingLoops,
    SimulatedLoopInput,
    StubVehicleIdentifier,
)
from lane_controller.sync import SESSION_OPEN
from lane_controller.vend import MAX_HELD_KEYS
from serving import serving

TICKET = "TKT-4RS9WQ2M"
READ_TOKEN = "read-token-aaaa"
ACT_TOKEN = "act-token-bbbb"


# ---------------------------------------------------------------------------
# Lanes to complete
# ---------------------------------------------------------------------------


def a_lane(
    identities=None,
    crossings=None,
    *,
    direction="entry",
    default_action="allow",
    events=None,
    **config_kwargs,
):
    """The standard installation, wired to the simulated seams."""
    config = LaneConfig(
        lane_id="lane-1",
        site_id="site-1",
        direction=direction,
        camera=CameraConfig(camera_id="sim-cam-1", rtsp_url="", frames_per_read=1),
        gate=GateConfig(),
        loops=LoopConfig(
            arming_loops=2,
            arming_spacing_m=1.5,
            closing_loops=2,
            closing_spacing_m=1.5,
            confirmation_window_seconds=10.0,
        ),
        **config_kwargs,
    )
    cache = DecisionCache()
    cache.load([])
    cache.default_action = default_action
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


#: A vehicle the lane could not read: the case the intercom exists for.
UNREADABLE = [VehicleIdentity(plate=None, confidence=0.10, presence=True)]
#: A vehicle it read confidently, which a garage defaulting to `allow` admits.
READABLE = [VehicleIdentity(plate="SIM-0001", plate_region="FL", confidence=0.97, presence=True)]


def a_lane_awaiting_a_completion(**kwargs):
    """A lane that has fallen back on one vehicle and is holding the case.

    The crossing is scripted FORWARD so a completion that is accepted settles
    to a confirmed entry -- the whole path, not the first half of it.
    """
    controller = a_lane(
        identities=kwargs.pop("identities", UNREADABLE),
        crossings=kwargs.pop("crossings", [(ClosingSequence.FORWARD, 1.0)]),
        **kwargs,
    )
    controller.run_once()
    return controller


def body(controller, *, authority=VendAuthority.HUMAN_OPEN_NOW, ticket=TICKET, decision_at=None):
    return {
        "authorised_by": authority.value,
        "identity": {"kind": "ticket", "ticket_ref": ticket},
        "decision_at": decision_at or controller.last_decision_at,
    }


def complete(service, controller, *, key="key-1", **kwargs):
    """Call the route the way `vend.py` does, without a socket in the way."""
    from lane_controller.vend import parse

    return service.assisted.complete(parse(body(controller, **kwargs), key))


def settled(service):
    """Wait for the transit the last accepted vend opened, if there was one."""
    thread = service.assisted.settling
    if thread is not None:
        thread.join(timeout=5)
    return service


# ---------------------------------------------------------------------------
# GUARANTEE — a read token never authorises an act
# ---------------------------------------------------------------------------


def _post(base, path, payload, *, token=None, key="key-1", headers=None):
    request = urllib.request.Request(
        f"{base}{path}", data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if key:
        request.add_header("Idempotency-Key", key)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _get(base, path, *, token=None):
    request = urllib.request.Request(f"{base}{path}", method="GET")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_the_read_token_does_not_open_the_barrier():
    """403, not 401: the caller is known and is asking for what it cannot have."""
    controller = a_lane_awaiting_a_completion()
    service = LaneService(controller)
    with serving(make_server(service, port=0, token=READ_TOKEN, act_token=ACT_TOKEN)) as base:
        status, refused = _post(base, "/v1/lane/vend", body(controller), token=READ_TOKEN)
        assert status == 403, refused
        assert "does not authorise" in refused["error"]
        assert controller.vend.vend_count == 0, "nothing may have moved"

        # THE CONTROL: the same call with the ACT token is accepted. Without
        # it this test passes against a route that refuses everybody.
        status, accepted = _post(base, "/v1/lane/vend", body(controller), token=ACT_TOKEN)
        assert status == 202, accepted
        assert controller.vend.vend_count == 1
    settled(service)


def test_the_act_token_does_not_read_the_lane():
    """The rule in the other direction, on every read route."""
    service = LaneService(a_lane_awaiting_a_completion())
    with serving(make_server(service, port=0, token=READ_TOKEN, act_token=ACT_TOKEN)) as base:
        for route in ("/v1/lane", "/v1/lane/state", "/v1/lane/health", "/v1/lane/events"):
            status, refused = _get(base, route, token=ACT_TOKEN)
            assert status == 403, f"{route} answered {status}"
            assert "does not authorise" in refused["error"]
            # THE CONTROL, per route: the READ token serves it.
            assert _get(base, route, token=READ_TOKEN)[0] == 200


def test_a_credential_in_a_query_string_is_refused_on_every_route():
    """401, whichever token, by NAME and by VALUE, and the request is not served."""
    controller = a_lane_awaiting_a_completion()
    service = LaneService(controller)
    with serving(make_server(service, port=0, token=READ_TOKEN, act_token=ACT_TOKEN)) as base:
        # By name, on a read route, with the header credential correct.
        status, refused = _get(base, f"/v1/lane?token={READ_TOKEN}", token=READ_TOKEN)
        assert status == 401 and "access log" in refused["error"]

        # By VALUE, under a parameter name nobody thought of.
        assert _get(base, f"/v1/lane?since=0&whatever={ACT_TOKEN}", token=READ_TOKEN)[0] == 401

        # And on the act route, where being served would also have vended.
        status, _ = _post(
            base, f"/v1/lane/vend?access_token={ACT_TOKEN}", body(controller), token=ACT_TOKEN
        )
        assert status == 401
        assert controller.vend.vend_count == 0

        # THE CONTROL: the same routes with an ordinary query string serve.
        assert _get(base, "/v1/lane/events?since=0", token=READ_TOKEN)[0] == 200
    settled(service)


def test_a_lane_with_a_read_token_and_no_act_token_serves_no_vend():
    """One credential configured and not the other authorises nothing.

    The bind rule catches this off loopback. On loopback it is a lane whose act
    token file was never written, and serving the route to anything that can
    reach loopback because of that is the silent failure this contract refuses.
    """
    controller = a_lane_awaiting_a_completion()
    service = LaneService(controller)
    with serving(make_server(service, port=0, token=READ_TOKEN)) as base:
        status, refused = _post(base, "/v1/lane/vend", body(controller), token=READ_TOKEN)
        assert status == 403
        assert "no credential configured" in refused["error"]
        assert controller.vend.vend_count == 0


def test_a_lane_with_an_act_token_and_no_read_token_still_serves_the_reads():
    """The asymmetry, and it is deliberate.

    A missing ACT token refuses the barrier; a missing READ token is the
    loopback default the reads have always had. Refusing a monitor with a 403
    for a credential it was never asked for is a confusing failure and not a
    safer one, and off loopback the bind already requires both.
    """
    service = LaneService(a_lane_awaiting_a_completion())
    with serving(make_server(service, port=0, act_token=ACT_TOKEN)) as base:
        assert _get(base, "/v1/lane")[0] == 200
        # THE CONTROL: the vend on the same lane still requires its own token.
        assert _post(base, "/v1/lane/vend", {}, token=None)[0] == 401


def test_a_loopback_lane_with_no_credentials_serves_the_vend():
    """The default, and the control on every 403 above."""
    controller = a_lane_awaiting_a_completion()
    service = LaneService(controller)
    with serving(make_server(service, port=0)) as base:
        status, accepted = _post(base, "/v1/lane/vend", body(controller))
        assert status == 202, accepted
        assert accepted["vend_commanded"] is True
        assert "opened" not in accepted, "nothing here measures the boom"
        assert accepted["transit"] == TransitState.PENDING.value
        assert accepted["contract_version"] == CONTRACT_VERSION
        assert isinstance(accepted["event_cursor"], int)
    settled(service)
    assert controller.vend.vend_count == 1


# ---------------------------------------------------------------------------
# GUARANTEE — presence is read from the LOOP, at the moment of the call
# ---------------------------------------------------------------------------


def test_the_loop_decides_whether_a_vehicle_is_there_not_the_caller():
    controller = a_lane_awaiting_a_completion()
    service = LaneService(controller)
    # The car has driven off. Everything else about the case is unchanged and
    # the caller is authorised.
    controller.loop.clear()

    status, refused = complete(service, controller)
    assert status == 409
    assert refused["code"] == VendRefusal.NO_VEHICLE.value
    assert controller.vend.vend_count == 0

    # THE CONTROL: the car is back on the loop and the same call is accepted.
    controller.loop._occupied = True
    assert complete(service, controller, key="key-2")[0] == 202
    settled(service)
    assert controller.vend.vend_count == 1


def test_a_body_that_asserts_presence_is_not_consulted():
    """A caller cannot put a vehicle on the loop by saying so.

    The route's parser accepts no presence field at all, and this is the proof
    that adding one to the body changes nothing: the refusal is unchanged.
    """
    from lane_controller.vend import parse

    controller = a_lane_awaiting_a_completion()
    service = LaneService(controller)
    controller.loop.clear()

    asserted = {**body(controller), "presence": True, "vehicle_present": True}
    status, refused = service.assisted.complete(parse(asserted, "key-1"))
    assert status == 409
    assert refused["code"] == VendRefusal.NO_VEHICLE.value


def test_the_presence_refusal_is_decides_own(monkeypatch):
    """The route's answer moves with `decide()`, so it is not a second copy.

    Perturbing the presence check inside `decide()` -- the function every
    ordinary arrival goes through -- must change what this route refuses. If it
    does not, the route is applying a rule of its own that can drift.
    """
    import lane_controller.vend as vend_module
    from lane_controller.decision import Decision, Outcome

    controller = a_lane_awaiting_a_completion()
    service = LaneService(controller)

    def blind(identity, cache, *, confidence_threshold, now=None):
        # `presence is False` no longer refuses. This is the perturbation.
        return Decision(outcome=Outcome.FALLBACK, reason="perturbed", identity=identity)

    controller.loop.clear()
    monkeypatch.setattr(vend_module, "decide", blind)
    status, _ = complete(service, controller)
    assert status == 202, "the route did not follow decide(); it holds its own copy"
    settled(service)


# ---------------------------------------------------------------------------
# GUARANTEE — the lane's own refusals, one test per code
# ---------------------------------------------------------------------------


def test_an_active_malfunction_refuses_and_names_the_code():
    controller = a_lane_awaiting_a_completion(
        identities=[
            VehicleIdentity(
                plate=None, confidence=0.0, presence=True, unavailable=Unavailable.UNREACHABLE
            )
        ],
    )
    service = LaneService(controller)
    assert (
        service.derived_states()[MalfunctionCode.IDENTITY_SERVICE_DOWN].value == "active"
    ), "the fixture must actually produce an active malfunction"

    status, refused = complete(service, controller)
    assert status == 409
    assert refused["code"] == VendRefusal.MALFUNCTION_ACTIVE.value
    assert refused["malfunction"] == MalfunctionCode.IDENTITY_SERVICE_DOWN.value
    assert controller.vend.vend_count == 0

    # THE CONTROL: the same lane with nothing wrong accepts the same call.
    healthy = a_lane_awaiting_a_completion()
    assert complete(LaneService(healthy), healthy, key="key-2")[0] == 202


def test_a_never_alarm_code_does_not_refuse(monkeypatch):
    """`reference_not_recognised` covers an ordinary car arriving.

    Refusing on it would turn a car into a locked barrier, which is the failure
    its caveat exists to prevent.
    """
    from lane_controller.contract import HealthState

    controller = a_lane_awaiting_a_completion()
    service = LaneService(controller)
    monkeypatch.setattr(
        LaneService,
        "derived_states",
        lambda self: {MalfunctionCode.REFERENCE_NOT_RECOGNISED: HealthState.ACTIVE},
    )
    assert complete(service, controller)[0] == 202
    settled(service)


def test_one_arming_loop_occupied_refuses_geometry_incomplete():
    controller = a_lane_awaiting_a_completion()
    service = LaneService(controller)
    controller.arming_loop_b.set_occupied(False)

    status, refused = complete(service, controller)
    assert status == 409
    assert refused["code"] == VendRefusal.GEOMETRY_INCOMPLETE.value
    assert controller.vend.vend_count == 0

    # THE CONTROL: both loops occupied and the same call is accepted.
    controller.arming_loop_b.set_occupied(True)
    assert complete(service, controller, key="key-2")[0] == 202
    settled(service)


def test_a_decision_older_than_the_sites_bound_is_stale():
    controller = a_lane_awaiting_a_completion(completion_max_age_s=30.0)
    service = LaneService(controller)
    # The lane's own clock moves on. The case is otherwise unchanged.
    real_now = controller.now()
    controller._clock = lambda: real_now + 31.0

    status, refused = complete(service, controller)
    assert status == 409
    assert refused["code"] == VendRefusal.DECISION_STALE.value
    assert "completion_max_age_s" in refused["error"]

    # THE CONTROL: one second inside the bound is accepted.
    controller._clock = lambda: real_now + 29.0
    assert complete(service, controller, key="key-2")[0] == 202
    settled(service)


def test_a_decision_at_that_is_not_this_lanes_last_is_refused():
    from lane_controller.sync import to_iso

    controller = a_lane_awaiting_a_completion()
    service = LaneService(controller)

    # RECENT but not this lane's. An ancient one would be `decision_stale`
    # first, which is the published order and is a different test.
    status, refused = complete(
        service, controller, decision_at=to_iso(controller.now() - 1.0)
    )
    assert status == 409
    assert refused["code"] == VendRefusal.DECISION_MISMATCH.value

    # THE CONTROL: the value `GET /v1/lane/state` publishes is accepted, and it
    # is accepted whichever way the caller spells the same instant.
    published = service.state().to_dict()["decision"]["at"]
    reserialised = published.replace("+00:00", "Z")
    assert complete(service, controller, decision_at=reserialised, key="key-2")[0] == 202
    settled(service)


def test_a_lane_that_has_decided_nothing_has_nothing_to_complete():
    """A restarted lane. `decision` is null, so there is no case to finish."""
    controller = a_lane()
    controller.loop.wait_for_vehicle()  # a car is on the loop; nothing decided
    service = LaneService(controller)
    assert service.state().to_dict()["decision"] is None

    from lane_controller.vend import parse

    request = parse(
        {
            "authorised_by": VendAuthority.HUMAN_OPEN_NOW.value,
            "identity": {"kind": "ticket", "ticket_ref": TICKET},
            "decision_at": "2026-08-30T14:03:11.482913+00:00",
        },
        "key-1",
    )
    # Ordered AFTER `decision_stale`, so an ancient `decision_at` is stale
    # first. This one is aged against the lane's clock and is not.
    controller._clock = lambda: 1788000000.0
    status, refused = service.assisted.complete(request)
    assert status == 409
    assert refused["code"] in (
        VendRefusal.DECISION_STALE.value,
        VendRefusal.DECISION_MISMATCH.value,
    )
    assert controller.vend.vend_count == 0


def test_an_allow_has_already_vended_and_is_not_completable():
    controller = a_lane_awaiting_a_completion(identities=READABLE)
    service = LaneService(controller)
    assert controller.last_decision.outcome.value == "allow"

    status, refused = complete(service, controller)
    assert status == 409
    assert refused["code"] == VendRefusal.NOT_COMPLETABLE.value
    assert "already vended" in refused["error"]
    assert controller.vend.vend_count == 1, "the ordinary vend, and not a second one"


@pytest.mark.parametrize(
    "authority",
    [VendAuthority.DISPLAY_CODE_CONFIRMED, VendAuthority.HUMAN_OPEN_AND_FLAG],
)
def test_a_deny_is_a_rule_and_only_human_open_now_overrides_it(authority):
    controller = a_lane_awaiting_a_completion(identities=READABLE, default_action="deny")
    service = LaneService(controller)
    assert controller.last_decision.outcome.value == "deny"

    status, refused = complete(service, controller, authority=authority)
    assert status == 409
    assert refused["code"] == VendRefusal.NOT_COMPLETABLE.value
    assert "overrides one" in refused["error"]
    assert controller.vend.vend_count == 0


def test_human_open_now_on_a_deny_vends_and_records_the_override():
    controller = a_lane_awaiting_a_completion(identities=READABLE, default_action="deny")
    service = LaneService(controller)

    status, accepted = complete(service, controller, authority=VendAuthority.HUMAN_OPEN_NOW)
    assert status == 202, accepted
    settled(service)
    assert controller.vend.vend_count == 1

    recorded = [
        event for _, event in controller.events._history if event.kind == "assisted_identity"
    ]
    assert len(recorded) == 1
    assert recorded[0].detail["override_of"] == "deny"
    assert recorded[0].detail["authorised_by"] == VendAuthority.HUMAN_OPEN_NOW.value

    # THE CONTROL: an ordinary completion of a FALLBACK records `null` there,
    # so the field distinguishes an override from every other vend.
    ordinary = a_lane_awaiting_a_completion()
    complete(LaneService(ordinary), ordinary, key="key-2")
    assert [
        event.detail["override_of"]
        for _, event in ordinary.events._history
        if event.kind == "assisted_identity"
    ] == [None]


def test_a_vend_in_progress_refuses_the_next_one():
    """`busy`, held from the acceptance until the transit settles."""
    controller = a_lane_awaiting_a_completion(
        crossings=[(ClosingSequence.NONE, 0.0), (ClosingSequence.NONE, 0.0)],
    )
    service = LaneService(controller)
    # A crossing that never comes, so the settle thread is still waiting when
    # the second call arrives.
    import threading

    release = threading.Event()
    controller.closing_loops.wait_for_sequence = lambda window: (
        release.wait(timeout=5) or ClosingSequence.NONE
    )

    assert complete(service, controller, key="key-1")[0] == 202
    status, refused = complete(service, controller, key="key-2")
    assert status == 409
    assert refused["code"] == VendRefusal.BUSY.value
    assert controller.vend.vend_count == 1, "the second call moved nothing"

    release.set()
    settled(service)


def test_the_refusals_are_applied_in_the_published_order():
    """A lane failing two conditions reports the EARLIER one.

    The order is what the document publishes, and a consumer that acts on the
    first refusal it is given would otherwise be sent to fix the second thing.
    """
    controller = a_lane_awaiting_a_completion(completion_max_age_s=1.0)
    service = LaneService(controller)
    # Every one of: no vehicle, one arming loop, a stale decision.
    controller.loop.clear()
    controller.arming_loop_b.set_occupied(False)
    real_now = controller.now()
    controller._clock = lambda: real_now + 60.0

    assert complete(service, controller)[1]["code"] == VendRefusal.NO_VEHICLE.value

    # Remove the first and the SECOND-earliest surfaces, and so on down.
    controller.loop._occupied = True
    assert complete(service, controller)[1]["code"] == VendRefusal.GEOMETRY_INCOMPLETE.value
    controller.arming_loop_b.set_occupied(True)
    assert complete(service, controller)[1]["code"] == VendRefusal.DECISION_STALE.value


# ---------------------------------------------------------------------------
# GUARANTEE — the identity is written BEFORE the relay moves
# ---------------------------------------------------------------------------


def test_the_identity_is_recorded_before_the_relay_and_the_order_is_exact():
    """Every call recorded, and any order but this one is a failure.

    This is the invariant the whole round exists for. A barrier that opened
    before the record was written is a barrier that opened with nothing saying
    who said so, and that is the vend every outside reviewer of this project
    has named.
    """
    controller = a_lane_awaiting_a_completion()
    service = LaneService(controller)

    calls: list[str] = []
    original_record = controller.events.record
    original_vend = controller.vend.vend

    def recording_record(kind, lane_id, **detail):
        calls.append(f"record:{kind}")
        return original_record(kind, lane_id, **detail)

    def recording_vend(reason):
        calls.append("relay")
        return original_vend(reason)

    controller.events.record = recording_record
    controller.vend.vend = recording_vend

    assert complete(service, controller)[0] == 202
    settled(service)

    assert calls[:4] == [
        "record:assisted_identity",
        "relay",
        "record:vended",
        "record:entry_pending",
    ], f"the assisted vend happened in this order: {calls}"
    # The control: the recorder saw the settling too, so `calls[:4]` is a slice
    # of a longer sequence rather than everything there was.
    assert "record:entry_confirmed" in calls
    assert "record:session_open" in calls


def test_the_relay_reason_is_the_authority():
    controller = a_lane_awaiting_a_completion()
    service = LaneService(controller)
    complete(service, controller, authority=VendAuthority.DISPLAY_CODE_CONFIRMED)
    settled(service)

    assert [reason for _, reason in controller.vend.vends] == [
        VendAuthority.DISPLAY_CODE_CONFIRMED.value
    ]


def test_the_completion_settles_to_a_confirmed_entry_with_the_ticket_on_it():
    controller = a_lane_awaiting_a_completion()
    service = LaneService(controller)
    assert complete(service, controller)[0] == 202
    settled(service)

    assert controller.transit_state == TransitState.CONFIRMED.value
    opens = [event for event in controller.events._queue if event.kind == SESSION_OPEN]
    assert len(opens) == 1
    detail = opens[0].detail
    assert detail["ticket_ref"] == TICKET
    assert detail["identity_kind"] == "ticket"
    assert "plate" not in detail, "a ticket identity invents no plate, not even a null one"
    assert detail["entry_confirmation"] == "confirmed"


def test_the_pending_event_carries_the_kind_and_not_the_reference():
    controller = a_lane_awaiting_a_completion()
    service = LaneService(controller)
    complete(service, controller)
    settled(service)

    pending = [
        event for _, event in controller.events._history if event.kind == "entry_pending"
    ]
    assert len(pending) == 1
    assert pending[0].detail["identity_kind"] == "ticket"
    assert TICKET not in json.dumps(pending[0].detail)

    # THE CONTROL: an ordinary plate arrival says `plate` in the same field, so
    # this is a derived kind rather than a constant.
    plated = a_lane(identities=READABLE, crossings=[(ClosingSequence.FORWARD, 1.0)])
    plated.run_once()
    assert [
        event.detail["identity_kind"]
        for _, event in plated.events._history
        if event.kind == "entry_pending"
    ] == ["plate"]


# ---------------------------------------------------------------------------
# GUARANTEE — no ticket_ref on any read route or in any log
# ---------------------------------------------------------------------------


def test_no_read_route_publishes_the_ticket():
    """An ABSENCE claim, so the same sweep is run against a planted value."""
    controller = a_lane_awaiting_a_completion()
    service = LaneService(controller)
    assert complete(service, controller)[0] == 202
    settled(service)

    served = {
        "/v1/lane": service.describe().to_dict(),
        "/v1/lane/state": service.state().to_dict(),
        "/v1/lane/health": service.health().to_dict(),
        "/v1/lane/events": service.events(0).to_dict(),
    }
    for route, payload in served.items():
        assert TICKET not in json.dumps(payload), f"{route} published the ticket"
        # THE CONTROL, per route: the same sweep over the same payload with the
        # ticket planted in it must find one.
        assert TICKET in json.dumps({**payload, "planted": TICKET}), (
            f"the sweep cannot see a ticket planted in {route}'s payload"
        )

    # And it is on the session action, which is where it belongs: that becomes
    # POST /lane/sessions/open and lands in a column the purge redacts.
    assert TICKET in json.dumps(
        [event.detail for event in controller.events._queue if event.kind == SESSION_OPEN]
    )


def test_the_ticket_is_in_no_log_line(caplog):
    import logging

    caplog.set_level(logging.DEBUG, logger="lane_controller")
    controller = a_lane_awaiting_a_completion()
    service = LaneService(controller)
    # A refusal path and an acceptance path, since they log different things.
    controller.arming_loop_b.set_occupied(False)
    complete(service, controller)
    controller.arming_loop_b.set_occupied(True)
    complete(service, controller, key="key-2")
    settled(service)

    assert TICKET not in caplog.text
    # THE CONTROL: the capture is working and would have seen one.
    logging.getLogger("lane_controller").warning("planted %s", TICKET)
    assert TICKET in caplog.text


def test_a_malformed_ticket_is_refused_without_being_quoted_back():
    from lane_controller.vend import BadVendRequest, parse

    controller = a_lane_awaiting_a_completion()
    for bad in ("tkt-lower", "SHORT", "T" * 65, "TKT REF", None, 12345):
        with pytest.raises(BadVendRequest) as refused:
            parse(body(controller, ticket=bad), "key-1")
        assert str(bad) not in str(refused.value), "a refusal must not quote the secret back"
    # THE CONTROL: a well-formed one at both bounds parses.
    for good in ("A1-2B3", "T" * 64):
        assert parse(body(controller, ticket=good), "key-1").ticket_ref == good


# ---------------------------------------------------------------------------
# GUARANTEE — one key, one vend
# ---------------------------------------------------------------------------


def test_a_replay_returns_the_same_body_and_moves_nothing():
    controller = a_lane_awaiting_a_completion()
    service = LaneService(controller)

    status, first = complete(service, controller, key="the-same-key")
    assert status == 202
    settled(service)
    cursor_after = controller.events.cursor

    # The car has driven off by now, which is what makes this the real case: a
    # replay checked against the loop would be answered `no_vehicle`.
    controller.loop.clear()
    status, again = complete(service, controller, key="the-same-key")
    assert status == 202
    assert again == first, "a replay must return the first answer, byte for byte"
    assert controller.vend.vend_count == 1, "one key, one vend"
    assert controller.events.cursor == cursor_after, "a replay records nothing"

    # THE CONTROL: a DIFFERENT key is a new attempt and is judged afresh --
    # here it meets the loop, which is empty.
    assert complete(service, controller, key="another-key")[1]["code"] == (
        VendRefusal.NO_VEHICLE.value
    )


def test_a_refusal_is_not_held_against_the_key():
    """A transient refusal must not freeze a caller's key for ever."""
    controller = a_lane_awaiting_a_completion()
    service = LaneService(controller)
    controller.loop.clear()
    assert complete(service, controller, key="k")[1]["code"] == VendRefusal.NO_VEHICLE.value

    controller.loop._occupied = True
    status, _ = complete(service, controller, key="k")
    assert status == 202, "the condition passed; the same key must be allowed to succeed"
    settled(service)


def test_the_key_store_is_bounded_and_the_bound_is_the_published_one():
    from lane_controller.vend import AssistedVend

    service = LaneService(a_lane_awaiting_a_completion())
    assert isinstance(service.assisted, AssistedVend)
    for index in range(MAX_HELD_KEYS + 10):
        service.assisted._hold(f"key-{index}", {"n": index})
    assert len(service.assisted._answers) == MAX_HELD_KEYS
    assert service.assisted._held("key-0") is None, "the oldest key is evicted"
    assert service.assisted._held(f"key-{MAX_HELD_KEYS + 9}") == {"n": MAX_HELD_KEYS + 9}


# ---------------------------------------------------------------------------
# GUARANTEE — what a caller may not assert
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload,why",
    [
        ({"identity": {"kind": "ticket", "ticket_ref": TICKET}}, "no authority"),
        ({"authorised_by": "because_i_said_so"}, "an authority outside the set"),
        ({"authorised_by": "human_open_now"}, "no identity"),
        (
            {"authorised_by": "human_open_now", "identity": {"kind": "plate", "plate": "SIM-0001"}},
            "a plate, which a caller may not assert",
        ),
        (
            # THE KIND ALONE. A well-formed ticket under a `plate` kind: the
            # only thing wrong with it is the kind, so this is what refuses a
            # caller that has learnt to send a plausible body.
            {
                "authorised_by": "human_open_now",
                "identity": {"kind": "plate", "ticket_ref": TICKET},
            },
            "a kind outside the published set",
        ),
    ],
)
def test_a_malformed_completion_is_a_400_and_never_a_409(payload, why):
    from lane_controller.vend import BadVendRequest, parse

    with pytest.raises(BadVendRequest):
        parse({"decision_at": "2026-08-30T14:03:11.482913+00:00", **payload}, "key-1")


def test_the_only_identity_kind_is_the_published_one():
    """`vend_identity_kinds` is a set of ONE, and it is checked as a set.

    A plate is deliberately not on it: a caller that could assert one would be
    handing this lane a measurement it did not make, on the record that prices
    the stay. The control is the accepted kind, so this is not a rule that
    refuses everything.
    """
    from lane_controller.contract import VEND_IDENTITY_KINDS
    from lane_controller.vend import BadVendRequest, parse

    assert VEND_IDENTITY_KINDS == ("ticket",)
    controller = a_lane_awaiting_a_completion()
    for kind in ("plate", "phone", "TICKET", ""):
        payload = {**body(controller), "identity": {"kind": kind, "ticket_ref": TICKET}}
        with pytest.raises(BadVendRequest, match="identity.kind"):
            parse(payload, "key-1")
    assert parse(body(controller), "key-1").ticket_ref == TICKET


def test_a_missing_idempotency_key_is_refused():
    from lane_controller.vend import BadVendRequest, parse

    controller = a_lane_awaiting_a_completion()
    for absent in (None, "", "   "):
        with pytest.raises(BadVendRequest, match="Idempotency-Key"):
            parse(body(controller), absent)
    assert parse(body(controller), "k").idempotency_key == "k"


def test_a_naive_decision_at_is_refused():
    from lane_controller.vend import BadVendRequest, parse

    controller = a_lane_awaiting_a_completion()
    with pytest.raises(BadVendRequest, match="UTC offset"):
        parse(body(controller, decision_at="2026-08-30T14:03:11.482913"), "key-1")


def test_an_identifier_may_not_supply_a_ticket(caplog):
    """A ticket is asserted, never measured. The seam strips one.

    A third-party identifier that could set this would be minting a parking
    identity through the interface that exists to report what a camera saw --
    and it would reach the platform's `vehicles` table on the session action.
    """
    import logging

    caplog.set_level(logging.WARNING, logger="lane_controller.controller")
    controller = a_lane(
        identities=[
            VehicleIdentity(
                plate="SIM-0001", confidence=0.97, presence=True, ticket_ref="SMUGGLED1"
            )
        ],
        crossings=[(ClosingSequence.FORWARD, 1.0)],
    )
    controller.run_once()

    assert "SMUGGLED1" not in json.dumps(
        [event.detail for event in controller.events._queue]
    ), "an identifier's ticket reached a session action"
    assert "ticket is asserted, never measured" in caplog.text
    assert "SMUGGLED1" not in caplog.text, "the refusal must not copy the value into a log"
    # THE CONTROL: the arrival really happened, so this is a strip rather than
    # a lane that transacted nothing.
    assert [event.detail["identity_kind"] for event in controller.events._queue
            if event.kind == SESSION_OPEN] == ["plate"]


# ---------------------------------------------------------------------------
# The platform this lane reports to
# ---------------------------------------------------------------------------


def test_the_session_action_sends_the_ticket_to_the_platform():
    """`sync._open_session` sends `ticket_ref`, and exactly one identity."""
    from fake_platform import FakePlatform
    from lane_controller.sync import PlatformTransport

    platform = FakePlatform()
    controller = a_lane_awaiting_a_completion(
        events=EventQueue(transport=PlatformTransport(platform))
    )
    service = LaneService(controller)
    assert complete(service, controller)[0] == 202
    settled(service)
    controller.events.flush()

    assert len(platform.opened) == 1
    sent = platform.opened[0]
    assert sent["ticket_ref"] == TICKET
    assert sent["plate"] is None
    assert sent["entry_confirmation"] == "confirmed"


def test_a_session_action_carrying_both_identities_never_leaves_this_lane():
    """The lane refuses its own malformed request rather than the platform.

    The platform answers 400, which this lane dead-letters -- after the barrier
    has already opened and with the stay lost from the money record. A fault
    this lane can see is louder here than three hops away.
    """
    from lane_controller.platform_client import PlatformClient, PlatformRejected

    client = PlatformClient("http://127.0.0.1:1", "token")
    for plate, ticket in ((None, None), ("SIM-0001", TICKET)):
        with pytest.raises(PlatformRejected, match="exactly one"):
            client.open_session(
                event_id="e1",
                plate=plate,
                ticket_ref=ticket,
                entry_at="2026-08-30T14:03:11+00:00",
                entry_confirmation="confirmed",
            )
