#!/usr/bin/env python3
"""The control for U7f: pictures kept with an entry, the tailgater identified,
and the banned car refused.

Runs tests/test_tailgater.py once intact, where it must pass, and once for
each break below, where it must FAIL. Every break is on a decision of the lane
-- in `EventQueue` or `LaneController` -- and never on the fixture that drives it.

  no_pictures           the pictures are taken and dropped: nothing kept
  read_left_unadmitted  the tailgater read at the closing loop opens no stay
  late_admitted_listed  the held car that crossed late is taken for a tailgater
  banned_vends          the car on the banned list is let in
  exit_refuses_banned   an exit keeps a banned car inside (gate F1)
  any_car_after_a_held_vend_is_late
                        a different car after a held vend is not a tailgater (F2)
  list_not_kept_on_disk the list is lost on a restart offline (F3)
  pictures_dropped_in_an_outage
                        a failed send drops the queued pictures (F4)
  thresholds_defaulted  a site with no thresholds refuses cars as banned (G1)
"""

from __future__ import annotations

import sys

from _control import intact, judge, run

SUITE = ["-q", "tests/test_tailgater.py"]
BREAKAGES = [
    ("no_pictures", "the pictures are taken and dropped"),
    ("read_left_unadmitted", "the read tailgater is left an entry nothing admitted"),
    ("late_admitted_listed", "the late admitted car is taken for a tailgater"),
    ("banned_vends", "the car on the banned list is let in"),
    ("exit_refuses_banned", "an exit keeps a banned car inside"),
    ("any_car_after_a_held_vend_is_late", "a different car after a held vend is not a tailgater"),
    ("list_not_kept_on_disk", "the banned list is lost on a restart offline"),
    ("pictures_dropped_in_an_outage", "a failed send drops the queued pictures"),
    ("thresholds_defaulted", "a site that states no thresholds refuses cars as banned"),
]


failures = 0

print("== control A: the tailgater suite must PASS intact ==")
collected, _ = intact(SUITE, {"BREAK_TAILGATER": ""})
if collected < 0:
    failures += 1

print("\n== control B: each breakage must make it FAIL ==")
for mode, description in BREAKAGES:
    if not judge(mode, description, collected, run(SUITE, {"BREAK_TAILGATER": mode})):
        failures += 1

if failures:
    print(f"\n{failures} control(s) failed. Do not trust the tailgater tests.", file=sys.stderr)
    sys.exit(1)
print("\nall controls OK — the tailgater suite fails when U7f is broken, as it must.")
