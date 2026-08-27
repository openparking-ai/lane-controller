"""A small CRNN with CTC — our own plate recogniser.

Deliberately small. Plates are a constrained domain: a fixed alphabet, a short
sequence, one line of text on a plain ground. The job does not need a large
model, and a small one is what fits in a gate housing beside everything else
the lane has to do.

The weights this produces are Open Parking AI's own asset: trained on data we
generated, under our own permissive terms, with no upstream able to withdraw
them. That is the point of E2, not a side effect.
"""

from __future__ import annotations

import torch
from torch import nn

from .templates import charset

#: index 0 is the CTC blank; real characters start at 1
BLANK = 0
CHARS = charset()
NUM_CLASSES = len(CHARS) + 1

IMG_H, IMG_W = 48, 160


def encode(text: str) -> list[int]:
    """Spaces are dropped: the gap is layout, not a character to predict."""
    return [CHARS.index(c) + 1 for c in text if c in CHARS]


def decode(indices: list[int]) -> str:
    """Greedy CTC collapse: drop repeats, then drop blanks."""
    out, previous = [], BLANK
    for i in indices:
        if i != previous and i != BLANK:
            out.append(CHARS[i - 1])
        previous = i
    return "".join(out)


class PlateNet(nn.Module):
    def __init__(self, num_classes: int = NUM_CLASSES) -> None:
        super().__init__()
        # Height is collapsed to 1 while width is preserved as the sequence
        # axis: a plate is read left to right, so width is time.
        self.cnn = nn.Sequential(
            nn.Conv2d(1, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(),
            nn.MaxPool2d(2, 2),                                   # 24 x 80
            nn.Conv2d(32, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
            nn.MaxPool2d(2, 2),                                   # 12 x 40
            nn.Conv2d(64, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
            nn.MaxPool2d((2, 1), (2, 1)),                         # 6 x 40
            nn.Conv2d(128, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
            nn.MaxPool2d((2, 1), (2, 1)),                         # 3 x 40
            nn.Conv2d(128, 192, (3, 3), padding=(0, 1)), nn.BatchNorm2d(192), nn.ReLU(),
        )                                                          # 1 x 40
        self.rnn = nn.LSTM(192, 128, num_layers=2, bidirectional=True, batch_first=True)
        self.head = nn.Linear(256, num_classes)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        f = self.cnn(x)                    # B, C, 1, W'
        f = f.squeeze(2).permute(0, 2, 1)  # B, W', C
        f, _ = self.rnn(f)
        return self.head(f)                # B, T, classes
