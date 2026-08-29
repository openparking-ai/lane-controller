#!/usr/bin/env python3
"""The control for "a dead engine is not a marginal read".

Runs the engine-unreachable suite once intact, where it must pass, and once for
each break below, where it must FAIL. Every break is on a line that carries the
guarantee -- the ordering inside `decide`, the classification inside the
client, or the cause reaching the event -- not on a fixture and not on a stub,
so a control that passes says the suite measures the guarantee rather than
something next to it.

  merge      `decide` stops looking at `unavailable`, so the zero confidence a
             failed request leaves behind is compared against a threshold again
             and a switched-off engine reports `low_confidence`. The defect
             exactly as it stood.
  blind      the CLIENT stops saying which failure it had, so the ordering has
             nothing to find. A different line, and one `merge` cannot reach.
  swallow    the arrow reversed: every fallback becomes `engine_unreachable`,
             so a plate the engine looked at and declined to vouch for reads as
             a service being down. Only the control catches this.
  onecause   every failure reports the same cause, so the record can no longer
             say whether to go and look at a camera or a service.
  nodetail   the cause is measured and never written to the event, so it does
             not leave the process.
  above_presence
             `unavailable` is looked at BEFORE presence, so a lane that
             measured nothing there while the engine was also down issues a
             ticket for a car that does not exist. The other ordering the round
             created, and the five breaks above all leave it green.
  freetext   the seam stops constraining `unavailable`, so an identifier that
             is not this package's client writes its own string into
             `events.detail`, which the retention purge cannot reach.

This also proves the suite RUNS. A guarantee that can quietly stop being
collected is not a guarantee -- and a suite that never ran would pass control A
and then fail to fail under every break, which is what control B reports.
"""

from __future__ import annotations

import os
import subprocess
import sys

SUITE = ["-q", "tests/test_engine_unreachable.py"]
BREAKAGES = [
    ("merge", "`decide` ignores which failure it was"),
    ("blind", "the client stops naming the failure"),
    ("swallow", "every fallback is reported as an unreachable engine"),
    ("onecause", "every failure reports the same cause"),
    ("nodetail", "the cause never reaches the event"),
    ("above_presence", "`unavailable` is checked before presence"),
    ("freetext", "the seam stops constraining `unavailable`"),
]


def run(env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    env = {**os.environ, **env_extra}
    return subprocess.run(
        [sys.executable, "-m", "pytest", *SUITE], env=env, capture_output=True, text=True
    )


def tail(result: subprocess.CompletedProcess, lines: int = 1) -> str:
    body = [line for line in result.stdout.strip().splitlines() if line.strip()]
    return " | ".join(body[-lines:]) if body else "(no output)"


failures = 0

print("== control A: the engine-unreachable suite must PASS intact ==")
intact = run({"BREAK_FALLBACK_CAUSE": ""})
if intact.returncode == 0:
    print(f"  control A OK — {tail(intact)}")
else:
    print(
        f"  CONTROL A FAILED — the suite does not pass even intact: {tail(intact)}", file=sys.stderr
    )
    print(intact.stdout, file=sys.stderr)
    failures += 1

print("\n== control B: each breakage must make it FAIL ==")
for mode, description in BREAKAGES:
    broken = run({"BREAK_FALLBACK_CAUSE": mode})
    if broken.returncode == 0:
        print(
            f"  {mode:9} *** PASSED WITH {description.upper()} —"
            " the suite is not measuring this ***",
            file=sys.stderr,
        )
        failures += 1
    else:
        print(f"  {mode:9} fails as required when {description} — {tail(broken)}")

if failures:
    print(
        f"\n{failures} control(s) failed. Do not trust the fallback-cause tests.",
        file=sys.stderr,
    )
    sys.exit(1)
print("\nall controls OK — the suite fails when the cause is merged back in, as it must.")
