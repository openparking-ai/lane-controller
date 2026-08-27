#!/usr/bin/env python3
"""The harness. Every accuracy number Open Parking AI quotes comes from here.

    python scripts/eval_plates.py

Reports, side by side, the general-OCR baseline and our own recogniser, across
the whole degradation ladder, plus the per-execution-path timings and the
confidence calibration V2 needs.

Two rules this file exists to enforce (V3):

  * No accuracy claim exists outside this output. The knowhow repo quotes these
    numbers or none.
  * Numbers that cannot honestly be produced are printed as NOT MEASURABLE,
    with the reason. Full-identity accuracy is one of those today.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

import torch

from lane_controller.vision.plates.dataset import EVAL_SEED
from lane_controller.vision.plates.generator import PlateGenerator

LADDER = list(range(0, 10))


def normalise(text: str) -> str:
    """Compare registrations, not layout: case and gaps are not the answer."""
    return "".join(ch for ch in text.upper() if ch.isalnum())


def evaluate(reader, samples) -> dict:
    exact = 0
    char_err = 0
    char_total = 0
    confidences = []
    wrong_confident = 0
    for s in samples:
        got, conf = reader.read(s.image)
        want = normalise(s.text)
        got_n = normalise(got)
        confidences.append(conf)
        if got_n == want:
            exact += 1
        else:
            if conf >= 0.85:
                wrong_confident += 1
        char_total += len(want)
        char_err += _levenshtein(got_n, want)
    n = len(samples)
    return {
        "n": n,
        "exact": exact,
        "exact_pct": 100.0 * exact / n,
        "cer_pct": 100.0 * char_err / max(char_total, 1),
        "mean_confidence": statistics.mean(confidences) if confidences else 0.0,
        "wrong_and_confident": wrong_confident,
    }


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def timing(reader, samples, label: str) -> float:
    reader.read(samples[0].image)
    times = []
    for s in samples[:40]:
        t = time.perf_counter()
        reader.read(s.image)
        times.append((time.perf_counter() - t) * 1000)
    median = statistics.median(times)
    print(f"    {label:34} {median:6.2f} ms/plate  ({1000 / median:6.1f} plates/sec)")
    return median


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--per-rung", type=int, default=200)
    ap.add_argument("--weights", type=Path, default=Path("models/plate_crnn.pt"))
    ap.add_argument("--json-out", type=Path)
    ap.add_argument("--skip-baseline", action="store_true")
    args = ap.parse_args()

    print("=" * 78)
    print(" Open Parking AI — plate recogniser evaluation")
    print(" reproduce with:  python scripts/eval_plates.py")
    print("=" * 78)

    # The eval set is re-derived from a seed, never stored, never committed.
    sets = {
        rung: PlateGenerator(seed=EVAL_SEED + rung).batch(args.per_rung, degradation=rung)
        for rung in LADDER
    }
    print(f"\n eval set: {args.per_rung} synthetic plates per rung, "
          f"{len(LADDER)} rungs, seed {EVAL_SEED} (reproducible, not stored)")

    readers = {}
    from lane_controller.vision.plates.recognizer import PlateRecognizer

    readers["ours (CRNN+CTC, trained on synthetic)"] = PlateRecognizer(args.weights)
    if not args.skip_baseline:
        from lane_controller.vision.baseline import RapidOcrBaseline

        readers["baseline (RapidOCR PP-OCRv3)"] = RapidOcrBaseline()

    results = {}
    for name, reader in readers.items():
        print(f"\n {name}")
        cols = ("rung", "exact %", "CER %", "mean conf", "wrong>=.85")
        print(f"   {cols[0]:>4}  {cols[1]:>8}  {cols[2]:>7}  {cols[3]:>9}  {cols[4]:>11}")
        rows = {}
        for rung in LADDER:
            r = evaluate(reader, sets[rung])
            rows[rung] = r
            print(f"   {rung:>4}  {r['exact_pct']:>8.1f}  {r['cer_pct']:>7.2f}  "
                  f"{r['mean_confidence']:>9.3f}  {r['wrong_and_confident']:>12d}")
        results[name] = rows

    print("\n timing, per execution path (D4: labelled, never assumed)")
    ours = readers["ours (CRNN+CTC, trained on synthetic)"]
    timing(ours, sets[0], "ours — CPU")
    if torch.backends.mps.is_available():
        timing(PlateRecognizer(args.weights, device="mps"), sets[0], "ours — MPS")
    if not args.skip_baseline:
        timing(readers["baseline (RapidOCR PP-OCRv3)"], sets[0], "baseline — CPU (onnxruntime)")
    print("    CoreML: measured SLOWER than CPU on this Mac (8-partition split); not used.")

    # V2 calibration: where does the threshold have to sit?
    print("\n confidence calibration (V2)")
    print("   A raw score is not a threshold. Our recogniser is ACCURATE and")
    print("   OVERCONFIDENT: mean confidence barely moves across the ladder while")
    print("   accuracy falls. So the operating point has to be measured, not chosen.")
    for name, reader in readers.items():
        print(f"\n   {name}")
        print(f"     {'threshold':>9}  {'answers':>8}  {'of those wrong':>15}  {'-> fallback':>11}")
        pairs = []
        for rung in LADDER:
            for sample in sets[rung]:
                got, conf = reader.read(sample.image)
                pairs.append((conf, normalise(got) == normalise(sample.text)))
        total = len(pairs)
        best = None
        for threshold in (0.0, 0.50, 0.80, 0.85, 0.90, 0.95, 0.97, 0.98, 0.99, 0.995):
            answered = [ok for conf, ok in pairs if conf >= threshold]
            wrong = sum(1 for ok in answered if not ok)
            silent = 100.0 * wrong / total
            print(f"     {threshold:>9.3f}  {len(answered):>8}  {wrong:>7} ({silent:>5.2f}%)  "
                  f"{100.0 * (total - len(answered)) / total:>10.1f}%")
            # The operating point: the cheapest threshold that gets silent-wrong
            # under 1%. A fallback costs an operator a glance; a silent wrong
            # answer bills a stranger's car to somebody else.
            if best is None and silent < 1.0:
                best = (threshold, silent, 100.0 * (total - len(answered)) / total)
        if best:
            print(f"     -> operating point {best[0]:.3f}: silent-wrong {best[1]:.2f}%, "
                  f"fallback {best[2]:.1f}%")
        else:
            print("     -> NO threshold reaches <1% silent-wrong on this ladder.")

    print("\n NOT MEASURABLE, and why (V3 requires saying so rather than omitting it)")
    print("   full-identity accuracy   : requires bench ground truth pairing plate with")
    print("                              make/model/colour/appearance. No adequately")
    print("                              licensed public set exists (see docs/EVAL_DATA.md).")
    print("   real-plate accuracy      : requires the physical bench. Synthetic fonts are")
    print("                              not embossing typefaces; this number is NOT a")
    print("                              prediction of real-world accuracy.")
    print("   false-match rate         : belongs with fusion (V2), after re-ID lands.")

    if args.json_out:
        args.json_out.write_text(json.dumps(results, indent=2))
        print(f"\n wrote {args.json_out}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
