#!/usr/bin/env python3
"""The control for how full the garage was at entry, at the barrier (platform 0034).

Runs tests/test_fill_rule.py once intact, where it must pass, and once per
break, where it must fail. A figure that has never been seen to be wrong is not
known to be right.

  figure_dropped          the exit prices the stay without its figure.
  figure_not_recorded     the record does not say which figure was used.
  board_without_figure    the board prices a car coming in now with no figure.
  board_guesses_empty     with no number of spaces, the board prices as empty.
  not_clamped             more cars than spaces is more than full.
  spaces_not_kept         the number of spaces is not kept on the disk.
  spaces_unchecked        any value is taken as the number of spaces.
"""

from __future__ import annotations

import sys

from _control import intact, judge, run

SUITE = ["-q", "tests/test_fill_rule.py"]
BREAKAGES = [
    ("figure_dropped", "the exit prices the stay without the figure the platform froze on it"),
    ("figure_not_recorded", "the record does not say which figure the stay was priced on"),
    ("board_without_figure", "the board prices a car coming in now with no figure"),
    ("board_guesses_empty", "with no number of spaces, the board prices the garage as empty"),
    ("not_clamped", "more cars than spaces is more than full"),
    ("spaces_not_kept", "the number of spaces is not kept on the disk"),
    ("spaces_unchecked", "any value is taken as the number of spaces"),
]


failures = 0

print("== control A: the fill-rule suite must PASS intact ==")
collected, _ = intact(SUITE, {"BREAK_FILL": ""})
if collected < 0:
    failures += 1

print("\n== control B: each breakage must make it FAIL ==")
for mode, description in BREAKAGES:
    if not judge(mode, description, collected, run(SUITE, {"BREAK_FILL": mode})):
        failures += 1

if failures:
    print(f"\n{failures} control(s) failed. Do not trust the fill-rule tests.", file=sys.stderr)
    sys.exit(1)
print("\nall controls OK — the fill-rule suite fails when the figure is broken, as it must.")
