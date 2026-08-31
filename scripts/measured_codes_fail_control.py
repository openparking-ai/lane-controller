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
  health_blocks_on_the_service
                        the health route waits on the identification service for
                        as long as THAT client will, instead of for as long as
                        this lane said. A hung third machine is then published
                        as a dead lane.
  version_is_not_checked
                        the health payload is read whatever `schema_version` it
                        declares, and the value taken off it is published as
                        `measured`. The half-read both contracts refuse.
  skew_never_recovers   the skew counts only ever go up. `active` until the
                        process restarts, however long ago the clock was fixed,
                        and no recovery for a monitor to report.
  never_alarm_as_a_string
                        the flag ships as a string. Every non-empty string is
                        truthy, so `"false"` silences a code with nothing
                        anywhere reporting it.
  doc_set_missing_a_member
                        the document publishes one fewer malfunction code than
                        the enum holds. A lane an implementer cannot write.
  enum_gained_a_member  the code gains a malfunction code the document does not
                        publish, which is what every future round does the day
                        it adds one.

This also proves the suite RUNS. A suite that had quietly stopped being
collected would pass control A and then fail to fail under every break, which is
what control B reports.
"""

from __future__ import annotations

import sys

from _control import intact, judge, run

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
    ("health_blocks_on_the_service", "the health route waits on another machine's client"),
    ("version_is_not_checked", "the health payload is read on any schema_version"),
    ("skew_never_recovers", "the skew state can never leave active"),
    ("never_alarm_as_a_string", "never_alarm ships as a string instead of a boolean"),
    ("doc_set_missing_a_member", "the document publishes fewer codes than the enum holds"),
    ("enum_gained_a_member", "the enum gains a code the document does not publish"),
]


failures = 0

print("== control A: the contract suite must PASS intact ==")
collected, _ = intact(SUITE, {"BREAK_MEASURED_CODE": ""})
if collected < 0:
    failures += 1

print("\n== control B: each break must make it FAIL ==")
for mode, description in BREAKS:
    if not judge(mode, description, collected, run(SUITE, {"BREAK_MEASURED_CODE": mode})):
        failures += 1

if failures:
    print(
        f"\n{failures} control(s) failed. Do not trust the measured-code labels.",
        file=sys.stderr,
    )
    sys.exit(1)
print("\nall controls OK — a `measured` label here is a measurement, not a label.")
