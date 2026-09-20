#!/usr/bin/env python3
"""The control for the deactivate-loop guarantees.

Runs `tests/test_deactivate_loop.py` once intact, where it must pass, and once
for each break below, where it must FAIL. Every break is on a decision point --
the level, an end of the held interval, its record, the vend route's check, the
stuck measurement, the bound -- and never on the fixture that drives it.

  never_held   the loop is read and ignored: the lane arms with a car on it
  edge         the idle turn no longer re-reads the level, so a driver that
               reported the held car once never gets it served -- two queued
               cars wait for each other
  silent_start the hold happens and nothing is written
  silent_end   the interval closes in state and never in the record
  repeat_start every held turn writes the start again
  wrong_end    both ends of the interval are reported as the same end
  vend_door    the assisted vend no longer applies the check: the intercom is
               a door around the loop
  unmeasured   the stuck code is never derived
  no_bound     a loop under the car's own tail is accepted

This also proves the suite RUNS -- see `_control.py` for what a break has to
do before it is reported as a control.
"""

from __future__ import annotations

import sys

from _control import intact, judge, run

SUITE = ["-q", "tests/test_deactivate_loop.py"]

BREAKAGES = [
    ("never_held", "the deactivate loop is read and ignored"),
    ("edge", "the idle turn does not re-read the level"),
    ("silent_start", "a hold is not recorded"),
    ("silent_end", "a release is not recorded"),
    ("repeat_start", "every held turn records the start again"),
    ("wrong_end", "both ends are reported as the same end"),
    ("vend_door", "the assisted vend does not apply the check"),
    ("unmeasured", "the stuck code is never derived"),
    ("no_bound", "a spacing under the longest vehicle is accepted"),
]


failures = 0

print("== control A: the deactivate-loop suite must PASS intact ==")
collected, _ = intact(SUITE, {"BREAK_DEACTIVATE": ""})
if collected < 0:
    failures += 1

print("\n== control B: each breakage must make it FAIL ==")
for mode, description in BREAKAGES:
    if not judge(mode, description, collected, run(SUITE, {"BREAK_DEACTIVATE": mode})):
        failures += 1

if failures:
    print(
        f"\n{failures} control(s) failed. Do not trust the deactivate-loop tests.", file=sys.stderr
    )
    sys.exit(1)
print(
    "\nall controls OK — the deactivate-loop suite fails when the guarantee is broken, as it must."
)
