"""A dead engine is not a marginal read.

`identify()` answers every failure with a zero confidence, and zero is below
every threshold, so a lane whose identification service was switched off used
to report `low_confidence` -- the code that means "a plate we could almost make
out". Everything downstream that reads these codes, the intercom agent first,
would then tell a driver to wipe a plate while nothing was looking at it.

**The service in this file is a real one.** It listens on loopback, the client
is a real `VehicleIdClient` with its real opener, and the failures are produced
by shutting the socket, by making the handler sleep past the timeout, and by
answering with a body. An injected opener that raises is a stub of a failure,
not a failure -- and this is the file where the difference decides whether the
measurement means anything, because what is being measured is what the client
does with a socket.

The outcome never changes: FALLBACK, a human, a driver who still gets served.
The REASON is what changes, and it changes from wrong to true.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from vehicle_id.contract import ANSWER, FALLBACK, Engine, Identity, Read

from lane_controller import (
    CameraConfig,
    DecisionCache,
    Fallback,
    Frame,
    GateConfig,
    LaneConfig,
    LaneController,
    Outcome,
    Rule,
    decide,
)
from lane_controller.simulated import (
    CannedCameraFeed,
    RecordingVendOutput,
    SimulatedLoopInput,
)
from lane_controller.vehicle_id_client import (
    CAUSE_BAD_RESPONSE,
    CAUSE_NO_FRAMES,
    CAUSE_SERVICE_ERROR,
    CAUSE_TIMEOUT,
    CAUSE_UNREACHABLE,
    VehicleIdClient,
)

THRESHOLD = 0.85


def a_frame() -> Frame:
    return Frame(image_bytes=b"jpeg-ish", captured_at=0.0, camera_id="lane-1")


def a_read(**overrides) -> dict:
    base = dict(
        read_id="r1",
        captured_at="2026-08-27T12:00:00+00:00",
        camera_id="lane-1",
        identity=Identity(plate="PERMIT001"),
        confidence=0.995,
        engine=Engine(name="test", version="0.1.0", weights_id="sha256:abc"),
        threshold_applied=0.99,
        outcome=ANSWER,
    )
    base.update(overrides)
    return Read(**base).to_dict()


#: The engine's OWN fallback: it looked, it measured a confidence, and it
#: declined to stand behind it. This is a read, and it must keep arriving as
#: `low_confidence` -- it is the control that says the new code has not simply
#: swallowed the old one.
A_MARGINAL_READ = a_read(
    identity=Identity(), confidence=0.31, outcome=FALLBACK, presence=True
)


class _Engine:
    """A vehicle-id service that can be switched off, slowed down, or broken.

    Only `POST /v1/reads` is served, because that is the only call on the path
    between a car arriving and a barrier moving.
    """

    def __init__(self, port: int = 0) -> None:
        self.body: dict | bytes = {"cursor": 1, "read": a_read()}
        self.status = 200
        self.delay = 0.0
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                if engine.delay:
                    time.sleep(engine.delay)
                payload = engine.body
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(engine.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):  # keep the test output readable
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self._server.server_address[1]
        self.endpoint = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def kill(self) -> None:
        """Stop listening. The port then refuses connections, as a dead engine does."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def engine():
    running = _Engine()
    try:
        yield running
    finally:
        try:
            running.kill()
        except Exception:
            pass


@pytest.fixture
def permit_list():
    """A permit-list garage: on the list opens, anything else does not."""
    cache = DecisionCache()
    cache.load([Rule(plate="PERMIT001", allow=True)], default_action="deny")
    return cache


# ---------------------------------------------------------------------------
# The fail-control, in one test: the service is killed MID-RUN, between two
# identifications by the same client, and restored by a second service on the
# same port.
# ---------------------------------------------------------------------------


def test_killing_the_engine_mid_run_says_engine_unreachable_and_never_low_confidence(
    engine, permit_list
):
    client = VehicleIdClient(endpoint=engine.endpoint, timeout=2.0)

    # (1) CONTROL, before anything is broken. The same client, the same lane,
    #     the same call: a gate that never opens proves nothing about a gate.
    live = decide(client.identify([a_frame()]), permit_list, confidence_threshold=THRESHOLD)
    assert live.outcome is Outcome.ALLOW, "control: the lane must work before it is broken"

    # (2) THE ENGINE DIES. Nothing else changes -- same client, same object,
    #     same frames.
    engine.kill()

    dead = decide(client.identify([a_frame()]), permit_list, confidence_threshold=THRESHOLD)

    assert dead.fallback is Fallback.ENGINE_UNREACHABLE
    assert dead.fallback is not Fallback.LOW_CONFIDENCE, (
        "a switched-off engine reported as a marginal read is the defect: "
        "the agent then tells a driver to wipe a plate nothing is looking at"
    )
    assert dead.identity.unavailable == CAUSE_UNREACHABLE
    # The outcome does NOT change. A driver at the barrier still gets the human
    # path; only the reason for it is now true.
    assert dead.outcome is Outcome.FALLBACK
    assert not dead.should_vend

    # (3) RESTORED -- same port, same client object, nothing rebuilt on the
    #     lane's side -- and a genuinely marginal read still says so. Without
    #     this the change could have been "call every fallback
    #     engine_unreachable", which is the same defect facing the other way.
    restored = _Engine(port=engine.port)
    try:
        restored.body = {"cursor": 1, "read": A_MARGINAL_READ}
        marginal = decide(
            client.identify([a_frame()]), permit_list, confidence_threshold=THRESHOLD
        )
    finally:
        restored.kill()

    assert marginal.fallback is Fallback.LOW_CONFIDENCE
    assert marginal.identity.unavailable is None, "the engine answered; that is a read"
    assert marginal.outcome is Outcome.FALLBACK


