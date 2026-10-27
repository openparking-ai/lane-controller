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
  twice                     a completed refusal is presented again
  unsure_plate_withheld     a plate read below the confidence is not sent
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

BREAKAGES = [
    ("any_answer_opens", "the gate opens on any answer", CONTROLLER,
     'if answer.get("open") is not True:', "if False:"),
    ("owner_overruled", "every entry refusal is QR-openable", DECISION,
     "    return decision.qr_may_open\n", "    return True\n"),
    ("banned_not_asked", "a banned car is presented", CONTROLLER,
     "if self._banned(identity) is not None:", "if False:"),
    ("car_gone_presented", "a QR is presented for an empty lane", CONTROLLER,
     "            or not self.loop.is_occupied()\n", ""),
    ("twice", "a completed refusal is presented again", CONTROLLER,
     "            or self.last_decision_completed_at is not None\n            or not qr_may_open",
     "            or not qr_may_open"),
    ("unsure_plate_withheld", "an unsure read is not sent", CONTROLLER,
     "vehicle = identity.plate or None",
     "vehicle = plate_read(identity, self.config.confidence_threshold)"),
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
