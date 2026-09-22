#!/usr/bin/env python3
"""The control for the outbox drain.

Runs tests/test_outbox_drain.py once intact, where it must pass, and once per
break, where it must fail. A drain that has never been seen to hold the lane
thread is not known to leave it alone.

  drain_on_the_lane_thread  the runner installs no drain: the flush runs on
                            the lane thread after the vend, as it did.
  signal_never_flushes      the signal is taken and nothing is sent.
  no_retry                  an outbox the platform refused waits for the
                            next car instead of the next tick.
  clear_after_send          the deques are cleared after the send, so what
                            was recorded during it is thrown away.
"""

from __future__ import annotations

import sys

from _control import intact, judge, run

SUITE = ["-q", "tests/test_outbox_drain.py"]
BREAKAGES = [
    ("drain_on_the_lane_thread", "the flush runs on the lane thread after the vend"),
    ("signal_never_flushes", "the signal is taken and nothing is sent"),
    ("no_retry", "a refused outbox waits for the next car"),
    ("clear_after_send", "what was recorded during the send is thrown away"),
]


failures = 0

print("== control A: the outbox-drain suite must PASS intact ==")
collected, _ = intact(SUITE, {"BREAK_OUTBOX_DRAIN": ""})
if collected < 0:
    failures += 1

print("\n== control B: each breakage must make it FAIL ==")
for mode, description in BREAKAGES:
    if not judge(mode, description, collected, run(SUITE, {"BREAK_OUTBOX_DRAIN": mode})):
        failures += 1

if failures:
    print(
        f"\n{failures} control(s) failed. Do not trust the outbox-drain tests.",
        file=sys.stderr,
    )
    sys.exit(1)
print("\nall controls OK — the outbox-drain suite fails when the drain is broken, as it must.")