# ---------------------------------------------------------------------------
# Each failure separately, against the same real service, because they are
# different repairs and the record is the only place that difference exists.
# ---------------------------------------------------------------------------


def test_a_service_that_is_not_running_is_unreachable(engine, permit_list):
    client = VehicleIdClient(endpoint=engine.endpoint, timeout=2.0)
    engine.kill()

    identity = client.identify([a_frame()])

    assert identity.unavailable == CAUSE_UNREACHABLE
    assert decide(identity, permit_list, confidence_threshold=THRESHOLD).fallback is (
        Fallback.ENGINE_UNREACHABLE
    )


def test_a_service_that_answers_too_slowly_is_a_timeout(engine, permit_list):
    engine.delay = 0.5
    client = VehicleIdClient(endpoint=engine.endpoint, timeout=0.1)

    identity = client.identify([a_frame()])

    assert identity.unavailable == CAUSE_TIMEOUT, (
        "a slow engine and a dead one need different repairs, so they are "
        "different causes under the one code"
    )
    assert decide(identity, permit_list, confidence_threshold=THRESHOLD).fallback is (
        Fallback.ENGINE_UNREACHABLE
    )


def test_a_service_that_answers_with_an_error_status_is_a_service_error(engine, permit_list):
    engine.status = 503
    engine.body = {"error": "model not loaded"}
    client = VehicleIdClient(endpoint=engine.endpoint, timeout=2.0)

    identity = client.identify([a_frame()])

    assert identity.unavailable == CAUSE_SERVICE_ERROR
    assert decide(identity, permit_list, confidence_threshold=THRESHOLD).fallback is (
        Fallback.ENGINE_UNREACHABLE
    )


def test_a_body_this_build_cannot_read_is_a_bad_response(engine, permit_list):
    engine.body = b"<html>proxy says hello</html>"
    client = VehicleIdClient(endpoint=engine.endpoint, timeout=2.0)

    identity = client.identify([a_frame()])

    assert identity.unavailable == CAUSE_BAD_RESPONSE
    assert decide(identity, permit_list, confidence_threshold=THRESHOLD).fallback is (
        Fallback.ENGINE_UNREACHABLE
    )


def test_a_schema_this_build_does_not_understand_is_a_bad_response(engine, permit_list):
    future = a_read()
    future["schema_version"] = 99
    engine.body = {"cursor": 1, "read": future}
    client = VehicleIdClient(endpoint=engine.endpoint, timeout=2.0)

    identity = client.identify([a_frame()])

    assert identity.plate is None, "a record from a newer contract must not be half-read"
    assert identity.unavailable == CAUSE_BAD_RESPONSE
    assert decide(identity, permit_list, confidence_threshold=THRESHOLD).fallback is (
        Fallback.ENGINE_UNREACHABLE
    )


def test_no_frames_is_its_own_cause_and_not_the_engines_fault(permit_list):
    """The camera produced nothing, so the engine was never asked.

    One code, because the lane does the same thing; a distinct cause, because
    the thing to go and fix is a camera and not a service.
    """
    identity = VehicleIdClient(endpoint="http://127.0.0.1:1").identify([])

    assert identity.unavailable == CAUSE_NO_FRAMES
    assert decide(identity, permit_list, confidence_threshold=THRESHOLD).fallback is (
        Fallback.ENGINE_UNREACHABLE
    )


# ---------------------------------------------------------------------------
# The controls. Each one is a way the change could have gone wrong that the
# tests above would not have noticed.
# ---------------------------------------------------------------------------


def test_a_live_engine_that_answers_still_opens_the_barrier(engine, permit_list):
    """A lane that falls back for everything satisfies every test above."""
    client = VehicleIdClient(endpoint=engine.endpoint, timeout=2.0)

    decision = decide(client.identify([a_frame()]), permit_list, confidence_threshold=THRESHOLD)

    assert decision.outcome is Outcome.ALLOW and decision.should_vend


