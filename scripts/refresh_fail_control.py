#!/usr/bin/env python3
"""The control for the production loop.

Runs tests/test_production_loop.py once intact, where it must pass, and once
per break, where it must fail. A loop nobody has seen fail to keep the cache
fresh is not known to keep it fresh.

  outage_empties   a module the platform could not read becomes an empty
                   register: the outage reads as "no pass holders".
  closed_kept      the delta never drops a car that left.
  no_fast_cadence  only the slow cadence ever runs: entry times reach the exit
                   lane every five minutes, not every five seconds.
  silent_failure   a refresh that could not run is neither counted nor said.
  thread_dies      a turn that raises kills the lane thread, health route
                   still answering.
  half_configured  `serve` accepts a server_url with no device token.
"""

from __future__ import annotations

import sys

from _control import intact, judge, run

SUITE = ["-q", "tests/test_production_loop.py"]
BREAKAGES = [
    ("outage_empties", "an outage is served as an empty register"),
    ("closed_kept", "the delta never drops a car that left"),
    ("no_fast_cadence", "only the slow cadence ever runs"),
    ("silent_failure", "a failed refresh is neither counted nor said"),
    ("thread_dies", "a raising turn kills the lane thread"),
    ("half_configured", "serve accepts a platform with no token"),
]


failures = 0

print("== control A: the production-loop suite must PASS intact ==")
collected, _ = intact(SUITE, {"BREAK_REFRESH": ""})
if collected < 0:
    failures += 1

print("\n== control B: each breakage must make it FAIL ==")
for mode, description in BREAKAGES:
    if not judge(mode, description, collected, run(SUITE, {"BREAK_REFRESH": mode})):
        failures += 1

if failures:
    print(
        f"\n{failures} control(s) failed. Do not trust the production-loop tests.",
        file=sys.stderr,
    )
    sys.exit(1)
print("\nall controls OK — the production-loop suite fails when the loop is broken, as it must.")
