#!/usr/bin/env python3
"""The control for the exit's local decision.

Runs tests/test_exit_decision.py once intact, where it must pass, and once per
break, where it must fail. An exit that has never been seen to decide wrongly
is not known to decide rightly.

  day_is_utc            the registers are read against UTC's day.
  revoked_covers        a pass's state is not read.
  uncached_guessed      a car with no cached entry is priced as if it entered now.
  platform_on_the_path  the exit asks the platform before it decides.
  plate_in_the_record   the decision event names the plate.
  no_folding            the camera's spelling must match the registrar's.
"""

from __future__ import annotations

import sys

from _control import intact, judge, run

SUITE = ["-q", "tests/test_exit_decision.py"]
BREAKAGES = [
    ("day_is_utc", "the registers are read against UTC's day"),
    ("revoked_covers", "a pass's state is not read"),
    ("uncached_guessed", "an uncached car is priced as if it entered now"),
    ("platform_on_the_path", "the exit asks the platform before it decides"),
    ("plate_in_the_record", "the decision event names the plate"),
    ("no_folding", "the camera's spelling must match the registrar's"),
]


failures = 0

print("== control A: the exit-decision suite must PASS intact ==")
collected, _ = intact(SUITE, {"BREAK_EXIT_DECISION": ""})
if collected < 0:
    failures += 1

print("\n== control B: each breakage must make it FAIL ==")
for mode, description in BREAKAGES:
    if not judge(mode, description, collected, run(SUITE, {"BREAK_EXIT_DECISION": mode})):
        failures += 1

if failures:
    print(
        f"\n{failures} control(s) failed. Do not trust the exit-decision tests.",
        file=sys.stderr,
    )
    sys.exit(1)
print("\nall controls OK — the exit-decision suite fails when the decision is broken, as it must.")
