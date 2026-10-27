#!/usr/bin/env python3
"""The control for the entry as the owner set it (2026-10-26).

Runs tests/test_entry_always_a_stay.py once intact, where it must pass, and
once for each break below, where it must FAIL. Every break is on a decision of
the lane -- in `LaneController`, `decision`, `EventQueue`, the outbox file or
the platform client -- and never on the fixture that drives it.

  no_stay_without_open_command  a car nobody could read through the gate with
                                no open command is in no record, as before
  not_flagged                   the stay no open command let in is not flagged
  held_is_a_stay                an open command with nothing crossing is a stay
  stay_as_a_log_event           the stay is a log event: dropped, and lost on a restart
  unreadable_takes_the_plate    a plate read below the confidence is the stay's plate
  unreadable_sent_as_a_plate    "unreadable" goes on the wire as one shared plate
  arming_read_dropped           the frames the car was identified from are thrown away
  pictures_not_on_disk          the pictures are never written: a restart loses them
  pictures_capped               past 200 pictures, the rest are not kept
  pictures_all_or_nothing       the old send: all at once, a taken picture sent again
  transients_fall_back          the garage's word is not read: unread cars go to a person
  no_vehicle_opens              nothing present opens at a garage that takes transients
  full_ignored                  a garage that takes transients opens when it is full
  low_read_looked_up            a read below the confidence matches someone's pass
  settings_wiped                the garage's settings go with the cache at its bound
  pass_only_to_a_person         an uncovered car at a pass-only garage goes to a person
  disk_on_the_lane_thread       the stay's write holds the next car on the disk
  pictures_disk_on_the_lane_thread
                                the pictures' writes hold the next car on the disk
"""

from __future__ import annotations

import sys

from _control import intact, judge, run

SUITE = ["-q", "tests/test_entry_always_a_stay.py"]
BREAKAGES = [
    ("no_stay_without_open_command", "an unread car with no open command opens no stay"),
    ("not_flagged", "the stay no open command let in is not flagged"),
    ("held_is_a_stay", "an open command with nothing crossing is a stay"),
    ("stay_as_a_log_event", "the stay with no open command is a log event"),
    ("unreadable_takes_the_plate", "a read below the confidence is the stay's plate"),
    ("unreadable_sent_as_a_plate", "unreadable goes on the wire as a shared plate"),
    ("arming_read_dropped", "the arming read's frames are thrown away"),
    ("pictures_not_on_disk", "the pictures are never written to disk"),
    ("pictures_capped", "the pictures past 200 are not kept"),
    ("pictures_all_or_nothing", "the pictures go all or nothing"),
    ("transients_fall_back", "a garage that takes transients sends unread cars to a person"),
    ("no_vehicle_opens", "nothing present opens the gate"),
    ("full_ignored", "a garage that takes transients opens when it is full"),
    ("low_read_looked_up", "a read below the confidence is looked up"),
    ("settings_wiped", "the garage's entry settings are wiped at the bound"),
    ("pass_only_to_a_person", "an uncovered car at a pass-only garage goes to a person"),
    ("disk_on_the_lane_thread", "the stay's write holds the next car on the disk"),
    ("pictures_disk_on_the_lane_thread", "the pictures' writes hold the next car"),
]


failures = 0

print("== control A: the entry suite must PASS intact ==")
collected, _ = intact(SUITE, {"BREAK_ENTRY_STAY": ""})
if collected < 0:
    failures += 1

print("\n== control B: each breakage must make it FAIL ==")
for mode, description in BREAKAGES:
    if not judge(mode, description, collected, run(SUITE, {"BREAK_ENTRY_STAY": mode}), width=34):
        failures += 1

if failures:
    print(f"\n{failures} control(s) failed. Do not trust the entry tests.", file=sys.stderr)
    sys.exit(1)
print("\nall controls OK — the entry suite fails when the owner's entry rule is broken.")
