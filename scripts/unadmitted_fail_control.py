#!/usr/bin/env python3
"""The control for the entry-nothing-admitted guarantees.

Runs `tests/test_unadmitted.py` once intact, where it must pass, and once for
each break below, where it must FAIL. Every break is on a decision point in
`LaneController` -- a gate on the idle read, a name, a reason, the flush, or
what the idle read writes -- and never on the fixture that drives it.

  pending   the idle read runs while a vend is pending, so the crossing that
            belongs to the vend is recorded as unadmitted and the vend is held
  lock      the idle read runs while a settle is reading the same loops: two
            readers of one board
  reason    an ordinary promotion is answered with the new reason
  folded    the new case is recorded under `entry_confirmed`, so a broken boom
            reads as business as usual
  bypass    the idle read records and does not flush, unlike every other path
            that writes a record
  session   the idle read opens a session with no identity: the phantom
            occupant in a different hat

This also proves the suite RUNS -- see `_control.py` for what a break has to
do before it is reported as a control.
"""

from __future__ import annotations

import sys

from _control import intact, judge, run

SUITE = ["-q", "tests/test_unadmitted.py"]
BREAKAGES = [
    ("pending", "the idle read ignores a pending vend"),
    ("lock", "the idle read ignores a settle reading the loops"),
    ("reason", "a promotion carries the unadmitted reason"),
    ("folded", "the new case is recorded as entry_confirmed"),
    ("bypass", "the idle read does not flush"),
    ("session", "the idle read opens a session"),
]


failures = 0

print("== control A: the unadmitted suite must PASS intact ==")
collected, _ = intact(SUITE, {"BREAK_UNADMITTED": ""})
if collected < 0:
    failures += 1

print("\n== control B: each breakage must make it FAIL ==")
for mode, description in BREAKAGES:
    if not judge(mode, description, collected, run(SUITE, {"BREAK_UNADMITTED": mode})):
        failures += 1

if failures:
    print(f"\n{failures} control(s) failed. Do not trust the unadmitted tests.", file=sys.stderr)
    sys.exit(1)
print("\nall controls OK — the unadmitted suite fails when the guarantee is broken, as it must.")
