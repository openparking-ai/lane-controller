#!/usr/bin/env python3
"""The control for the exit that opens only when settled (the owner's rule, 2026-10-27).

Runs tests/test_exit_case.py once intact, where it must pass, and once per
break, where it must fail. An exit nobody has seen open for an unpaid car is
not known to stay down for one.

  priced_opens               a car with a fee to pay is let out unpaid.
  unanswered_opens           a platform that cannot be reached is taken as settled.
  no_platform_opens          a lane with no platform wired opens for an unsettled car.
  plate_in_case              the case carries the plate text.
  unsure_plate_sent          a plate below the lane's confidence is sent as the car's ID.
  pictures_after_case        the case is opened before its pictures are sent.
  polled_too_fast            the case is asked about faster than Stripe is read.
  pay_every_turn             the fee is handed to the reader on every answer.
  decline_not_resent         after a decline the fee never reaches the reader again.
  left_case_opens            a car that left the loop is still let out.
  person_said_every_turn     a case that needs a person says so on every turn.
  person_and_case_both_open  a person opens, and the case opens the barrier again.
  close_not_named            the close does not name the case the car left on.
  no_receipt                 the text receipt the driver asked for is never sent.
  reader_driven_after_intent the lane drives its reader after the case's fee is on it.
  left_payment_stays_on_reader  a case let go of keeps its payment on the reader.
  person_close_unnamed       a person's vend leaves its case off the car's close.
  same_decline_not_resent    a second identical decline or a no-tap timeout is never re-sent.
  attempts_not_counted       a person is never told the driver has tried and failed.
  busy_reader_refused        a reader still showing the fee is taken as a refusal.
  vend_claims_outside_lock   a person's vend racing a settled case moves the barrier twice.
  entry_without_offset_sent  the driver's entry time goes without its timezone offset.
  refusal_settles            a platform refusal (4xx) is taken as settled.
  settled_truthy             `settled` is read as truthy rather than as `True`.
  let_out_ignores_person     the case opens though a person took it first.
  other_case_answer          a settled answer for another case opens this gate.
  receipt_before_vend        the text receipt is sent before the barrier opens.
  loop_flicker_drops         one clear sample of the loop drops the waiting car's case.
  rolled_on_car_let_out      a car that rolls on unarmed leaves on the backed-off car's case.
  flicker_ends_case          any clear sample ends the case's right to open.
  loop_sampled_slowly        the loop is sampled too seldom to tell a flicker from a car gone.
  busy_reader_never_told     a reader stuck on `reader_busy` alerts nobody.
  unpriced_case_reader_driven  the lane drives its reader for a case car it could not price.
  pay_answer_not_owned       a /pay answer for another case is taken for this one.
  first_answer_not_owned     the case's first answer is not checked against the car's event.
  route_busy_after_already_completed  the vend route stays busy after `already_completed`.
  cancel_before_next_frames  the last car's cancel holds up the next car's frames.
  left_during_call_let_out   a settled answer opens though the car backed off during the call.
  loop_unwatched_during_call the loop is not watched while the platform is asked.
  flicker_doubled            a clear of up to a second is taken for a flicker.
  busy_not_reset             busy answers before the reader took the fee still count.
  word_answer_not_owned      an answer to the driver's word for another event is taken.
  watcher_error_trusted      a loop read that raises on the watcher leaves its last sample vouching.
  loop_read_under_lock       the loop is read holding the lock a person's vend needs.
  stop_flicker_drops         one flicker as the call returns drops a paid car.
  decide_flicker_drops       one flicker as the lane decides drops a paid car.
  three_clear_samples        three clear samples are needed for a car gone, not two.
  watch_not_stopped_on_raise a platform call that raises leaves its watcher running.
  decide_not_read_now        the lane decides without reading the loop at that moment.
  no_final_sample            the watch stops without reading the loop as the call returns.
  person_checked_before_read a person's vend during the lane's last loop read is opened again.
  cancel_paid_failure        a cancel answered exit_paid is read as a failed cancel.
  cancel_paid_opens          a cancel answered exit_paid opens for a car the loop lost.
  in_progress_lets_out       a cancel answered payment_in_progress lets a car out.
  cancel_paid_asked_again    a cancel answered exit_paid is asked again.
  close_unsettled_dropped    a close answered 503 payment_unsettled is dropped, not resent.
  let_out_by_not_sent        a person's close does not say what they opened on.
  flagged_let_out_dropped    a close after a flagged opening goes without let_out_by.
  cancel_dropped             a cancel the platform did not confirm is never asked again.
  cancel_backoff_unbounded   the wait before a cancel is asked again has no bound.
  cancel_retried_on_lane_thread  the lane's thread asks the cancels again under the runner.
  cancel_not_kept_on_disk    a pending cancel is in memory only: a restart forgets it.
  cancel_not_asked_after_restart  the cancels held on disk are not asked after a restart.
  cancel_pending_never_told  a cancel that keeps failing tells nobody.
  cancel_refused_retried     a cancel the platform refuses outright is asked for ever.
  cancel_retry_blocks        a second retry pass waits on the one in flight.
  payment_guard_removed      a payment that is not open is handed to the reader again.
  plate_logged               the plate is written into a log line.
  no_payment_retried         a cancel answered no_payment is asked again for ever.
  first_cancel_on_lane_thread  the first cancel waits on the lane's thread.
  fee_not_held               the next car's fee goes on before the last car's cancel is confirmed.
  held_car_never_told        a car held behind an unconfirmed cancel never reaches a person.
  held_car_fee_sent          the held car's fee is sent once a person is told.
"""

