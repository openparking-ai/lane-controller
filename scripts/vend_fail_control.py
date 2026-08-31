#!/usr/bin/env python3
"""The control for the assisted vend, and it is the most important one here.

`POST /v1/lane/vend` is the first route in this project that can open a
barrier. Every break below is a barrier that opens when it should not, a
credential that buys what it must not, or a record that is missing when one
does -- and each one is applied to a line that CARRIES a guarantee, not to a
fixture and not to a stub. The suite must go red for every one of them. A pass
is the failure.

The seven named refusals share one mechanism: the refusal is computed and then
thrown away. So a refusal added to `contract.VendRefusal` is broken here
without anything being remembered, and a control that passes says the suite
measures THAT refusal rather than something beside it.

  suppress_no_vehicle           the loop is not consulted; a caller with a
                                microphone opens a barrier with no car at it
  suppress_malfunction_active   a lane with an active fault vends anyway
  suppress_geometry_incomplete  one arming loop is enough -- the object that
                                cannot span the gap is completed in instead
  suppress_decision_stale       a ten-minute-old case opens the barrier for
                                whoever happens to be standing there now
  suppress_decision_mismatch    any `decision_at` completes; a caller no longer
                                has to name a case this lane is holding
  suppress_not_completable      an `allow` vends twice and a `deny` is
                                overturned by any authority at all
  suppress_busy                 two relays pulsed for one arrival

  presence_not_decides          the refusal reads the loop ITSELF rather than
                                going through `decide()`. It answers identically
                                today; it is the shape that drifts, and only the
                                test that perturbs `decide()` can see it
  relay_before_identity         THE ORDER SWAPPED. The barrier opens and the
                                record of who authorised it is written after --
                                which on a lane that loses power between the two
                                is written never
  replay_vends_again            a caller's retry is a second vend, after the car
                                has gone
  read_token_vends              the two tokens become one, so anything that may
                                READ this lane may open its barrier
  query_credential_served       a credential in a URL is accepted and the
                                request served, so a working integration
                                publishes its own token into every access log
  ticket_on_the_pending_event   the reference on the READ contract and in the
                                platform's append-only `events` table: the one
                                identity nothing could ever redact
  ticket_in_a_log_line          the same exposure by the other door, which the
                                payload sweep cannot see
  identifier_ticket_kept        a third-party IDENTIFIER mints a parking
                                identity through the interface that exists to
                                report what a camera saw
  vend_says_opened              the answer claims the barrier opened, which
                                nothing in this system measures
  ticket_shape_unchecked        any string at all is a ticket
  plate_asserted_by_a_caller    a caller may hand this lane a PLATE -- a
                                measurement it did not make, on the record that
                                prices the stay

  no_flush_after_the_vend       the barrier opens and nothing reaches the
                                platform until some later ordinary arrival --
                                and a restart before that loses the record
  decision_not_consumed         one arrival completed any number of times: two
                                keys, two pulses, two tickets, two billable
                                stays for one car
  compare_digest_on_str         the credential comparison goes back to `str`, so
                                an unauthenticated non-ASCII query string drops
                                the connection with a traceback instead of a 401
  key_recorded_on_the_event     the caller's raw idempotency key goes back onto
                                `assisted_identity`, which is append-only at the
                                platform: whatever is written there is permanent
  no_settle_deadline            the settle waits on a hung loop driver for ever,
                                so the lane refuses every completion `busy`
  subset_widened_to_all_codes   refusal 2 refuses on every measured code again,
                                so a lane whose engine is down refuses the
                                driver that engine could not read
  no_arming_dwell_measure       nothing measures the dwell, so a stuck arming
                                loop tells `no_vehicle` there is a car there
  finite_check_removed          `nan` and `inf` are legal durations again, and
                                `completion_max_age_s = nan` makes
                                `decision_stale` unreachable

This also proves the suite RUNS. A guarantee that can quietly stop being
collected is not a guarantee, and a suite that never ran would pass control A
and then fail to fail under every break, which is what control B reports.
"""

from __future__ import annotations

import sys

from _control import intact, judge, run

SUITE = [
    "-q",
    "tests/test_vend.py",
    "tests/test_lane_contract.py",
    "tests/test_loops.py",
    # The per-site DURATIONS the act surface rests on are validated here, and
    # `completion_max_age_s = nan` removes a refusal on the route that opens a
    # barrier. A control for it belongs with the route's other controls.
    "tests/test_config.py",
]

BREAKS = [
    ("suppress_no_vehicle", "the loop is not consulted at all"),
    ("suppress_malfunction_active", "a lane with an active fault vends anyway"),
    ("suppress_geometry_incomplete", "one arming loop is enough"),
    ("suppress_decision_stale", "an old case still opens the barrier"),
    ("suppress_decision_mismatch", "any decision_at completes"),
    ("suppress_not_completable", "an allow vends twice and a deny is overturned by anything"),
    ("suppress_busy", "two relays are pulsed for one arrival"),
    ("presence_not_decides", "the presence rule is a second copy that can drift"),
    ("relay_before_identity", "the barrier opens before the record is written"),
    ("replay_vends_again", "a retry is a second vend"),
    ("read_token_vends", "the read token opens the barrier"),
    ("query_credential_served", "a credential in a URL is served"),
    ("ticket_on_the_pending_event", "the ticket reaches the read contract"),
    ("ticket_in_a_log_line", "the ticket reaches a log line"),
    ("identifier_ticket_kept", "an identifier mints a parking identity"),
    ("vend_says_opened", "the answer claims the barrier opened"),
    ("ticket_shape_unchecked", "any string at all is a ticket"),
    ("plate_asserted_by_a_caller", "a caller may assert a plate"),
    ("no_flush_after_the_vend", "the completed vend never reaches the platform"),
    ("decision_not_consumed", "one arrival is completed any number of times"),
    ("compare_digest_on_str", "a non-ASCII credential crashes the handler"),
    ("key_recorded_on_the_event", "the caller's key is published on an append-only event"),
    ("no_settle_deadline", "a hung loop driver leaves the lane busy for ever"),
    ("subset_widened_to_all_codes", "every measured code refuses an assisted vend again"),
    ("no_arming_dwell_measure", "a stuck arming loop vends"),
    ("finite_check_removed", "nan and inf are legal durations again"),
]


CLEAN = {"BREAK_VEND": ""}

failures = 0

print("== control A: the vend suite must PASS intact ==")
collected, _ = intact(SUITE, CLEAN)
if collected < 0:
    failures += 1

print("\n== control B: each break must make it FAIL ==")
for mode, description in BREAKS:
    if not judge(mode, description, collected, run(SUITE, {"BREAK_VEND": mode})):
        failures += 1

if failures:
    print(f"\n{failures} control(s) failed. Do not trust the vend tests.", file=sys.stderr)
    sys.exit(1)
print("\nall controls OK — the suite fails on every property the act surface rests on.")
