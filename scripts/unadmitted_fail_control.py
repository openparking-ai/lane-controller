#!/usr/bin/env python3
"""The control for the entry-nothing-admitted guarantees.

Runs `tests/test_unadmitted.py` once intact, where it must pass, and once for
each break below, where it must FAIL. Every break is on a decision point in
`LaneController` -- a gate on the idle read, a name, a reason, the flush, or
what the idle read writes -- and never on the fixture that drives it.

  pending   the idle read runs while a vend is pending, so the crossing that
            belongs to the vend is recorded as unadmitted and the vend is held
  outstanding
            the idle read runs while a settle's read of the same loops is
            outstanding: two readers of one board
  overlap   the poll's check and its read are two moments again, and a vend's
            read no longer waits for a poll's read in flight: two reads of one
            board overlap
  starve    the mutex is put back: a settle's read waits behind an outstanding
            read, so one hung loop driver leaves every later vend unconfirmable
            and the next ordinary arrival's `run_once` hung -- the defect the
            gate of 2026-09-19 found
  unlocked_transit
            the transit state is published outside the board lock again, so a
            vend can begin between the poll's pending check and its read
  split_check
            the pending check is taken outside the board lock and the read
            inside it: two moments -- the defect the re-gate of 2026-09-19
            found. Either half reverted, the poll takes the crossing of a car
            the lane admitted and writes it down as one nothing admitted
  route_waits
            the poll's read is taken under the board lock again, so a poll
            driver that hangs holds PENDING's publication and with it the vend
            route, after the relay has pulsed -- the defect the outside review
            of 2026-09-19 measured
  unbound_slot
            a handed crossing is taken by whichever read reaches the board
            first instead of by the transit it was handed for, so when that
            transit has already been answered by the settle's deadline the
            next car is confirmed on a crossing that is not its own -- the
            defect the gate of 2026-09-19 found
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
    ("outstanding", "the idle read ignores an outstanding read of the loops"),
    ("overlap", "a vend's read overlaps a poll's read in flight"),
    ("starve", "a settle's read waits behind an abandoned one"),
    ("unlocked_transit", "the transit state is published outside the board lock"),
    ("split_check", "the pending check and the read are two moments"),
    ("route_waits", "the vend route waits on the poll's read"),
    ("unbound_slot", "a handed crossing is taken by a read it was not handed to"),
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
