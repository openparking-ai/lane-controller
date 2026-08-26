"""V4: never wrong silently, proven on the real recogniser.

Skipped when the weights are absent, because they are not committed by design.
CI trains a small model first, so the guarantee is enforced there rather than
being a test nobody ever runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("torch")
pytest.importorskip("cv2")

import cv2  # noqa: E402

from lane_controller import DecisionCache, Fallback, Outcome, decide  # noqa: E402
from lane_controller.interfaces import Frame  # noqa: E402
from lane_controller.vision.engine import (  # noqa: E402
    RECOMMENDED_CONFIDENCE_THRESHOLD,
    PlateVehicleIdentifier,
)
from lane_controller.vision.plates.generator import PlateGenerator  # noqa: E402

WEIGHTS = Path("models/plate_crnn.pt")
needs_weights = pytest.mark.skipif(
    not WEIGHTS.exists(), reason="no trained weights; run the train module first"
)


def as_frame(image) -> Frame:
    ok, buf = cv2.imencode(".png", image)
    assert ok
    return Frame(image_bytes=buf.tobytes(), captured_at=0.0, camera_id="test")


@pytest.fixture(scope="module")
def identifier():
    return PlateVehicleIdentifier(WEIGHTS)


# --- the generator, which needs no weights --------------------------------

def test_the_generator_is_deterministic_from_its_seed():
    # The eval set is re-derived from a number rather than stored, so this is
    # what makes an evaluation reproducible at all.
    a = PlateGenerator(seed=42).batch(5)
    b = PlateGenerator(seed=42).batch(5)
    assert [s.text for s in a] == [s.text for s in b]
    assert (a[0].image == b[0].image).all()


def test_different_seeds_give_different_plates():
    a = PlateGenerator(seed=1).batch(5)
    b = PlateGenerator(seed=2).batch(5)
    assert [s.text for s in a] != [s.text for s in b]


def test_the_degradation_ladder_actually_degrades():
    gen = PlateGenerator(seed=7)
    clean = gen.sample(degradation=0).image
    rough = gen.sample(degradation=9).image
    # Laplacian variance is a standard sharpness proxy; a rung-9 plate must be
    # measurably less sharp than a rung-0 one or the ladder is decoration.
    sharp = cv2.Laplacian(cv2.cvtColor(clean, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
    blurred = cv2.Laplacian(cv2.cvtColor(rough, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
    assert blurred < sharp


def test_florida_is_weighted_up():
    states = [s.state for s in PlateGenerator(seed=3).batch(300)]
    assert states.count("FL") > len(states) / 3, "Florida first, per E2"


# --- the engine -----------------------------------------------------------

@needs_weights
def test_a_clean_plate_is_read_confidently(identifier):
    """The control. Without it, the fallback tests below could pass because the
    engine never reads anything at all."""
    sample = PlateGenerator(seed=11).sample(degradation=0)
    identity = identifier.identify([as_frame(sample.image)])
    assert identity.plate is not None
    assert identity.confidence > RECOMMENDED_CONFIDENCE_THRESHOLD
    assert identity.plate.replace(" ", "") == sample.text.replace(" ", "")


@needs_weights
def test_no_frames_is_zero_confidence_not_a_guess(identifier):
    identity = identifier.identify([])
    assert identity.plate is None
    assert identity.confidence == 0.0


@needs_weights
def test_an_undecodable_frame_is_not_an_invention(identifier):
    junk = Frame(image_bytes=b"not an image at all", captured_at=0.0, camera_id="test")
    identity = identifier.identify([junk])
    assert identity.plate is None
    assert identity.confidence == 0.0


@needs_weights
def test_the_engine_never_invents_make_model_or_colour(identifier):
    # Those slices are not built. A plausible value here would be
    # indistinguishable from a measurement to everything downstream.
    sample = PlateGenerator(seed=13).sample(degradation=0)
    identity = identifier.identify([as_frame(sample.image)])
    assert identity.make is None and identity.model is None and identity.color is None
    assert identity.marks == ()


@needs_weights
def test_the_best_frame_wins(identifier):
    """Several frames are grabbed precisely so one bad moment does not decide."""
    import random

    from lane_controller.vision.plates.generator import degrade

    sample = PlateGenerator(seed=17).sample(degradation=0)
    bad = degrade(sample.image, 9, random.Random(0))
    identity = identifier.identify([as_frame(bad), as_frame(sample.image)])
    assert identity.confidence > RECOMMENDED_CONFIDENCE_THRESHOLD


@needs_weights
def test_garbage_input_reaches_the_lane_as_a_fallback(identifier):
    """V4 end to end: an image with no plate in it must not open a gate.

    The engine may still emit some text -- OCR on noise sometimes does. What
    must never happen is the LANE acting on it.
    """
    import numpy as np

    noise = np.random.default_rng(0).integers(0, 255, (160, 320, 3), dtype=np.uint8)
    identity = identifier.identify([as_frame(noise)])

    cache = DecisionCache()
    cache.load([], default_action="allow")
    decision = decide(identity, cache, confidence_threshold=RECOMMENDED_CONFIDENCE_THRESHOLD)

    assert decision.outcome is Outcome.FALLBACK
    assert decision.fallback in (Fallback.LOW_CONFIDENCE, Fallback.NO_PLATE_READ)
    assert not decision.should_vend


@needs_weights
def test_the_measured_threshold_is_what_the_lane_must_use(identifier):
    """The recogniser is accurate AND overconfident: at the lane's own 0.85
    default it would act on reads the harness measured as wrong 4.4% of the
    time. The engine publishes the measured operating point for that reason."""
    assert RECOMMENDED_CONFIDENCE_THRESHOLD >= 0.99
