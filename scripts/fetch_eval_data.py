#!/usr/bin/env python3
"""Download the real-image sanity set. Never committed.

  Roboflow / Open Images "License Plates (US/EU)"
  images     CC BY 2.0
  annotations CC BY 4.0
  https://public.roboflow.com/object-detection/license-plates-us-eu

Both licences permit commercial use with attribution, which is why this set is
usable at all -- see docs/EVAL_DATA.md for the V-C2 table and what is NOT
usable. 350 images: a sanity check on real photographs, never a benchmark.

The data lands in eval-data/, which is gitignored. It is not redistributed by
this repository; this script references the source and records the licence.
"""

from __future__ import annotations

import sys
from pathlib import Path

DEST = Path("eval-data/license-plates-us-eu")
SOURCE = "https://public.roboflow.com/object-detection/license-plates-us-eu"

NOTICE = """\
License Plates (US/EU) — obtained from Roboflow Public Datasets.
Derived from the Open Images Dataset.
  Images:      CC BY 2.0     https://creativecommons.org/licenses/by/2.0/
  Annotations: CC BY 4.0     https://creativecommons.org/licenses/by/4.0/
Attribution: Google LLC (Open Images), curated by Roboflow.
Not redistributed by Open Parking AI. Downloaded locally, never committed.
"""


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    (DEST / "LICENSE-NOTICE.txt").write_text(NOTICE)

    print(f"destination: {DEST}")
    print(NOTICE)
    print(
        "This set requires an export from Roboflow, which needs an account and an\n"
        "API key -- it is not a plain URL fetch. Download the Pascal VOC or COCO\n"
        f"export from\n\n  {SOURCE}\n\n"
        f"and unpack it into {DEST}.\n\n"
        "Recorded rather than automated on purpose: pretending a keyed export is a\n"
        "one-line download would leave a script that fails for everyone but us."
    )
    have = list(DEST.rglob("*.jpg")) + list(DEST.rglob("*.png"))
    print(f"\nimages present: {len(have)}")
    return 0 if have else 1


if __name__ == "__main__":
    sys.exit(main())
