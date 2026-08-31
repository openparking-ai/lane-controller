#!/usr/bin/env python3
"""The control for the offline guarantees.

Runs the offline suite three times: once intact, where it must pass, and twice
with the queue deliberately broken, where it must fail. A guarantee that has
never been seen to fail is not known to be a guarantee.

  drop  -- the queue clears whether or not delivery succeeded. This is what
           "offline-tolerant" looks like when it is only claimed.
  noid  -- delivery happens twice with a regenerated event id, which is an
           acknowledgement lost on the way back plus no idempotency key.
"""

from __future__ import annotations

import sys

from _control import intact, judge, run

SUITE = ["-q", "tests/test_offline.py"]
BREAKAGES = [
    ("drop", "the queue drops what it could not deliver"),
    ("noid", "delivery is retried without an idempotency key"),
]


failures = 0

print("== control A: the offline suite must PASS intact ==")
collected, _ = intact(SUITE, {"BREAK_OFFLINE_QUEUE": ""})
if collected < 0:
    failures += 1

print("\n== control B: each breakage must make it FAIL ==")
for mode, description in BREAKAGES:
    if not judge(mode, description, collected, run(SUITE, {"BREAK_OFFLINE_QUEUE": mode})):
        failures += 1

if failures:
    print(f"\n{failures} control(s) failed. Do not trust the offline tests.", file=sys.stderr)
    sys.exit(1)
print("\nall controls OK — the offline suite fails when the queue is broken, as it must.")
