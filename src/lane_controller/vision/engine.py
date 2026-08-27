"""The Vehicle ID engine, implementing the lane's existing VehicleIdentifier.

Same contract, same confidence semantics as the stub it replaces, so the whole
simulated lane keeps passing with this dropped in. Today it reads plates only;
make/model, colour and the appearance fingerprint are the next slice (D6 step 4)
and their fields stay None rather than being invented.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

import cv2
import numpy as np

from ..interfaces import Frame, VehicleIdentity
from .plates.recognizer import DEFAULT_WEIGHTS, PlateRecognizer

log = logging.getLogger(__name__)

#: MEASURED, not chosen. scripts/eval_plates.py, 10-rung ladder, 1500 plates:
#: this is the cheapest threshold whose silent-wrong rate falls below 1%
#: (0.87% wrong-but-answered, 30.9% sent to fallback).
#:
#: It is far above the lane's own 0.85 default because this recogniser is
#: accurate AND overconfident -- its mean confidence barely moves across the
#: ladder while accuracy falls. A lane running this engine must set
#: confidence_threshold to this value or it will act on wrong reads.
RECOMMENDED_CONFIDENCE_THRESHOLD = 0.99


class PlateVehicleIdentifier:
    """Plate-only Vehicle ID. Real weights, real confidence, no guessing."""

    def __init__(self, weights: Path = DEFAULT_WEIGHTS, device: str = "cpu") -> None:
        self._recognizer = PlateRecognizer(weights, device=device)

    def identify(self, frames: Sequence[Frame]) -> VehicleIdentity:
        if not frames:
            return VehicleIdentity(plate=None, confidence=0.0)

        # Best of the batch. Grabbing several frames exists precisely so one
        # bad moment -- a wiper, a headlight, a bump -- does not decide.
        best_text, best_confidence = "", 0.0
        for frame in frames:
            image = _decode(frame)
            if image is None:
                continue
            text, confidence = self._recognizer.read(image)
            if text and confidence > best_confidence:
                best_text, best_confidence = text, confidence

        if not best_text:
            return VehicleIdentity(plate=None, confidence=0.0)

        return VehicleIdentity(
            plate=best_text,
            plate_region=None,
            # Not invented. The slice that measures these has not been built,
            # and a plausible guess in this field would be indistinguishable
            # from a measurement to everything downstream.
            make=None,
            model=None,
            color=None,
            marks=(),
            confidence=best_confidence,
        )


def _decode(frame: Frame) -> np.ndarray | None:
    buffer = np.frombuffer(frame.image_bytes, dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is None:
        log.warning("frame from %s could not be decoded", frame.camera_id)
    return image
