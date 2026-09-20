#!/usr/bin/env python3
"""The control for the appearance descriptor on the session open AND close.

Runs the suites that carry the descriptor once intact, where they must pass,
and once for each break below, where they must FAIL. Every break is on one of
the three points the descriptor crosses between the identity service and the
platform -- not on a fixture and not on a stub -- so a control that passes says
the suite measures that crossing rather than something next to it. The record
and echo seams are ONE function each for both ends of a stay, so each break
below reaches the close as well as the open, and the suites assert both.

  translation  the client drops the descriptor on translation from the
               service's contract, as it did before this round
  record       the session action does not carry it, so the open goes out
               exactly as before the field existed
  echo         the lane sends it and does not require it back, so a platform
               older than `sessions.entry_descriptor` answers 201, drops it,
               and the lane counts the open delivered -- the silent loss

This also proves the suites RUN. A guarantee that can quietly stop being
collected is not a guarantee -- and a suite that never ran would pass control A
and then fail to fail under every break, which is what control B reports.
"""

from __future__ import annotations

import sys

from _control import intact, judge, run

SUITE = [
    "-q",
    "tests/test_vehicle_id_client.py",
    "tests/test_offline.py",
    "tests/test_loops.py",
    "tests/test_lane_contract.py",
    "tests/test_vend.py",
]
BREAKAGES = [
    ("translation", "the client drops the descriptor on translation"),
    ("record", "the session action does not carry the descriptor"),
    ("echo", "the lane does not require the platform to echo the descriptor"),
]


failures = 0

print("== control A: the suites must PASS intact ==")
collected, _ = intact(SUITE, {"BREAK_DESCRIPTOR": ""})
if collected < 0:
    failures += 1

print("\n== control B: each breakage must make them FAIL ==")
for mode, description in BREAKAGES:
    if not judge(mode, description, collected, run(SUITE, {"BREAK_DESCRIPTOR": mode})):
        failures += 1

if failures:
    print(f"\n{failures} control(s) failed. Do not trust the descriptor tests.", file=sys.stderr)
    sys.exit(1)
print("\nall controls OK — the suites fail when the descriptor is dropped, as they must.")