def test_the_engines_own_fallback_is_a_read_and_stays_low_confidence(engine, permit_list):
    """The engine looked and declined to stand behind what it saw.

    That is a measurement, and the code for it is the one it always was. The
    two are different events and they now have different names -- which is the
    entire change, and it is worth nothing if the new name eats the old one.
    """
    engine.body = {"cursor": 1, "read": A_MARGINAL_READ}
    client = VehicleIdClient(endpoint=engine.endpoint, timeout=2.0)

    identity = client.identify([a_frame()])
    assert identity.unavailable is None

    assert decide(identity, permit_list, confidence_threshold=THRESHOLD).fallback is (
        Fallback.LOW_CONFIDENCE
    )


def test_no_vehicle_present_is_still_no_vehicle_and_not_a_fallback(engine, permit_list):
    """`presence=False` is checked before this and must stay that way.

    Nothing was there, so nothing happens: no ticket, no session, no vend. A
    new check inserted in the wrong place would turn that into a fallback,
    which is a ticket for a car that does not exist.
    """
    engine.body = {
        "cursor": 1,
        "read": a_read(identity=Identity(), confidence=0.0, outcome=FALLBACK, presence=False),
    }
    client = VehicleIdClient(endpoint=engine.endpoint, timeout=2.0)

    decision = decide(client.identify([a_frame()]), permit_list, confidence_threshold=THRESHOLD)

    assert decision.outcome is Outcome.NO_VEHICLE
    assert decision.fallback is None


# ---------------------------------------------------------------------------
# What reaches the RECORD, which is the only place the difference survives.
# ---------------------------------------------------------------------------


def _lane(endpoint: str):
    config = LaneConfig(
        lane_id="lane-test",
        site_id="site-test",
        camera=CameraConfig(camera_id="sim-cam-1", rtsp_url="", frames_per_read=3),
        gate=GateConfig(),
        confidence_threshold=THRESHOLD,
    )
    cache = DecisionCache()
    cache.load([Rule(plate="PERMIT001", allow=True)], default_action="deny")
    return LaneController(
        config,
        loop=SimulatedLoopInput(arrivals=1),
        camera=CannedCameraFeed(),
        vend=RecordingVendOutput(),
        identifier=VehicleIdClient(endpoint=endpoint, timeout=2.0),
        cache=cache,
    )


def test_the_event_says_which_failure_it_was(engine):
    """One code for what the lane did; the cause for what has to be fixed.

    A code with nothing behind it sends whoever answers the intercom to look at
    the wrong thing -- and `confidence: 0.0` sitting in the same detail is not
    a measurement, so a reader that takes the number without this field is
    reading a marginal plate that never existed.
    """
    controller = _lane(engine.endpoint)
    engine.kill()

    controller.run_once()

    fallbacks = [e for e in list(controller.events._queue) if e.kind == "fallback_needs_human"]
    assert len(fallbacks) == 1
    assert fallbacks[0].detail["fallback"] == Fallback.ENGINE_UNREACHABLE.value
    assert fallbacks[0].detail["cause"] == CAUSE_UNREACHABLE


def test_the_event_carries_no_cause_when_there_was_a_read(engine):
    """The control for the field above: it is not a constant that is always set."""
    engine.body = {"cursor": 1, "read": A_MARGINAL_READ}
    controller = _lane(engine.endpoint)

    controller.run_once()

    fallback = next(e for e in list(controller.events._queue) if e.kind == "fallback_needs_human")
    assert fallback.detail["fallback"] == Fallback.LOW_CONFIDENCE.value
    assert fallback.detail["cause"] is None


def test_the_kind_the_lane_emits_is_one_the_platform_already_accepts(engine):
    """No new event KIND, and that is what keeps a lane deployable ahead of the
    platform: the platform allow-lists kinds and stores `detail` opaquely, so a
    new fallback VALUE inside an existing kind needs nothing on the other side.

    Asserted against the kinds this repository emits rather than against a copy
    of the platform's list -- the copy is what the platform's own comment warns
    about. The comparison against the real list is in the receipt for this
    round, run against `platform` `main`.
    """
    controller = _lane(engine.endpoint)
    engine.kill()

    controller.run_once()

    emitted = {e.kind for e in list(controller.events._queue)}
    assert emitted <= {
        "armed",
        "frames_captured",
        "vehicle_identified",
        "decision",
        "fallback_needs_human",
    }, "this round must not introduce an event kind"


def test_the_two_codes_are_distinct_values_a_consumer_can_tell_apart():
    """Asserted rather than left to be inferred: the agent reads these strings."""
    assert Fallback.ENGINE_UNREACHABLE is not Fallback.LOW_CONFIDENCE
    assert Fallback.ENGINE_UNREACHABLE.value != Fallback.LOW_CONFIDENCE.value


def test_every_cause_is_distinct_so_the_detail_can_name_the_repair():
    causes = [
        CAUSE_NO_FRAMES,
        CAUSE_UNREACHABLE,
        CAUSE_TIMEOUT,
        CAUSE_SERVICE_ERROR,
        CAUSE_BAD_RESPONSE,
    ]
    assert len(set(causes)) == len(causes)
