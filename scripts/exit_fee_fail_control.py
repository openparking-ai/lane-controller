#!/usr/bin/env python3
"""The control for the fee published on `GET /v1/lane/state` as `exit_fee`.

Runs tests/test_exit_fee_published.py once intact, where it must pass, and once
per break, where it must fail. A fee published for a second screen that has
never been seen to differ from the reader's is not known to match it.

  fee_resummed            the published figure is added up from the lines.
  digits_guessed          every currency is published with two decimals.
  discount_not_followed   the fee as priced, after a validation was held.
  left_up_after_close     the fee stays published once the car has gone.
  figure_without_a_cart   a figure for a record the reader shows nothing for.
  no_reader_no_fee        an exit with no reader publishes nothing.
  stay_on_the_wire        the stay's session id rides along with the fee.
"""

from __future__ import annotations

import sys

from _control import intact, judge, run

SUITE = ["-q", "tests/test_exit_fee_published.py"]
BREAKAGES = [
    ("fee_resummed", "the published figure is added up from the lines"),
    ("digits_guessed", "every currency is published with two decimals"),
    ("discount_not_followed", "the fee as priced, after a validation was held"),
    ("left_up_after_close", "the fee stays published once the car has gone"),
    ("figure_without_a_cart", "a figure for a record the reader shows nothing for"),
    ("no_reader_no_fee", "an exit with no reader publishes nothing"),
    ("stay_on_the_wire", "the stay's session id rides along with the fee"),
]


failures = 0

print("== control A: the exit-fee suite must PASS intact ==")
collected, _ = intact(SUITE, {"BREAK_EXIT_FEE": ""})
if collected < 0:
    failures += 1

print("\n== control B: each breakage must make it FAIL ==")
for mode, description in BREAKAGES:
    if not judge(mode, description, collected, run(SUITE, {"BREAK_EXIT_FEE": mode})):
        failures += 1

if failures:
    print(
        f"\n{failures} control(s) failed. Do not trust the exit-fee tests.",
        file=sys.stderr,
    )
    sys.exit(1)
print("\nall controls OK — the exit-fee suite fails when the publication is broken, as it must.")
