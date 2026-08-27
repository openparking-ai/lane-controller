"""The lane as a client of Vehicle ID.

This is the half of the never-wrong-silently guarantee that lives in the LANE.
The engine's half -- a degraded plate must come back as `fallback` -- is tested
in the vehicle-id repository against the real recogniser. Here we test what the
lane DOES with the record it is handed, which is the part that opens a barrier.
"""

from __future__ import annotations

from vehicle_id.contract import ANSWER, FALLBACK, Engine, Identity, Read

from lane_controller import DecisionCache, Fallback, Frame, Outcome, decide
from lane_controller.vehicle_id_client import VehicleIdClient


def a_frame(camera_id: str = "lane-1") -> Frame:
    return Frame(image_bytes=b"jpeg-ish", captured_at=0.0, camera_id=camera_id)


def a_read(**overrides) -> dict:
    base = dict(
        read_id="r1",
        captured_at="2026-08-27T12:00:00+00:00",
        camera_id="lane-1",
        identity=Identity(plate="ABC123"),
        confidence=0.995,
        engine=Engine(name="test", version="0.1.0", weights_id="sha256:abc"),
        threshold_applied=0.99,
        outcome=ANSWER,
    )
    base.update(overrides)
    return Read(**base).to_dict()


def client_returning(payload, health=None) -> VehicleIdClient:
    def opener(url, body, timeout):
        if callable(payload):
            return payload(url, body, timeout)
        return payload

    c = VehicleIdClient(opener=opener)
    if health is not None:
        c._open_health = lambda url: health
    return c


def test_an_answer_becomes_an_identity_the_lane_can_use():
    c = client_returning({"cursor": 1, "read": a_read()})
    identity = c.identify([a_frame()])
    assert identity.plate == "ABC123"
    assert identity.confidence == 0.995


def test_a_fallback_read_reaches_the_lane_as_no_identity():
    """The engine measured a confidence and declined to stand behind it.

    Passing that number through would let the lane's own threshold second-guess
    a decision the engine already made against measured data.
    """
    c = client_returning({"cursor": 1, "read": a_read(outcome=FALLBACK, confidence=0.93)})
    identity = c.identify([a_frame()])
    assert identity.plate is None
    assert identity.confidence == 0.0


def test_a_fallback_read_cannot_open_a_barrier_even_on_an_allow_all_lane():
    """End to end, and the reason the rule above exists.

    The lane is configured to allow everything it can identify. A fallback must
    still not vend, because the lane never learned a plate to allow.
    """
    cache = DecisionCache()
    cache.load([], default_action="allow")
    c = client_returning({"cursor": 1, "read": a_read(outcome=FALLBACK, confidence=0.93)})

    decision = decide(c.identify([a_frame()]), cache, confidence_threshold=0.85)

    assert decision.outcome is Outcome.FALLBACK
    assert decision.fallback in (Fallback.LOW_CONFIDENCE, Fallback.NO_PLATE_READ)
    assert not decision.should_vend


def test_the_engine_never_inventing_a_field_survives_the_translation():
    c = client_returning({"cursor": 1, "read": a_read(identity=Identity(plate="ABC123"))})
    identity = c.identify([a_frame()])
    assert identity.make is None and identity.model is None and identity.color is None
    assert identity.plate_region is None
    assert identity.marks == ()


def test_no_frames_asks_nothing_of_the_service():
    called = []

    def opener(url, body, timeout):
        called.append(url)
        return {"cursor": 1, "read": a_read()}

    identity = VehicleIdClient(opener=opener).identify([])
    assert identity.confidence == 0.0
    assert called == [], "with no frames there is nothing to identify and nothing to ask"


def test_the_service_being_down_is_a_fallback_not_a_crash():
    def down(url, body, timeout):
        raise ConnectionError("vehicle-id is not running")

    identity = client_returning(down).identify([a_frame()])
    assert identity.plate is None
    assert identity.confidence == 0.0


