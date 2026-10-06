#!/usr/bin/env python3
"""The control for a lane that obeys its owner's closing, and for its board.

Runs tests/test_lane_closing.py once intact, where it must pass, and once per
break, where it must fail. A closed lane nobody has seen open for the wrong car
is not known to keep it out.

  closed_test_dropped        the decision no longer asks whether the lane is closed
  display_code_opens         a display code completes at a closed lane
  full_lets_uncovered_in     `full` is read as open
  full_refuses_register      `full` is read as everyone
  person_refused             a person's word is refused at a closed lane
  closed_exit_publishes_fee  a closed exit puts the fee on the reader anyway
  slow_read_only             the closing reaches the lane on the slow read only
  fast_read_ignored          the lane drops the closing the fast read carried
  closing_not_persisted      the closing is not kept on disk
  plate_in_the_refusal       the refusal names the car
  end_time_ignored           a board message outlives its end
  board_untaxed              the board's price leaves the tax out
  plan_not_in_force          the board prices from a plan not in force
"""

from __future__ import annotations

import sys

from _control import intact, judge, run

SUITE = ["-q", "tests/test_lane_closing.py"]
BREAKAGES = [
    ("closed_test_dropped", "the decision no longer asks whether the lane is closed"),
    ("display_code_opens", "a display code completes at a closed lane"),
    ("full_lets_uncovered_in", "full is read as open"),
    ("full_refuses_register", "full is read as everyone"),
    ("person_refused", "a person's word is refused at a closed lane"),
    ("closed_exit_publishes_fee", "a closed exit puts the fee on the reader"),
    ("slow_read_only", "the closing reaches the lane on the slow read only"),
    ("fast_read_ignored", "the lane drops the closing the fast read carried"),
    ("closing_not_persisted", "the closing is not kept on disk"),
    ("plate_in_the_refusal", "the refusal names the car"),
    ("end_time_ignored", "a board message outlives its end"),
    ("board_untaxed", "the board's price leaves the tax out"),
    ("plan_not_in_force", "the board prices from a plan not in force"),
]


failures = 0

print("== control A: the closing suite must PASS intact ==")
collected, _ = intact(SUITE, {"BREAK_CLOSING": ""})
if collected < 0:
    failures += 1

print("\n== control B: each breakage must make it FAIL ==")
for mode, description in BREAKAGES:
    if not judge(mode, description, collected, run(SUITE, {"BREAK_CLOSING": mode})):
        failures += 1

if failures:
    print(f"\n{failures} control(s) failed. Do not trust the closing tests.", file=sys.stderr)
    sys.exit(1)
print("\nall controls OK — the closing suite fails when the lane stops obeying, as it must.")
