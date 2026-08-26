"""Turning generated plates into tensors, and keeping train and eval apart.

The split is by SEED, not by shuffling one pool: the eval generator is seeded
differently and its plates are re-derived, never drawn from the same stream the
model trained on. Two pools that came off one generator with one seed would
share sampling state and quietly overlap.
"""

from __future__ import annotations

import cv2
import numpy as np
import torch
from torch.utils.data import Dataset

from .generator import PlateGenerator, Sample
from .model import IMG_H, IMG_W, encode

TRAIN_SEED = 1
EVAL_SEED = 9_999_991  # deliberately far from the train seed


def to_tensor(image: np.ndarray) -> torch.Tensor:
    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    grey = cv2.resize(grey, (IMG_W, IMG_H), interpolation=cv2.INTER_AREA)
    x = grey.astype(np.float32) / 255.0
    x = (x - 0.5) / 0.5
    return torch.from_numpy(x).unsqueeze(0)


class SyntheticPlates(Dataset):
    def __init__(self, size: int, seed: int, degradation: int | None = None) -> None:
        gen = PlateGenerator(seed=seed)
        self.samples: list[Sample] = gen.batch(size, degradation=degradation)

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, i: int):
        s = self.samples[i]
        return to_tensor(s.image), torch.tensor(encode(s.text), dtype=torch.long), s.text


def collate(batch):
    images = torch.stack([b[0] for b in batch])
    targets = torch.cat([b[1] for b in batch])
    lengths = torch.tensor([len(b[1]) for b in batch], dtype=torch.long)
    texts = [b[2] for b in batch]
    return images, targets, lengths, texts
