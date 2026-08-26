#!/usr/bin/env python3
"""Train the plate recogniser on generated plates.

    python -m lane_controller.vision.plates.train --steps 3000

Weights land in models/plate_crnn.pt, which is gitignored. They are not
committed and do not need to be: the generator is deterministic, so the
training set is reproducible from a seed and anyone can rebuild the model from
the repository alone. Distributing the weights is an optimisation, not a
requirement.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import torch
from torch import nn
from torch.utils.data import DataLoader

from .dataset import TRAIN_SEED, SyntheticPlates, collate
from .model import BLANK, PlateNet

DEFAULT_OUT = Path("models/plate_crnn.pt")


def pick_device(requested: str) -> torch.device:
    if requested != "auto":
        return torch.device(requested)
    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def train(steps: int, batch_size: int, device: torch.device, out: Path, train_size: int) -> dict:
    torch.manual_seed(0)
    data = SyntheticPlates(size=train_size, seed=TRAIN_SEED)
    loader = DataLoader(
        data, batch_size=batch_size, shuffle=True, collate_fn=collate, drop_last=True
    )

    model = PlateNet().to(device)
    # zero_infinity: a CTC target longer than the input sequence yields inf and
    # poisons the whole run. Rare here, but silent when it happens.
    loss_fn = nn.CTCLoss(blank=BLANK, zero_infinity=True)
    opt = torch.optim.AdamW(model.parameters(), lr=3e-3, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=3e-3, total_steps=steps)

    model.train()
    step, started, running = 0, time.perf_counter(), 0.0
    while step < steps:
        for images, targets, lengths, _ in loader:
            if step >= steps:
                break
            images = images.to(device)
            logits = model(images)                       # B, T, C
            logp = logits.log_softmax(2).permute(1, 0, 2)  # T, B, C
            input_lengths = torch.full((images.size(0),), logits.size(1), dtype=torch.long)
            # aten::_ctc_loss has no MPS kernel, so the loss is computed on the
            # CPU and the gradient flows back across the transfer. Done here
            # explicitly rather than via PYTORCH_ENABLE_MPS_FALLBACK=1, which
            # would silently do the same thing for every unimplemented op and
            # leave nobody able to see which ones.
            loss = loss_fn(logp.cpu(), targets, input_lengths, lengths)

            opt.zero_grad(set_to_none=True)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            opt.step()
            sched.step()

            running += loss.item()
            step += 1
            if step % 250 == 0:
                print(f"  step {step:5d}/{steps}  loss {running / 250:.4f}")
                running = 0.0

    elapsed = time.perf_counter() - started
    out.parent.mkdir(parents=True, exist_ok=True)
    torch.save({"state_dict": model.state_dict(), "steps": steps, "train_size": train_size}, out)
    print(f"\ntrained {steps} steps in {elapsed:.1f}s on {device.type}; wrote {out}")
    return {"steps": steps, "seconds": elapsed, "device": device.type}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--steps", type=int, default=3000)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--train-size", type=int, default=20_000)
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "mps"])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    train(args.steps, args.batch_size, pick_device(args.device), args.out, args.train_size)


if __name__ == "__main__":
    main()
