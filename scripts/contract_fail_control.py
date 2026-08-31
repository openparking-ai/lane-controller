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
  plant_post      a `do_POST` that is not the route table: it answers every
                  path, the four reads included. The act surface is
                  `ACT_ROUTES` and nothing else, and this is what keeps it so.
  vend_capability the capability without the route -- `can_vend: true` at a
                  lane that serves none. The mirror of `plant_post`, broken
                  separately because one derivation joins them and a control
                  that only breaks one end proves half of it.
  stored_fallback `fallback` stops being derived from `reason`, so a foreign
                  lane's own vocabulary arrives looking like one of our codes.
  no_reset        the cursor stops saying the process restarted, so an empty
                  list means both "nothing happened" and "you missed it all".
  extra_field     the code grows a field the document does not show. Only the
                  doc/contract agreement test can see this.
  session_actions_on_the_wire
                  every event goes in the read history again, so
                  `GET /v1/lane/events` publishes `session_open {plate: ...}`
                  -- the plate, on a READ contract, in a `detail` the contract
                  declares opaque and the retention purge cannot reach.
  plate_in_a_log_event
                  the same exposure through an ORDINARY log event, which the
                  mode above cannot prove: without this, the route sweep could
                  be a test of `SESSION_KINDS` rather than of the surface.
  evicted_reset   the eviction comparison removed. A consumer behind a full
                  256-deep window is served what survived and told `reset:
                  false`, which is indistinguishable from a complete page.
  depth_blind     `outbox_depth_growing` reads `dropped` -- events ALREADY LOST
                  -- instead of the depth its name promises. The HealthEntry
                  guard cannot catch it: the entry genuinely IS `measured`, of
                  something else.
  doc_values      the document publishes the opposite of what the code
                  enforces: `can_vend: true`, `contract_version: 99`, and a
                  rewritten `reference_not_recognised` row. The shape check
                  passes all three, because it discards every leaf value.

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

import sys

from _control import intact, judge, run

SUITE = ["-q", "tests/test_lane_contract.py", "tests/test_third_party_seat.py"]

CONTRACT_BREAKS = [
    ("geometry_copy", "the service renders its own geometry"),
    ("drop_code", "a malfunction code is missing from the payload"),
    ("unknown_is_ok", "an unmeasured code reports a clean bill of health"),
    ("plant_post", "a POST handler answers every path instead of the act routes"),
    ("vend_capability", "a lane that serves no act route claims it can vend"),
    ("stored_fallback", "`fallback` echoes any reason instead of being derived"),
    ("no_reset", "the cursor stops saying the process restarted"),
    ("extra_field", "the code carries a field the document does not show"),
    ("session_actions_on_the_wire", "a session action reaches the read contract"),
    ("plate_in_a_log_event", "a log event carries plate text"),
    ("evicted_reset", "an evicted cursor is not told it missed anything"),
    ("depth_blind", "the outbox depth code reads the drop count instead"),
    ("doc_values", "the document publishes values the code contradicts"),
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


CLEAN = {"BREAK_LANE_CONTRACT": "", "BREAK_THIRD_PARTY_LANE": ""}

failures = 0

print("== control A: the contract suite must PASS intact ==")
collected, _ = intact(SUITE, CLEAN)
if collected < 0:
    failures += 1

print("\n== control B: each breakage of the CONTRACT must make it FAIL ==")
for mode, description in CONTRACT_BREAKS:
    result = run(SUITE, {**CLEAN, "BREAK_LANE_CONTRACT": mode})
    if not judge(mode, description, collected, result):
        failures += 1

print("\n== control C: each breakage of the THIRD-PARTY LANE must make it FAIL ==")
for mode, description in SEAT_BREAKS:
    result = run(SUITE, {**CLEAN, "BREAK_THIRD_PARTY_LANE": mode})
    if not judge(mode, description, collected, result):
        failures += 1

if failures:
    print(
        f"\n{failures} control(s) failed. Do not trust the lane-contract tests.",
        file=sys.stderr,
    )
    sys.exit(1)
print("\nall controls OK — the suite fails on every property the contract exists to have.")
