#!/usr/bin/env python3
"""The control for the lane contract, and for the third-party seat.

Runs the contract suite once intact, where it must pass, and once for each
break below, where it must FAIL. Every break is on a line that carries a
guarantee -- the geometry the service publishes, the completeness of the health
table, the invariant that `unknown` is not `ok`, the read-only sweep, the
derived fallback, the cursor's reset flag -- not on a fixture and not on a
stub, so a control that passes says the suite measures the guarantee rather
than something next to it.

  geometry_copy   the service renders its own geometry instead of publishing
                  the lane's. Identical for a default lane and wrong for every
                  other one, which is how a second copy fails: not at once.
  drop_code       one malfunction code left out of the payload. A consumer
                  cannot tell an absent code from a healthy one.
  unknown_is_ok   the invariant removed at the seam that enforces it AND at the
                  seam that produces it, so a code nothing measures reports a
                  clean bill of health.
  plant_post      a route that changes something, planted on the handler. The
                  read-only sweep is what keeps the act surface a later round.
  vend_capability the capability alone, without the route -- the mirror of
                  `plant_post`, broken separately because one derivation joins
                  them and a control that only breaks one end proves half of it.
  stored_fallback `fallback` stops being derived from `reason`, so a foreign
                  lane's own vocabulary arrives looking like one of our codes.
  no_reset        the cursor stops saying the process restarted, so an empty
                  list means both "nothing happened" and "you missed it all".
  extra_field     the code grows a field the document does not show. Only the
                  doc/contract agreement test can see this.

And the SEAT's own controls, on the third-party lane itself -- because a stub
that satisfies every assertion is a fixture that measures nothing:

  no_direction    a required field removed from its `GET /v1/lane`.
  can_vend        it claims it can vend.
  our_reason      it stops speaking its own vocabulary and speaks ours, so the
                  escalation case is no longer exercised by a foreign lane.
  no_transit      a required object removed from its state payload.
  short_health    its health payload is one code short.
  no_source_label its health entries stop saying where the answer came from.
  no_reset        its cursor stops saying it restarted.

This also proves the suites RUN. A guarantee that can quietly stop being
collected is not a guarantee -- and a suite that never ran would pass control A
and then fail to fail under every break, which is what control B reports.
"""

from __future__ import annotations

import os
import subprocess
import sys

SUITE = ["-q", "tests/test_lane_contract.py", "tests/test_third_party_seat.py"]

CONTRACT_BREAKS = [
    ("geometry_copy", "the service renders its own geometry"),
    ("drop_code", "a malfunction code is missing from the payload"),
    ("unknown_is_ok", "an unmeasured code reports a clean bill of health"),
    ("plant_post", "a route that changes something is planted on the handler"),
    ("vend_capability", "the lane claims it can vend"),
    ("stored_fallback", "`fallback` echoes any reason instead of being derived"),
    ("no_reset", "the cursor stops saying the process restarted"),
    ("extra_field", "the code carries a field the document does not show"),
]

SEAT_BREAKS = [
    ("no_direction", "the third-party lane drops a required field"),
    ("can_vend", "the third-party lane claims it can vend"),
    ("our_reason", "the third-party lane speaks our vocabulary"),
    ("no_transit", "the third-party lane drops its transit object"),
    ("short_health", "the third-party lane's health table is one code short"),
    ("no_source_label", "the third-party lane's codes stop naming their source"),
    ("no_reset", "the third-party lane's cursor stops saying it restarted"),
]


def run(env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    env = {**os.environ, **env_extra}
    return subprocess.run(
        [sys.executable, "-m", "pytest", *SUITE], env=env, capture_output=True, text=True
    )


def tail(result: subprocess.CompletedProcess, lines: int = 1) -> str:
    body = [line for line in result.stdout.strip().splitlines() if line.strip()]
    return " | ".join(body[-lines:]) if body else "(no output)"


CLEAN = {"BREAK_LANE_CONTRACT": "", "BREAK_THIRD_PARTY_LANE": ""}

failures = 0

print("== control A: the contract suite must PASS intact ==")
intact = run(CLEAN)
if intact.returncode == 0:
    print(f"  control A OK — {tail(intact)}")
else:
    print(
        f"  CONTROL A FAILED — the suite does not pass even intact: {tail(intact)}", file=sys.stderr
    )
    print(intact.stdout, file=sys.stderr)
    failures += 1

print("\n== control B: each breakage of the CONTRACT must make it FAIL ==")
for mode, description in CONTRACT_BREAKS:
    broken = run({**CLEAN, "BREAK_LANE_CONTRACT": mode})
    if broken.returncode == 0:
        print(
            f"  {mode:15} *** PASSED WITH {description.upper()} —"
            " the suite is not measuring this ***",
            file=sys.stderr,
        )
        failures += 1
    else:
        print(f"  {mode:15} fails as required when {description} — {tail(broken)}")

print("\n== control C: each breakage of the THIRD-PARTY LANE must make it FAIL ==")
for mode, description in SEAT_BREAKS:
    broken = run({**CLEAN, "BREAK_THIRD_PARTY_LANE": mode})
    if broken.returncode == 0:
        print(
            f"  {mode:15} *** PASSED WITH {description.upper()} —"
            " the seat test is not measuring this ***",
            file=sys.stderr,
        )
        failures += 1
    else:
        print(f"  {mode:15} fails as required when {description} — {tail(broken)}")

if failures:
    print(
        f"\n{failures} control(s) failed. Do not trust the lane-contract tests.",
        file=sys.stderr,
    )
    sys.exit(1)
print("\nall controls OK — the suite fails on every property the contract exists to have.")
