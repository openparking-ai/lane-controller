#!/usr/bin/env python3
"""The control for the exit that opens only when settled (the owner's rule, 2026-10-27).

Runs tests/test_exit_case.py once intact, where it must pass, and once per
break, where it must fail. An exit nobody has seen open for an unpaid car is
not known to stay down for one.

  priced_opens               a car with a fee to pay is let out unpaid.
  unanswered_opens           a platform that cannot be reached is taken as settled.
  no_platform_opens          a lane with no platform wired opens for an unsettled car.
  plate_in_case              the case carries the plate text.
  unsure_plate_sent          a plate below the lane's confidence is sent as the car's ID.
  pictures_after_case        the case is opened before its pictures are sent.
  polled_too_fast            the case is asked about faster than Stripe is read.
  pay_every_turn             the fee is handed to the reader on every answer.
  decline_not_resent         after a decline the fee never reaches the reader again.
  left_case_opens            a car that left the loop is still let out.
  person_said_every_turn     a case that needs a person says so on every turn.
  person_and_case_both_open  a person opens, and the case opens the barrier again.
  close_not_named            the close does not name the case the car left on.
  no_receipt                 the text receipt the driver asked for is never sent.
"""

from __future__ import annotations

import sys

from _control import intact, judge, run

SUITE = ["-q", "tests/test_exit_case.py"]
BREAKAGES = [
    ("priced_opens", "a car with a fee to pay is let out on the lane's copy, unpaid"),
    ("unanswered_opens", "a platform that cannot be reached is taken as a settled case"),
    ("no_platform_opens", "a lane with no platform wired opens for a car it could not settle"),
    ("plate_in_case", "the case carries the plate text"),
    ("unsure_plate_sent", "a plate read below the lane's confidence is sent as the car's ID"),
    ("pictures_after_case", "the case is opened before its pictures are sent"),
    ("polled_too_fast", "the case is asked about on every turn, faster than Stripe is read"),
    ("pay_every_turn", "the fee is handed to the reader again on every answer"),
    ("decline_not_resent", "after a decline the fee is never handed to the reader again"),
    ("left_case_opens", "a car that left the loop keeps its case and is let out"),
    ("person_said_every_turn", "a case that needs a person says so on every turn"),
    ("person_and_case_both_open", "a person opens for the car and its case opens again"),
    ("close_not_named", "the close does not name the case the car left on"),
    ("no_receipt", "the text receipt the driver asked for is never sent"),
]


failures = 0

print("== control A: the exit-case suite must PASS intact ==")
collected, _ = intact(SUITE, {"BREAK_EXIT_SETTLED": ""})
if collected < 0:
    failures += 1

print("\n== control B: each breakage must make it FAIL ==")
for mode, description in BREAKAGES:
    if not judge(mode, description, collected, run(SUITE, {"BREAK_EXIT_SETTLED": mode})):
        failures += 1

if failures:
    print(f"\n{failures} control(s) failed. Do not trust the exit-case tests.", file=sys.stderr)
    sys.exit(1)
print("\nall controls OK — the exit-case suite fails when the settled exit is broken, as it must.")
