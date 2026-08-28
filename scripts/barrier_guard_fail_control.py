#!/usr/bin/env python3
"""The control for the barrier guarantee.

Runs the barrier guard once intact, where it must pass, and once for each plant
below, where it must FAIL:

  protocol    VendOutput gains a second action, so the one interface that
              reaches the barrier can command it down
  impl        a relay implementation gains close(), which is the shape a driver
              for real hardware would arrive in
  call        the controller calls close() on the relay, with nothing declaring
              one anywhere -- planted into the source the guard reads
  controller  LaneController itself gains a method named for a boom going down

The guard asserts four separate things, so a control that only broke one of them
would leave three untested and read as coverage.

This also proves the guard RUNS. A guarantee that has quietly stopped being
collected passes control A and then fails to fail under every plant, which is
what control B reports.
"""

from __future__ import annotations

import os
import subprocess
import sys

SUITE = ["-q", "tests/test_barrier_guard.py"]
PLANTS = [
    ("protocol", "VendOutput declares a second action"),
    ("impl", "a relay implementation gains close()"),
    ("call", "the controller calls close() on the relay"),
    ("controller", "LaneController gains a method named for lowering the boom"),
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

print("== control A: the barrier guard must PASS intact ==")
intact = run({"BREAK_BARRIER_GUARD": ""})
if intact.returncode == 0:
    print(f"  control A OK — {tail(intact)}")
else:
    print(f"  CONTROL A FAILED — the guard does not pass intact: {tail(intact)}", file=sys.stderr)
    print(intact.stdout, file=sys.stderr)
    failures += 1

print("\n== control B: each plant must make it FAIL ==")
for mode, description in PLANTS:
    broken = run({"BREAK_BARRIER_GUARD": mode})
    if broken.returncode == 0:
        print(
            f"  {mode:11} *** PASSED WHILE {description.upper()} —"
            " the guard is not measuring this ***",
            file=sys.stderr,
        )
        failures += 1
    else:
        print(f"  {mode:11} fails as required when {description} — {tail(broken)}")

if failures:
    print(f"\n{failures} control(s) failed. Do not trust the barrier guard.", file=sys.stderr)
    sys.exit(1)
print("\nall controls OK — the guard fails when this package gains a way to close a barrier.")
