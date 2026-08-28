#!/usr/bin/env python3
"""The control for the entry-confirmation guarantees.

Runs the loop suite once intact, where it must pass, and once for each break
below, where it must FAIL. Every break is on a decision point in
`LaneController` -- not on a fixture and not on a stub -- so a control that
passes says the suite measures the decision rather than something next to it.

  window     the configured confirmation window is ignored, so a crossing at
             any speed confirms
  elapsed    the window is still configured and stamped on the event, and the
             controller stops comparing it against the time the crossing took,
             so loops that report FORWARD late confirm
  direction  B-then-A confirms as well as A-then-B, so a vehicle backing out
             of the gate opens a session
  promote    the window elapsing with nothing confirms, which is the phantom
             occupant: a ticket no car ever followed, turned into money
  arming     one arming loop is enough again, so a piece of metal on a single
             loop arms the lane

This also proves the suite RUNS. A guarantee that can quietly stop being
collected is not a guarantee -- and a suite that never ran would pass control A
and then fail to fail under every break, which is what control B reports.
"""

from __future__ import annotations

import os
import subprocess
import sys

SUITE = ["-q", "tests/test_loops.py"]
BREAKAGES = [
    ("window", "the confirmation window is ignored"),
    ("elapsed", "the controller never compares the crossing against the window"),
    ("direction", "a vehicle backing out confirms the entry"),
    ("promote", "an unconfirmed entry is promoted to a session"),
    ("arming", "one arming loop is enough"),
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

print("== control A: the loop suite must PASS intact ==")
intact = run({"BREAK_CONFIRMATION": ""})
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
    broken = run({"BREAK_CONFIRMATION": mode})
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
    print(f"\n{failures} control(s) failed. Do not trust the confirmation tests.", file=sys.stderr)
    sys.exit(1)
print("\nall controls OK — the loop suite fails when confirmation is broken, as it must.")