def test_a_malformed_response_is_a_fallback_not_a_crash():
    identity = client_returning({"unexpected": "shape"}).identify([a_frame()])
    assert identity.confidence == 0.0


def test_a_schema_this_build_does_not_understand_is_refused_not_guessed_at():
    future = a_read()
    future["schema_version"] = 99
    identity = client_returning({"cursor": 1, "read": future}).identify([a_frame()])
    assert identity.plate is None, "a record from a newer contract must not be half-read"
    assert identity.confidence == 0.0


def test_every_frame_is_sent_as_one_read_of_one_vehicle():
    sent = {}

    def opener(url, body, timeout):
        sent.update(body)
        return {"cursor": 1, "read": a_read()}

    client_returning(opener).identify([a_frame(), a_frame(), a_frame()])
    assert len(sent["captures"]) == 3
    assert sent["camera_id"] == "lane-1"


def test_the_lane_can_read_the_engines_measured_operating_point():
    c = client_returning({}, health={"threshold_applied": 0.99})
    assert c.operating_threshold() == 0.99


def test_an_unreachable_service_reports_no_operating_point_rather_than_a_default():
    c = VehicleIdClient()

    def boom(url):
        raise ConnectionError("down")

    c._open_health = boom
    assert c.operating_threshold() is None


def test_the_capture_time_sent_is_the_cameras_not_the_services():
    """`captured_at` means when the camera took it.

    Letting the service stamp its own arrival time instead skews every entry
    and exit by however long the lane and the engine were busy -- and the
    platform prices a stay from those two timestamps.
    """
    sent = {}

    def opener(url, body, timeout):
        sent.update(body)
        return {"cursor": 1, "read": a_read()}

    # 2026-08-27T12:00:00+00:00
    frame = Frame(image_bytes=b"jpeg-ish", captured_at=1787832000.0, camera_id="lane-1")
    client_returning(opener).identify([frame])

    assert sent["captures"][0]["captured_at"] == "2026-08-27T12:00:00+00:00"


def test_the_capture_time_is_utc_with_an_offset_whatever_the_lanes_timezone():
    sent = {}

    def opener(url, body, timeout):
        sent.update(body)
        return {"cursor": 1, "read": a_read()}

    client_returning(opener).identify([a_frame()])
    stamp = sent["captures"][0]["captured_at"]
    assert stamp.endswith("+00:00"), f"a naive timestamp crossed the boundary: {stamp}"


def test_the_lanes_rule_lookup_is_a_second_gate_the_engine_does_not_share():
    """Measured, and worth pinning: the engine has no rejection stage.

    A noisy camera feed makes it answer confidently on a small fraction of
    reads with a plate that is essentially random. The lane does not rely on
    the engine being right about that -- a plate that matches no rule is a
    fallback whatever confidence came with it, so two independent things have
    to fail before a barrier opens.
    """
    from lane_controller import Rule

    cache = DecisionCache()
    cache.load([Rule(plate="PERMIT001", allow=True)], default_action="deny")

    confident_nonsense = a_read(identity=Identity(plate="X7QK2ZB"), confidence=0.9991)
    c = client_returning({"cursor": 1, "read": confident_nonsense})

    decision = decide(c.identify([a_frame()]), cache, confidence_threshold=0.99)
    assert not decision.should_vend
    # DENY on a deny-default garage, FALLBACK on one that prices unknown
    # vehicles; either way the barrier stays down and the reason is recorded.
    assert decision.outcome in (Outcome.DENY, Outcome.FALLBACK)


def test_the_second_gate_still_lets_a_known_vehicle_through():
    """The control. A gate that refuses everything is not a gate."""
    from lane_controller import Rule

    cache = DecisionCache()
    cache.load([Rule(plate="PERMIT001", allow=True)], default_action="deny")
    c = client_returning({"cursor": 1, "read": a_read(identity=Identity(plate="PERMIT001"))})

    decision = decide(c.identify([a_frame()]), cache, confidence_threshold=0.99)
    assert decision.should_vend
