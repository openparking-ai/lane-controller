#!/usr/bin/env python3
"""The control for the two codes this round moved from `not_measured` to `measured`.

A `measured` label is a claim that this build derives a state for a code from
something it observes. The label costs nothing to write and `HealthEntry` cannot
check it: that guard refuses a code claiming `ok` without being `measured`, and
says nothing about a code that is `measured` and derives a constant.

So each break below removes exactly one thing that makes a derivation a
MEASUREMENT, and the contract suite must go red. Every one of them fails in the
REASSURING direction -- an honest `unknown` or a real `active` becoming `ok` --
because that is the direction a health surface fails in when nobody is looking.

  degraded_is_ok        a service that cannot be READ reports healthy. `unknown`
                        folded into `ok` at the seam an operator trusts.
  degraded_never_fires  it asks, and never reports what it was told. A
                        derivation that cannot answer `active` reads exactly
                        like a service that is fine.
  degraded_is_remembered
                        the service is asked once and the answer kept. At a lane
                        with no arrivals since midnight the memory is the whole
                        night old -- a check turned into a memory.
  skew_from_the_message the refusal is classified by matching words in the
                        message instead of reading the field. A platform that
                        rewords its error goes silent; one that mentions the
                        words elsewhere goes loud.
  unnamed_is_not_a_skew an unnamed conflict is read as "not a skew". A platform
                        that predates the field refuses a skew exactly as it
                        refuses everything else, so this reports a healthy clock
                        on the one deployment where the failure is invisible.
  silence_is_ok         a lane that has attempted nothing reports its clock as
                        fine. Nothing was sent, so nothing could have been
                        refused: a confident negative about an unasked question.
  labelled_not_derived  a code marked `measured` with nothing deriving it. It
                        answers `unknown` for ever, which reads as "asked, and
                        could not tell" rather than as "never wired up" -- the
                        direction `HealthEntry` structurally cannot see.
  doc_stale_list        the document still lists a code this build now reads
                        among the ones waiting to be read. Applied to the parsed
                        paragraph, so no tracked document is edited here.

This also proves the suite RUNS. A suite that had quietly stopped being
collected would pass control A and then fail to fail under every break, which is
what control B reports.
"""

from __future__ import annotations

import os
import subprocess
import sys

SUITE = ["-q", "tests/test_lane_contract.py"]

BREAKS = [
    ("degraded_is_ok", "an unreadable identification service reports healthy"),
    ("degraded_never_fires", "the degraded derivation can never answer active"),
    ("degraded_is_remembered", "the service is asked once and the answer is kept"),
    ("skew_from_the_message", "the refusal is classified from message text"),
    ("unnamed_is_not_a_skew", "an unnamed conflict is read as not-a-skew"),
    ("silence_is_ok", "a lane that has sent nothing reports its clock as fine"),
    ("labelled_not_derived", "a code is labelled measured with nothing deriving it"),
    ("doc_stale_list", "the document still lists a code this build now reads"),
]


def run(mode: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "BREAK_MEASURED_CODE": mode}
    return subprocess.run(
        [sys.executable, "-m", "pytest", *SUITE], env=env, capture_output=True, text=True
    )


def tail(result: subprocess.CompletedProcess, lines: int = 1) -> str:
    body = [line for line in result.stdout.strip().splitlines() if line.strip()]
    return " | ".join(body[-lines:]) if body else "(no output)"


failures = 0

print("== control A: the contract suite must PASS intact ==")
intact = run("")
if intact.returncode == 0:
    print(f"  control A OK — {tail(intact)}")
else:
    print(f"  CONTROL A FAILED — the suite does not pass even intact: {tail(intact)}",
          file=sys.stderr)
    print(intact.stdout, file=sys.stderr)
    failures += 1

print("\n== control B: each break must make it FAIL ==")
for mode, description in BREAKS:
    broken = run(mode)
    if broken.returncode == 0:
        print(
            f"  {mode:22} *** PASSED WHEN {description.upper()} —"
            " the suite is not measuring this ***",
            file=sys.stderr,
        )
        failures += 1
    else:
        print(f"  {mode:22} fails as required when {description} — {tail(broken)}")

if failures:
    print(
        f"\n{failures} control(s) failed. Do not trust the measured-code states.",
        file=sys.stderr,
    )
    sys.exit(1)
print("\nall controls OK — a `measured` label here is a measurement, not a label.")
