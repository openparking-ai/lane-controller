#!/usr/bin/env python3
"""The control for tax at the barrier (platform 0023).

Runs tests/test_exit_tax.py once intact, where it must pass, and once per
break, where it must fail. A tax that has never been seen to be wrong is not
known to be right.

  untaxed                 the barrier draws the quote alone.
  chosen_by_the_clock     the set is chosen by the box's clock, not the exit.
  empty_taxed_at_zero     a cache with no tax sets prices at no tax.
  newest_by_text          the newest set is the latest spelling, not instant.
  sets_merged             a refresh merges sets instead of replacing them.
  sets_not_kept           the sets are not kept on the disk.
  reader_keeps_lane_tax   a held validation goes after the lane's tax lines.
  reader_claims_on_taxed  an answer made on the taxed fee is put up.
"""

from __future__ import annotations

import sys

from _control import intact, judge, run

SUITE = ["-q", "tests/test_exit_tax.py"]
BREAKAGES = [
    ("untaxed", "the barrier draws the quote alone"),
    ("chosen_by_the_clock", "the set is chosen by the box's clock, not the exit"),
    ("empty_taxed_at_zero", "a cache with no tax sets prices at no tax"),
    ("newest_by_text", "the newest set is the latest spelling, not the latest instant"),
    ("sets_merged", "a refresh merges the tax sets instead of replacing them"),
    ("sets_not_kept", "the tax sets are not kept on the disk"),
    ("reader_keeps_lane_tax", "a held validation goes after the lane's own tax lines"),
    ("reader_claims_on_taxed", "an answer made on the taxed fee is put up"),
]


failures = 0

print("== control A: the exit-tax suite must PASS intact ==")
collected, _ = intact(SUITE, {"BREAK_EXIT_TAX": ""})
if collected < 0:
    failures += 1

print("\n== control B: each breakage must make it FAIL ==")
for mode, description in BREAKAGES:
    if not judge(mode, description, collected, run(SUITE, {"BREAK_EXIT_TAX": mode})):
        failures += 1

if failures:
    print(f"\n{failures} control(s) failed. Do not trust the exit-tax tests.", file=sys.stderr)
    sys.exit(1)
print("\nall controls OK — the exit-tax suite fails when the tax is broken, as it must.")
