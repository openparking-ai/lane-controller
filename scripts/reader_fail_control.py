#!/usr/bin/env python3
"""The control for the fee on the card reader.

Runs tests/test_reader_fee.py once intact, where it must pass, and once per
break, where it must fail. A reader that has never been seen to show a
recomputed or a stale fee is not known to show the stored one.

  total_resummed               the cart's total is added up from its lines.
  lines_reworded               a line is described by its code, not its words.
  zero_fee_shows_a_cart        a priced stay of zero goes up as a cart.
  cart_forced_first            the lane hands the reader a cart, not the record.
  fee_shown_after_the_vend     the fee reaches the reader after the barrier moved.
  stale_fee_left_up            nothing takes the last car's fee down.
  reader_on_the_lane_thread    the barrier waits on the reader under the runner.
  clear_erases_an_unshown_fee  a clear replaces a fee the reader never showed.
  a_raising_reader_stops_the_lane  a reader that raises takes the arrival with it.
"""

from __future__ import annotations

import sys

from _control import intact, judge, run

SUITE = ["-q", "tests/test_reader_fee.py"]
BREAKAGES = [
    ("total_resummed", "the cart's total is added up from its lines"),
    ("lines_reworded", "a line is described by its code, not its words"),
    ("zero_fee_shows_a_cart", "a priced stay of zero goes up as a cart"),
    ("cart_forced_first", "the lane hands the reader a cart, not the record"),
    ("fee_shown_after_the_vend", "the fee reaches the reader after the barrier moved"),
    ("stale_fee_left_up", "nothing takes the last car's fee down"),
    ("reader_on_the_lane_thread", "the barrier waits on the reader under the runner"),
    ("clear_erases_an_unshown_fee", "a clear replaces a fee the reader never showed"),
    ("a_raising_reader_stops_the_lane", "a reader that raises takes the arrival with it"),
]


failures = 0

print("== control A: the reader-fee suite must PASS intact ==")
collected, _ = intact(SUITE, {"BREAK_READER_FEE": ""})
if collected < 0:
    failures += 1

print("\n== control B: each breakage must make it FAIL ==")
for mode, description in BREAKAGES:
    if not judge(mode, description, collected, run(SUITE, {"BREAK_READER_FEE": mode})):
        failures += 1

if failures:
    print(
        f"\n{failures} control(s) failed. Do not trust the reader-fee tests.",
        file=sys.stderr,
    )
    sys.exit(1)
print("\nall controls OK — the reader-fee suite fails when the reader is broken, as it must.")
