#!/usr/bin/env python3
"""The control for the decision on the close (brief 4.9, the lane's half).

Runs tests/test_close_decision.py once intact, where it must pass, and once per
break, where it must fail. A close that has never been seen to carry the wrong
decision is not known to carry the right one.

  close_carries_nothing  the close records no decision at all.
  last_decision_read     the settle records the lane's LAST decision, not the
                         one it was handed -- the next car's, on a thread.
  transport_drops_it     the event carries it; the transport does not hand it
                         to the route.
  fee_edited_in_transit  the lane sends a fee other than the one it showed.
  client_drops_it        the client never puts it on the wire.
  null_not_absent        a close with no decision says `null` instead of
                         nothing.
"""

from __future__ import annotations

import sys

from _control import intact, judge, run

SUITE = ["-q", "tests/test_close_decision.py"]
BREAKAGES = [
    ("close_carries_nothing", "the close records no decision at all"),
    ("last_decision_read", "the settle records the last decision, not the one it was handed"),
    ("transport_drops_it", "the transport does not hand the decision to the route"),
    ("fee_edited_in_transit", "the lane sends a fee other than the one it showed"),
    ("client_drops_it", "the client never puts the decision on the wire"),
    ("null_not_absent", "a close with no decision says null instead of nothing"),
]


failures = 0

print("== control A: the close-decision suite must PASS intact ==")
collected, _ = intact(SUITE, {"BREAK_CLOSE_DECISION": ""})
if collected < 0:
    failures += 1

print("\n== control B: each breakage must make it FAIL ==")
for mode, description in BREAKAGES:
    if not judge(mode, description, collected, run(SUITE, {"BREAK_CLOSE_DECISION": mode})):
        failures += 1

if failures:
    print(
        f"\n{failures} control(s) failed. Do not trust the close-decision tests.",
        file=sys.stderr,
    )
    sys.exit(1)
print("\nall controls OK — the close-decision suite fails when the close is broken, as it must.")
