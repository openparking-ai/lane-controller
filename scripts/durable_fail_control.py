#!/usr/bin/env python3
"""The control for the durable decision cache.

Runs tests/test_durable_cache.py once intact, where it must pass, and once per
break, where it must fail. A cache nobody has seen fail to survive a restart
is not known to survive one.

  not_persisted       writes go nowhere; a restart finds nothing.
  restart_resets_age  a restored cache reads as refreshed now.
  bound_not_enforced  personal data past its bound is held for ever.
  world_readable      the file is left at the umask.
  any_directory       a directory anyone can write is accepted.
  half_persisted      only the rules survive; the stays and registers do not.
"""

from __future__ import annotations

import sys

from _control import intact, judge, run

SUITE = ["-q", "tests/test_durable_cache.py"]
BREAKAGES = [
    ("not_persisted", "writes go nowhere"),
    ("restart_resets_age", "a restored cache reads as refreshed now"),
    ("bound_not_enforced", "data past its bound is held for ever"),
    ("world_readable", "the file is left at the umask"),
    ("any_directory", "a directory anyone can write is accepted"),
    ("half_persisted", "only the rules survive a restart"),
]


failures = 0

print("== control A: the durable-cache suite must PASS intact ==")
collected, _ = intact(SUITE, {"BREAK_DURABLE": ""})
if collected < 0:
    failures += 1

print("\n== control B: each breakage must make it FAIL ==")
for mode, description in BREAKAGES:
    if not judge(mode, description, collected, run(SUITE, {"BREAK_DURABLE": mode})):
        failures += 1

if failures:
    print(
        f"\n{failures} control(s) failed. Do not trust the durable-cache tests.",
        file=sys.stderr,
    )
    sys.exit(1)
print("\nall controls OK — the durable-cache suite fails when the store is broken, as it must.")