from __future__ import annotations

import sys

from _control import intact, judge, run

SUITE = ["-q", "tests/test_exit_case.py"]
BREAKAGES = [
    ("priced_opens", "a car with a fee to pay is let out on the lane's copy, unpaid"),
    ("unanswered_opens", "a platform that cannot be reached is taken as a settled case"),
    ("no_platform_opens", "a lane with no platform wired opens for a car it could not settle"),
    ("plate_in_case", "the case carries the plate text"),
    ("unsure_plate_sent", "a plate read below the lane's confidence is sent as the car's ID"),
    ("pictures_after_case", "the case is opened before its pictures are sent"),
    ("polled_too_fast", "the case is asked about on every turn, faster than Stripe is read"),
    ("pay_every_turn", "the fee is handed to the reader again on every answer"),
    ("decline_not_resent", "after a decline the fee is never handed to the reader again"),
    ("left_case_opens", "a car that left the loop keeps its case and is let out"),
    ("person_said_every_turn", "a case that needs a person says so on every turn"),
    ("person_and_case_both_open", "a person opens for the car and its case opens again"),
    ("close_not_named", "the close does not name the case the car left on"),
    ("no_receipt", "the text receipt the driver asked for is never sent"),
    ("reader_driven_after_intent",
     "the lane drives its reader screen after the case's fee is on the reader"),
    ("left_payment_stays_on_reader", "a case the lane lets go of keeps its payment on the reader"),
    ("person_close_unnamed", "a person's vend leaves its case off the car's close"),
    ("same_decline_not_resent",
     "a second identical decline, or a reader that timed out, is never offered the fee again"),
    ("attempts_not_counted", "a person is never told the driver has tried and failed"),
    ("busy_reader_refused", "a reader still showing the fee is taken as the platform refusing it"),
    ("vend_claims_outside_lock",
     "a person's vend racing a case that settled moves the barrier a second time"),
    ("entry_without_offset_sent", "the driver's entry time goes without its timezone offset"),
    ("refusal_settles", "a platform refusal (4xx) of the case is taken as settled"),
    ("settled_truthy", "`settled` is read as truthy rather than as `True`"),
    ("let_out_ignores_person", "the case opens the barrier though a person took it first"),
    ("other_case_answer", "a settled answer naming another case opens this gate"),
    ("receipt_before_vend", "the text receipt is sent before the barrier opens"),
    ("loop_flicker_drops", "one clear sample of the arming loop drops the waiting car's case"),
    ("rolled_on_car_let_out",
     "a car that rolls on unarmed after the paid car backed off leaves on that car's case"),
    ("flicker_ends_case", "any clear sample of the loop ends the case's right to open"),
    ("loop_sampled_slowly",
     "the loop is sampled too seldom, while a car waits, to tell a flicker from a car gone"),
    ("busy_reader_never_told", "a reader that answers reader_busy for good alerts nobody"),
    ("unpriced_case_reader_driven",
     "the lane drives its reader screen for a case car it could not price"),
    ("pay_answer_not_owned", "a settled /pay answer for another case opens this gate"),
    ("first_answer_not_owned",
     "the case's first answer is taken without checking the car's own event"),
    ("route_busy_after_already_completed",
     "the vend route stays busy for good after refusing already_completed"),
    ("cancel_before_next_frames",
     "the last car's payment comes off the reader before the next car's frames are taken"),
    ("left_during_call_let_out",
     "a settled answer opens though the car backed off the loop while the platform was asked"),
    ("loop_unwatched_during_call",
     "the loop is not watched during a platform call, so a car swapped on is not seen"),
    ("flicker_doubled", "a clear of up to a second is taken for a flicker"),
    ("busy_not_reset", "busy answers from before the reader took the fee still count"),
    ("word_answer_not_owned", "a settled answer to the driver's word for another event opens"),
    ("watcher_error_trusted",
     "a loop read that raises on the watcher's thread leaves its last good sample vouching"),
    ("loop_read_under_lock", "the loop is read holding the lock a person's vend needs"),
    ("stop_flicker_drops", "one flicker of the loop as the call returns drops a paid car"),
    ("decide_flicker_drops", "one flicker of the loop as the lane decides drops a paid car"),
    ("three_clear_samples", "three clear samples in a row are needed for a car gone, not two"),
    ("watch_not_stopped_on_raise", "a platform call that raises leaves its watcher thread running"),
    ("decide_not_read_now", "the lane decides without reading the loop at that moment"),
    ("no_final_sample", "the watch stops without reading the loop as the call returns"),
    ("person_checked_before_read",
     "whether a person took the case is checked before the lane's last loop read, not after"),
    ("cancel_paid_failure", "a cancel answered exit_paid is read as a failed cancel"),
    ("cancel_paid_opens", "a cancel answered exit_paid opens for a car the loop lost"),
    ("in_progress_lets_out", "a cancel answered payment_in_progress lets a car out"),
    ("cancel_paid_asked_again", "a cancel answered exit_paid is asked again"),
    ("close_unsettled_dropped",
     "a close answered 503 payment_unsettled is dropped from the outbox, not sent again"),
    ("let_out_by_not_sent", "a person's close does not say what authority they opened on"),
    ("flagged_let_out_dropped", "a close after a flagged opening goes without let_out_by"),
    ("cancel_dropped", "a cancel the platform did not confirm is dropped, never asked again"),
    ("cancel_backoff_unbounded", "the wait before a cancel is asked again has no bound"),
    ("cancel_retried_on_lane_thread",
     "the lane's own thread asks the pending cancels again under the runner"),
    ("cancel_not_kept_on_disk", "a pending cancel is kept in memory only"),
    ("cancel_not_asked_after_restart", "the cancels held on disk are not asked after a restart"),
    ("cancel_pending_never_told", "a cancel that keeps failing tells nobody"),
    ("cancel_refused_retried", "a cancel the platform refuses outright is asked for ever"),
    ("cancel_retry_blocks", "a second retry pass waits on the one in flight"),
    ("payment_guard_removed", "a payment that is not open is handed to the reader again"),
    ("plate_logged", "the plate is written into a log line"),
    ("no_payment_retried", "a cancel answered no_payment is not taken as confirmed"),
    ("first_cancel_on_lane_thread",
     "the first cancel is asked, and waited on, on the lane's thread"),
    ("fee_not_held",
     "the next car's fee is handed to the reader before the last car's cancel is confirmed"),
    ("held_car_never_told", "a car held behind an unconfirmed cancel never reaches a person"),
    ("held_car_fee_sent", "the held car's fee is sent once a person has been told"),
]


failures = 0

print("== control A: the exit-case suite must PASS intact ==")
collected, _ = intact(SUITE, {"BREAK_EXIT_SETTLED": ""})
if collected < 0:
    failures += 1

print("\n== control B: each breakage must make it FAIL ==")
for mode, description in BREAKAGES:
    if not judge(mode, description, collected, run(SUITE, {"BREAK_EXIT_SETTLED": mode})):
        failures += 1

if failures:
    print(f"\n{failures} control(s) failed. Do not trust the exit-case tests.", file=sys.stderr)
    sys.exit(1)
print("\nall controls OK — the exit-case suite fails when the settled exit is broken, as it must.")
