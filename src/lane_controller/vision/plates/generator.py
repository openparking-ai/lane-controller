"""Synthetic plate generator: training data, evaluation data, and the
degradation ladder V4's fallback tests and V2's calibration both need.

Everything the recogniser ever sees comes from here. That is the point: the
training data is generated, so no dataset licence question exists, and the
weights that come out are Open Parking AI's own asset.

Determinism is by seed, so an eval set is reproducible from a number rather
than from a file somebody has to store and nobody may commit.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import cv2
import numpy as np

from .templates import DIGITS, LETTERS, TEMPLATES, PlateTemplate

PLATE_W, PLATE_H = 320, 160


@dataclass(frozen=True, slots=True)
class Sample:
    image: np.ndarray
    text: str
    state: str
    degradation: int


def _render_text(rng: random.Random, pattern: str) -> str:
    out = []
    for slot in pattern:
        if slot == "L":
            out.append(rng.choice(LETTERS))
        elif slot == "N":
            out.append(rng.choice(DIGITS))
        else:
            out.append(" ")
    return "".join(out)


def _draw(template: PlateTemplate, text: str, rng: random.Random) -> np.ndarray:
    img = np.full((PLATE_H, PLATE_W, 3), template.background, np.uint8)

    # Plate furniture: border, state name, slogan. Present so the recogniser
    # learns to ignore text that is not the registration.
    cv2.rectangle(img, (5, 5), (PLATE_W - 6, PLATE_H - 6), template.ink, 2)
    if template.top_text:
        cv2.putText(img, template.top_text, (14, 30), cv2.FONT_HERSHEY_DUPLEX, 0.55,
                    template.ink, 1, cv2.LINE_AA)
    if template.bottom_text:
        cv2.putText(img, template.bottom_text, (14, PLATE_H - 14), cv2.FONT_HERSHEY_SIMPLEX,
                    0.4, template.ink, 1, cv2.LINE_AA)

    # The registration itself. Font, scale and thickness all vary, because the
    # real variation we cannot model (embossing typefaces) has to be replaced
    # by variation we can.
    font = rng.choice([cv2.FONT_HERSHEY_SIMPLEX, cv2.FONT_HERSHEY_DUPLEX,
                       cv2.FONT_HERSHEY_TRIPLEX])
    scale = rng.uniform(1.5, 1.9)
    thickness = rng.choice([4, 5, 6])
    (tw, th), _ = cv2.getTextSize(text, font, scale, thickness)
    x = max(8, (PLATE_W - tw) // 2)
    y = (PLATE_H + th) // 2 + rng.randint(-4, 6)
    cv2.putText(img, text, (x, y), font, scale, template.ink, thickness, cv2.LINE_AA)
    return img


def degrade(img: np.ndarray, level: int, rng: random.Random) -> np.ndarray:
    """The ladder. 0 is pristine; 9 should be unreadable by anything honest.

    Each rung adds what a real lane actually does to an image: angle, motion,
    dusk, rain on the lens, compression. A recogniser that only ever saw rung 0
    would be confident and wrong the first time it rained.
    """
    if level <= 0:
        return img
    f = level / 9.0
    out = img.astype(np.float32)

    # perspective — the camera is never square-on to the plate
    if level >= 1:
        m = 12 * f
        src = np.float32([[0, 0], [PLATE_W, 0], [PLATE_W, PLATE_H], [0, PLATE_H]])
        dst = src + np.float32([[rng.uniform(-m, m), rng.uniform(-m, m)] for _ in range(4)])
        out = cv2.warpPerspective(out, cv2.getPerspectiveTransform(src, dst), (PLATE_W, PLATE_H),
                                  borderMode=cv2.BORDER_REPLICATE)

    # motion blur — the car is moving
    if level >= 3:
        k = int(1 + 2 * round(3 * f))
        kernel = np.zeros((k, k), np.float32)
        kernel[k // 2, :] = 1.0 / k
        out = cv2.filter2D(out, -1, kernel)

    # defocus and light
    out = cv2.GaussianBlur(out, (0, 0), 0.6 + 3.2 * f)
    out = out * rng.uniform(1 - 0.55 * f, 1 + 0.25 * f)

    # sensor noise, then compression
    out = out + rng.gauss(0, 1) * 0 + np.random.default_rng(rng.randint(0, 2**31)).normal(
        0, 24 * f, out.shape)
    out = np.clip(out, 0, 255).astype(np.uint8)
    if level >= 5:
        quality = int(90 - 70 * f)
        ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, max(5, quality)])
        if ok:
            out = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    return out


class PlateGenerator:
    def __init__(self, seed: int = 0) -> None:
        self.rng = random.Random(seed)
        self._weights = [t.weight for t in TEMPLATES]

    def sample(self, degradation: int | None = None) -> Sample:
        template = self.rng.choices(TEMPLATES, weights=self._weights, k=1)[0]
        pattern = self.rng.choice(template.patterns)
        text = _render_text(self.rng, pattern)
        level = self.rng.randint(0, 6) if degradation is None else degradation
        img = degrade(_draw(template, text, self.rng), level, self.rng)
        return Sample(image=img, text=text, state=template.state, degradation=level)

    def batch(self, n: int, degradation: int | None = None) -> list[Sample]:
        return [self.sample(degradation) for _ in range(n)]
