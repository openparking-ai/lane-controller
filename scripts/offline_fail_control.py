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

import os
import subprocess
import sys

SUITE = ["-q", "tests/test_offline.py"]
BREAKAGES = [
    ("drop", "the queue drops what it could not deliver"),
    ("noid", "delivery is retried without an idempotency key"),
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

print("== control A: the offline suite must PASS intact ==")
intact = run({"BREAK_OFFLINE_QUEUE": ""})
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
    broken = run({"BREAK_OFFLINE_QUEUE": mode})
    if broken.returncode == 0:
        print(
            f"  {mode:6} *** PASSED WITH {description.upper()} —"
            " the suite is not measuring this ***",
            file=sys.stderr,
        )
        failures += 1
    else:
        print(f"  {mode:6} fails as required when {description} — {tail(broken)}")

if failures:
    print(f"\n{failures} control(s) failed. Do not trust the offline tests.", file=sys.stderr)
    sys.exit(1)
print("\nall controls OK — the offline suite fails when the queue is broken, as it must.")
