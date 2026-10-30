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
     "    denied = owner_denies(cache, plate_seen(identity), stale=stale)\n",
     "    denied = None\n"),
    ("deny_on_read_dropped", "a denied read is presented", CONTROLLER,
     "        if owner_denies(self.cache, plate_seen(identity), stale=stale) is not None:\n",
     "        if False:\n"),
    ("deny_on_qr_car_dropped", "the QR's denied car is let in", CONTROLLER,
     '        if owner_denies(self.cache, _text(enrolment.get("qr_plate")), stale=stale) is not '
     'None:\n', "        if False:\n"),
    ("deny_normal_form_dropped", "a deny typed with a dash misses the QR's plate", DECISION,
     "        key = normalise_identity(plate)\n",
     "        return None\n        key = normalise_identity(plate)\n"),
    ("pass_not_recorded", "the rule is not on vended", CONTROLLER,
     '**({"qr_passed_by": passed_by} if passed_by else {}),', ""),
    ("pictures_not_kept", "no pictures of a passed car", CONTROLLER,
     "        if passed_by in QR_PASSES_KEPT:\n", "        if False:\n"),
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
