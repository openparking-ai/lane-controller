"""Inference, and an honest confidence.

The confidence returned here is the mean per-step probability of the characters
actually emitted, not a softmax peak. It is still NOT a calibrated threshold --
scripts/eval_plates.py measures where it should sit against the degradation
ladder, because the V-C3 probe already showed a general OCR reading correctly at
0.74, which is precisely the trap of treating a raw score as a threshold.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import torch

from .dataset import to_tensor
from .model import BLANK, CHARS, PlateNet

DEFAULT_WEIGHTS = Path("models/plate_crnn.pt")


class PlateRecognizer:
    def __init__(self, weights: Path = DEFAULT_WEIGHTS, device: str = "cpu") -> None:
        if not Path(weights).exists():
            raise FileNotFoundError(
                f"no weights at {weights}. They are not committed by design -- "
                "rebuild them with `python -m lane_controller.vision.plates.train`."
            )
        self.device = torch.device(device)
        self.model = PlateNet().to(self.device)
        blob = torch.load(weights, map_location=self.device)
        self.model.load_state_dict(blob["state_dict"])
        self.model.eval()

    @torch.no_grad()
    def read(self, image: np.ndarray) -> tuple[str, float]:
        x = to_tensor(image).unsqueeze(0).to(self.device)
        probs = self.model(x).softmax(2)[0]           # T, C
        best = probs.argmax(1)

        text, kept, previous = [], [], BLANK
        for t, index in enumerate(best.tolist()):
            if index != previous and index != BLANK:
                text.append(CHARS[index - 1])
                kept.append(probs[t, index].item())
            previous = index

        if not text:
            return "", 0.0
        return "".join(text), float(np.mean(kept))
