#!/usr/bin/env python3
"""The control for one closing loop: the barrier's own, read only.

Runs tests/test_closing_loop.py once intact, where it must pass, and once for
each break below, where it must FAIL. Every break is on a decision about the
loop -- in `LoopConfig`, `LaneController` or what the lane records -- and never
on the fixture that drives it.

  unconfirmable  one closing loop treated as none, as before U7e: the lane
                 cannot confirm anything
  occupancy      the closing loop occupied no longer confirms; every car is held
  no_vend        the loop occupied with no vend behind it is written down as a
                 confirmed entry, not one nothing admitted
  geometry       every answer is used whatever the lane declared: a direction
                 one loop cannot see, one occupancy at a two-loop lane
  drawing        the drawing's lane declares no closing loops, and the car it
                 admits is missing from the platform's count of cars inside
"""

from __future__ import annotations

import sys

from _control import intact, judge, run

SUITE = ["-q", "tests/test_closing_loop.py"]
BREAKAGES = [
    ("unconfirmable", "one closing loop is treated as none"),
    ("occupancy", "the closing loop occupied does not confirm"),
    ("no_vend", "an occupancy with no vend is confirmed"),
    ("geometry", "an answer the lane's geometry cannot give is used"),
    ("drawing", "the drawing's lane declares no closing loops"),
]


failures = 0

print("== control A: the closing-loop suite must PASS intact ==")
collected, _ = intact(SUITE, {"BREAK_CLOSING_LOOP": ""})
if collected < 0:
    failures += 1

print("\n== control B: each breakage must make it FAIL ==")
for mode, description in BREAKAGES:
    if not judge(mode, description, collected, run(SUITE, {"BREAK_CLOSING_LOOP": mode})):
        failures += 1

if failures:
    print(f"\n{failures} control(s) failed. Do not trust the closing-loop tests.", file=sys.stderr)
    sys.exit(1)
print("\nall controls OK — the closing-loop suite fails when one loop is broken, as it must.")
