#!/usr/bin/env python3
"""The control for the QR scanner (Phase 1b).

Runs tests/test_qr_scanner.py once intact, where it must pass, and once for
each break below, where it must FAIL. Every break is a plant in a COPY of the
tree -- `src/` of the copy goes first on the path -- and never in the fixture
that drives it; no tracked file is edited.

  any_answer_opens          the gate opens on whatever the module answered
  owner_overruled           every entry refusal is one a QR may open
  banned_not_asked          a QR lets in a car on the banned list
  car_gone_presented        a QR is presented with the arming loop clear
  deactivate_ignored        a QR is presented with a car on the deactivate loop
  second_arming_ignored     a QR is presented with the second arming loop clear
  twice                     a completed refusal is presented again
  unsure_plate_sent         a plate read below the confidence is sent as a read
  offline_silent            a QR that could not be checked leaves no record
  recorded_twice            the module's own could-not-answer recorded again
  token_in_event            the token rides on `qr_presented`
  token_logged              the token is in a log line
  plate_on_event            the plate rides on `qr_presented`
  exit_never                an exit the lane did not let out is never presented
  closed_exit_opens         an exit closed to everyone is presented
  wrong_route               the client posts somewhere the platform does not serve
  unanswered_code_lost      a 503's name is dropped, so it cannot be told from an outage
  not_wired                 a lane with a platform presents nothing
  not_covered_only          the gate opens on any answer but not covered
  no_recheck                the gate opens for a refusal a person completed meanwhile
  left_during_call          the gate opens for a car that left while the QR was checked
  watch_ignored             the loop's watch is not consulted: a swapped car is let in
  watch_never_started       the loop is not watched while the platform is asked
  final_read_dropped        the loop is not read again as the lane decides
  watch_error_trusted       a loop read that raises on the watch is taken as the car there
  deny_first_dropped        a read below the confidence gets around the owner's deny
  deny_on_read_dropped      the QR is presented for a car whose read the owner denies
  deny_on_qr_car_dropped    the QR opens for the car it was made for, which the owner denied
  deny_normal_form_dropped  a deny typed with a dash misses the QR's plate
  pass_not_recorded         the rule the module passed the QR on is not on `vended`
  pictures_not_kept         an opening by one difference or nothing read keeps no pictures
  plate_on_vended           the QR's plate rides on `vended`
  stale_deny_skipped        a deny on the QR's car lapses with rules past their age
  stale_deny_unrecorded     a deny applied from stale rules does not say so
  watch_time_rule_dropped   a clear longer than a flicker in one sample is a flicker
  qr_not_on_the_open        the stay a QR opened does not name the QR
  qr_not_on_the_close       the exit a QR let out does not name the QR on its close
  qr_not_sent               the transport leaves the QR off the open
  exit_direct_opener        a covered QR at an exit opens the barrier itself, case unsettled
  exit_case_not_reasked     a covered QR at an exit never asks the car's case again
  exit_old_cancel_dropped   the old exit case's payment cancel is not queued
  exit_no_stay_reasked      a case with no stay is asked again: a second open case
  exit_card_taken_reasked   a case is asked again while its card is being taken
  exit_qr_reused            the same QR asks the same car's case again and again
  exit_rolled_on_ignored    a clear longer than a flicker before the QR is ignored
  exit_vend_does_not_wait   a person's vend takes the case while the ask-again is out
  exit_claim_before_open    the new case is the car's before the platform opened it
  exit_stepped_for_person   a case just opened is stepped while a person waits for it
  exit_clear_after_answer   the answer's own time is counted as clear time
  exit_unsettled_alarm      a card being authorised tells a person
  exit_retries_unbounded    the ask-again is tried again without end
  exit_retry_new_request    a retry opens another case instead of asking again
  exit_wait_not_floored     a person's vend waits less than the client's timeout
  exit_stay_not_text        a stay id that is not text is taken for a stay
  exit_second_opening       the ask-again's case is taken as the car's though a person took it
  exit_idle_not_finally     an ask-again that raises leaves later person's vends waiting
  exit_vend_not_counted     a person's waiting vend is not counted, and their case is stepped
  exit_open_answer_trusted  an open's answer for another event is taken as the new case
  close_not_followed        a close taken on the old case keeps naming it after the supersede
  close_authority_lost      the close follows the supersede without the person's authority
  close_409_not_followed    the transport drops a close the platform refused as superseded
  close_followed_anywhere   a close is followed to a case that is not the lane's ask-again
  close_refused_silent      a close that cannot land is dropped without telling a person
  close_reads_unlocked      the close reads where it follows and who opened apart
  close_ended_shape         a supersede answered in another shape jams the outbox
  close_moved_shape         a superseded_by that is not text jams the outbox
  close_any_409_followed    a close refused for another reason is followed
  close_no_event_followed   a close carrying no ask-again event id is followed
  ask_again_taken_alarm     a refused ask-again on a case a person took tells a person
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from _control import counts, judge, tail

ROOT = Path(__file__).resolve().parent.parent
SUITE = ["-q", "tests/test_qr_scanner.py"]

CONTROLLER = "src/lane_controller/controller.py"
DECISION = "src/lane_controller/decision.py"
CLIENT = "src/lane_controller/platform_client.py"
CLI = "src/lane_controller/cli.py"
WATCH = "src/lane_controller/loop_watch.py"
EXIT_CASE = "src/lane_controller/exit_case.py"
SYNC = "src/lane_controller/sync.py"

BREAKAGES = [
    ("any_answer_opens", "the gate opens on any answer", CONTROLLER,
     'if answer.get("open") is not True:', "if False:"),
    ("owner_overruled", "every entry refusal is QR-openable", DECISION,
     "    return decision.qr_may_open\n", "    return True\n"),
    ("banned_not_asked", "a banned car is presented", CONTROLLER,
     "if self._banned(identity) is not None:", "if False:"),
    ("car_gone_presented", "a QR is presented for an empty lane", CONTROLLER,
     "            (not read or self._loop_now())\n", "            True\n"),
    ("deactivate_ignored", "a car behind does not keep the QR back", CONTROLLER,
     "            and not self.held_by_deactivate_loop(self.deactivate_loop)\n", ""),
    ("second_arming_ignored", "one arming loop of two is enough", CONTROLLER,
     "            and self.arming_complete(self.arming_loop_b)\n", ""),
    ("twice", "a completed refusal is presented again", CONTROLLER,
     "            or self.last_decision_completed_at is not None\n            or not qr_may_open",
     "            or not qr_may_open"),
    ("unsure_plate_sent", "an unsure read is sent as a read", CONTROLLER,
     "vehicle = plate_read(identity, self.config.confidence_threshold) or None",
     "vehicle = identity.plate or None"),
    ("offline_silent", "an unchecked QR leaves no record", CONTROLLER,
     "            if unanswered is not None:\n", "            if False:\n"),
    ("recorded_twice", "the platform's record is doubled", CONTROLLER,
     'unanswered = None if err.code == QR_UNANSWERED else "platform unreachable"',
     'unanswered = "platform unreachable"'),
    ("token_in_event", "the token rides on the event", CONTROLLER,
     "                    read_identity=vehicle is not None,\n                )",
     "                    read_identity=vehicle is not None,\n                    token=token,\n"
     "                )"),
    ("token_logged", "the token is logged", CONTROLLER,
     'log.info("lane %s: a QR could not be checked; the gate stays shut", lane)',
     'log.info("lane %s: QR %s could not be checked", lane, token)'),
    ("plate_on_event", "the plate rides on the event", CONTROLLER,
     "                    read_identity=vehicle is not None,\n",
     "                    read_identity=vehicle,\n"),
    ("exit_never", "an exit is never presented", DECISION,
     "        return (\n            decision.outcome is Outcome.FALLBACK and decision.fallback is "
     "not Fallback.LANE_CLOSED\n        )",
     "        return False"),
    ("closed_exit_opens", "a closed exit is presented", DECISION,
     " and decision.fallback is not Fallback.LANE_CLOSED\n", "\n"),
    ("wrong_route", "the client posts to the wrong route", CLIENT,
     '"/api/v1/lane/redeem-enrolment"', '"/api/v1/lane/redeem"'),
    ("unanswered_code_lost", "a 503 loses its name", CLIENT,
     'f"HTTP {err.code}: {body}", status=err.code, code=_refusal_code(body)',
     'f"HTTP {err.code}: {body}"'),
    ("not_wired", "a lane with a platform presents nothing", CLI,
     "qr_presenter = platform.redeem_enrolment", "qr_presenter = None"),
    ("not_covered_only", "the gate opens on anything but not covered", CONTROLLER,
     'if answer.get("open") is not True:', 'if answer.get("outcome") == "not_covered":'),
    ("no_recheck", "a completed refusal is vended again", CONTROLLER,
     "        if self.last_decision_at != decided_at or self.last_decision_completed_at is not "
     "None:\n", "        if False:\n"),
    ("left_during_call", "a car that left is opened for", CONTROLLER,
     "        if not (self._loop_now() and watch.stayed() and "
     "self._car_at_the_barrier(read=False)):\n", "        if False:\n"),
    ("watch_ignored", "the watch is not consulted", CONTROLLER,
     "self._loop_now() and watch.stayed() and", "self._loop_now() and True and"),
    ("watch_never_started", "the loop is not watched during the call", CONTROLLER,
     "watch = LoopWatch(self.loop, self._clock).start()",
     "watch = LoopWatch(self.loop, self._clock)"),
    ("final_read_dropped", "the loop is not read as the lane decides", CONTROLLER,
     "if not (self._loop_now() and watch.stayed()", "if not (True and watch.stayed()"),
    ("watch_error_trusted", "a failed loop read vouches for the car", WATCH,
     "            with self._lock:\n                self._left = True\n            return\n",
     "            return\n"),
    ("deny_first_dropped", "an unsure read gets around the owner's deny", DECISION,
     "        denied = owner_denies(cache, plate_seen(identity))\n",
     "        denied = None\n"),
    ("deny_on_read_dropped", "a denied read is presented", CONTROLLER,
     "        if owner_denies(self.cache, plate_seen(identity)) is not None:\n",
     "        if False:\n"),
    ("deny_on_qr_car_dropped", "the QR's denied car is let in", CONTROLLER,
     '        if owner_denies(self.cache, _text(enrolment.get("qr_plate"))) is not None:\n',
     "        if False:\n"),
    ("deny_normal_form_dropped", "a deny typed with a dash misses the QR's plate", DECISION,
     "        key = normalise_identity(plate)\n",
     "        return None\n        key = normalise_identity(plate)\n"),
    ("pass_not_recorded", "the rule is not on vended", CONTROLLER,
     '**({"qr_passed_by": passed_by} if passed_by else {}),', ""),
    ("pictures_not_kept", "no pictures of a passed car", CONTROLLER,
     "        if passed_by in QR_PASSES_KEPT:\n", "        if False:\n"),
    ("stale_deny_skipped", "a stale deny lapses", DECISION,
     "    return next((rule for plate in plates if (rule := cache.denying(plate)) is not None), "
     "None)",
     "    if cache.is_stale():\n        return None\n"
     "    return next((rule for plate in plates if (rule := cache.denying(plate)) is not None), "
     "None)"),
    ("stale_deny_unrecorded", "a stale deny is not said", CONTROLLER,
     '            **({"stale_deny": True} if self.cache.is_stale() else {}),\n', ""),
    ("watch_time_rule_dropped", "a long clear in one sample is a flicker", WATCH,
     "                if since is not None and now - since >= FLICKER_SECONDS:\n"
     "                    self._left = True\n", ""),
    ("qr_not_on_the_open", "the open does not name the QR", CONTROLLER,
     "                **self._take_qr_opening(at),\n                at=at,",
     "                at=at,"),
    ("qr_not_on_the_close", "the close does not name the QR", CONTROLLER,
     "            **self._take_qr_opening(at),\n            **({\"local_decision\"",
     "            **({\"local_decision\""),
    ("qr_not_sent", "the transport drops the QR", SYNC,
     '            **({"qr": event.detail["qr"]} if event.detail.get("qr") else {}),\n'
     "            # What was read below the confidence, not an identity (0036).\n",
     "            # What was read below the confidence, not an identity (0036).\n"),
    ("exit_direct_opener", "a QR at the exit opens directly", CONTROLLER,
     '        if self.config.direction == "exit":\n            # AT AN EXIT A QR OPENS NOTHING',
     "        if False:\n            # AT AN EXIT A QR OPENS NOTHING"),
    ("exit_case_not_reasked", "the exit case is not asked again", CONTROLLER,
     "        self._advance_exit_case(fresh)\n        return None\n\n    def _open_superseding",
     "        return None\n\n    def _open_superseding"),
    ("exit_old_cancel_dropped", "the old case's cancel is not queued", CONTROLLER,
     "never left behind.\n        self._cancel_on_reader(case)\n", "never left behind.\n"),
    ("exit_no_stay_reasked", "a case with no stay is asked again", CONTROLLER,
     "        if stay is None:\n            self._tell_qr_covered(",
     "        if False:\n            self._tell_qr_covered("),
    ("exit_card_taken_reasked", "asked again mid-payment", CONTROLLER,
     "            if exit_case.payment_status(case) not in exit_case.SUPERSEDE_PAYMENTS:\n",
     "            if False:\n"),
    ("exit_qr_reused", "the same QR asks again", CONTROLLER,
     "            if key in case.qr_used:\n", "            if False:\n"),
    ("exit_rolled_on_ignored", "a rolled-on car's QR asks again", CONTROLLER,
     "            rolled_on = cleared_for is not None and cleared_for >= "
     "exit_case.EXIT_FLICKER_SECONDS\n", "            rolled_on = False\n"),
    ("exit_vend_does_not_wait", "a person's vend does not wait", CONTROLLER,
     "        if not self._supersede_idle.wait(self._supersede_wait_seconds()):\n",
     "        if False:\n"),
    ("exit_claim_before_open", "the new case claimed before it is opened", CONTROLLER,
     "        try:\n            opened = self._open_superseding(case, fresh, qr_fields, key)\n",
     "        with self._exit_lock:\n            self._exit_case = fresh\n"
     "        try:\n            opened = self._open_superseding(fresh, fresh, qr_fields, key)\n"),
    ("exit_stepped_for_person", "a just-opened case stepped while a person waits", CONTROLLER,
     "        if person_waiting:\n", "        if False:\n"),
    ("exit_clear_after_answer", "clear measured after the answer", CONTROLLER,
     "            rolled_on = cleared_for is not None and cleared_for >= "
     "exit_case.EXIT_FLICKER_SECONDS\n",
     "            cleared_for = None if case.clear_since is None else "
     "self._clock() - case.clear_since\n"
     "            rolled_on = cleared_for is not None and cleared_for >= "
     "exit_case.EXIT_FLICKER_SECONDS\n"),
    ("exit_unsettled_alarm", "a card being authorised tells a person", CONTROLLER,
     "            log.info(\"lane %s: the platform could not ask the case again yet; it is "
     "asked \"\n",
     "            self._tell_qr_covered(case, key, \"planted\")\n"
     "            log.info(\"lane %s: the platform could not ask the case again yet; it is "
     "asked \"\n"),
    ("exit_retries_unbounded", "the ask-again retried without end", CONTROLLER,
     "if self._exit_case is case and case.qr_retries < exit_case.EXIT_QR_RETRIES:",
     "if self._exit_case is case:"),
    ("exit_retry_new_request", "a retry is a new request", CONTROLLER,
     "            if case.qr_fresh_event is None:\n", "            if True:\n"),
    ("exit_wait_not_floored", "the wait is not floored", CONTROLLER,
     "        return max(exit_case.EXIT_SUPERSEDE_WAIT_SECONDS, floor)",
     "        return exit_case.EXIT_SUPERSEDE_WAIT_SECONDS"),
    ("exit_stay_not_text", "a stay id not text is a stay", EXIT_CASE,
     "    return found if isinstance(found, str) and found else None",
     "    return found or None"),
    ("exit_second_opening", "the mine guard always true", CONTROLLER,
     "            mine = self._exit_case is case\n", "            mine = True\n"),
    ("exit_idle_not_finally", "idle set outside the finally", CONTROLLER,
     "        try:\n            opened = self._open_superseding(case, fresh, qr_fields, key)\n"
     "            with self._exit_lock:\n                person_waiting = self._vends_waiting > 0\n"
     "        finally:\n            self._supersede_idle.set()\n",
     "        opened = self._open_superseding(case, fresh, qr_fields, key)\n"
     "        with self._exit_lock:\n            person_waiting = self._vends_waiting > 0\n"
     "        self._supersede_idle.set()\n"),
    ("exit_vend_not_counted", "a waiting vend not counted", CONTROLLER,
     "            self._vends_waiting += 1\n", "            pass\n"),
    ("exit_open_answer_trusted", "the open's event id not checked", CONTROLLER,
     '            if answer.get("event_id") != fresh.event_id or not answer.get("id"):',
     '            if not answer.get("id"):'),
    ("close_not_followed", "the close does not follow the supersede", CONTROLLER,
     "                self._close_follows[case.exit_id] = fresh.exit_id\n",
     "                pass\n"),
    ("close_authority_lost", "let_out_by not re-pointed", CONTROLLER,
     "                    self._let_out_by = (fresh.exit_id, held[1])\n",
     "                    pass\n"),
    ("close_409_not_followed", "the transport does not follow", SYNC,
     '            moved = self._superseded_by(err, exit_id, '
     'event.detail.get("superseding_event"))',
     "            moved = None"),
    ("close_followed_anywhere", "followed without the own-ask-again check", SYNC,
     "            if not isinstance(taken, dict) or taken.get(\"event_id\") != asked_again:\n",
     "            if not isinstance(taken, dict):\n"),
    ("close_refused_silent", "a refused close dropped silently", SYNC,
     "                        self.on_close_refused(event, self.last_refused)",
     "                        pass"),
    ("close_reads_unlocked", "the close's reads apart", CONTROLLER,
     "        with self._exit_lock:\n"
     "            asked_again = self._ask_again_events.pop(exit_id, None) if exit_id else None\n"
     "            if exit_id is not None:\n"
     "                exit_id = self._close_follows.pop(exit_id, exit_id)\n"
     "            let_out_by = self._take_let_out_by(exit_id)\n",
     "        asked_again = self._ask_again_events.pop(exit_id, None) if exit_id else None\n"
     "        if exit_id is not None:\n"
     "            exit_id = self._close_follows.pop(exit_id, exit_id)\n"
     "        let_out_by = self._take_let_out_by(exit_id)\n"),
    ("close_ended_shape", "ended's shape not checked", SYNC,
     "            if not isinstance(ended, dict):\n                return None\n", ""),
    ("close_moved_shape", "superseded_by's shape not checked", SYNC,
     "            if not isinstance(moved, str) or not moved:\n", "            if not moved:\n"),
    ("close_any_409_followed", "the exit_closed code not checked", SYNC,
     '            or getattr(err, "code", None) != "exit_closed"\n', ""),
    ("close_no_event_followed", "no ask-again event, followed", SYNC,
     "            or not asked_again\n", ""),
    ("ask_again_taken_alarm", "a taken case's refused ask-again tells", CONTROLLER,
     "            if not taken:\n", "            if True:\n"),
    ("plate_on_vended", "the QR's plate rides on vended", CONTROLLER,
     '**({"qr_passed_by": passed_by} if passed_by else {}),',
     '**({"qr_passed_by": passed_by, "qr_plate": enrolment.get("qr_plate")} '
     'if passed_by else {}),'),
]


def stage() -> Path:
    copy = Path(tempfile.mkdtemp(prefix="lane-qr-control-"))
    for entry in ("src", "tests", "scripts"):
        shutil.copytree(ROOT / entry, copy / entry)
    shutil.copy(ROOT / "pyproject.toml", copy / "pyproject.toml")
    return copy


def plant(path: Path, before: str, after: str) -> bool:
    """Exactly once, or the plant measures nothing."""
    text = path.read_text()
    if text.count(before) != 1:
        return False
    path.write_text(text.replace(before, after))
    return True


def run_in(copy: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(copy / "src")}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *SUITE],
        cwd=copy, env=env, capture_output=True, text=True,
    )


failures = 0

print("== control A: the QR suite must PASS intact, in a copy ==")
copy = stage()
try:
    result = run_in(copy)
finally:
    shutil.rmtree(copy, ignore_errors=True)
got = counts(result)
if result.returncode != 0 or got["failed"] or got["errors"] or got["passed"] < 1:
    print(f"  CONTROL A FAILED — {tail(result)}", file=sys.stderr)
    print(result.stdout, file=sys.stderr)
    sys.exit(1)
collected = got["passed"]
print(f"  control A OK — {collected} passed")

print("\n== control B: each breakage must make it FAIL ==")
for name, why, file, before, after in BREAKAGES:
    copy = stage()
    try:
        if not plant(copy / file, before, after):
            print(f"  {name.ljust(24)} *** ANCHOR NOT FOUND in {file} ***", file=sys.stderr)
            failures += 1
            continue
        if not judge(name, why, collected, run_in(copy), width=24):
            failures += 1
    finally:
        shutil.rmtree(copy, ignore_errors=True)

if failures:
    print(f"\n{failures} control(s) failed. Do not trust the QR tests.", file=sys.stderr)
    sys.exit(1)
print("\nall controls OK — the QR suite fails when the QR scanner's guarantees are broken.")
