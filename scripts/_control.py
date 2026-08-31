"""How a fail-control decides that a break BROKE something.

THE ONE PLACE, and it is one place because the alternative was measured and
failed. Every script here used to judge on the subprocess's EXIT STATUS: zero
meant the break did nothing, non-zero meant "fails as required". That is true
of a break that makes tests FAIL and it is equally true of one that makes them
ERROR -- and an anchor that has moved does exactly that. Renaming
`LaneController.arming_complete` to `arming_complete_v2` in a copy of the tree
made an autouse fixture's assertion fire on every test, 38 errors, exit code
non-zero, and the control printed:

    arming    fails as required when one arming loop is enough — 38 errors

...followed by `all controls OK` and exit 0, with the break never applied. The
repair that produced that message was the right diagnosis and it changed the
words in a line nothing parsed.

So the judgement is on THE SUMMARY LINE, and it requires three things of a
break, each of which is a different way for a control to be worthless:

  * `failed >= 1`   -- something has to have gone red. A break that changes
                       nothing is not a control.
  * `errors == 0`   -- an error is a suite that could not RUN. It is the shape
                       an anchor that has moved takes, and it is not evidence
                       about the guarantee.
  * `passed + failed == control A's collected count` -- the same tests ran. A
                       break that changes what is COLLECTED is measuring a
                       different suite from the intact run it is compared with.
  * `passed >= 1`   -- and something has to have stayed green. A break that
                       fails everything is a broken tree, not a control: it
                       cannot distinguish the guarantee from the harness.

A break that fails any of these is reported as ANCHOR NOT FOUND / NOT A CONTROL
and the script exits non-zero. It is never reported as "fails as required".
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

#: pytest's own summary line: `3 failed, 35 passed in 0.03s`, `38 errors in
#: 0.15s`, `291 passed in 21.35s`. Counted per word rather than positionally,
#: because the words appear in different orders and some of them are absent.
_COUNT = r"(\d+) {word}s?\b"


def counts(result: subprocess.CompletedProcess) -> dict[str, int]:
    """`{failed, passed, errors}` from the LAST summary line pytest printed."""
    text = result.stdout
    found = {}
    for word in ("failed", "passed", "error"):
        hits = re.findall(_COUNT.format(word=word), text)
        found["errors" if word == "error" else word] = int(hits[-1]) if hits else 0
    return found


def run(suite: list[str], env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    env = {**os.environ, **env_extra}
    return subprocess.run(
        [sys.executable, "-m", "pytest", *suite], env=env, capture_output=True, text=True
    )


def tail(result: subprocess.CompletedProcess, lines: int = 1) -> str:
    body = [line for line in result.stdout.strip().splitlines() if line.strip()]
    return " | ".join(body[-lines:]) if body else "(no output)"


def intact(suite: list[str], env_extra: dict[str, str]) -> tuple[int, subprocess.CompletedProcess]:
    """Control A. Returns the COLLECTED count every break is measured against.

    A collected count of zero is itself a failure: a suite that runs nothing
    passes, and every break under it would then "fail to fail" for a reason that
    has nothing to do with the guarantees.
    """
    result = run(suite, env_extra)
    got = counts(result)
    if result.returncode != 0 or got["failed"] or got["errors"] or got["passed"] < 1:
        print(
            f"  CONTROL A FAILED — the suite does not pass even intact: {tail(result)}",
            file=sys.stderr,
        )
        print(result.stdout, file=sys.stderr)
        return -1, result
    print(f"  control A OK — {got['passed']} passed")
    return got["passed"], result


def judge(
    name: str,
    why: str,
    collected: int,
    result: subprocess.CompletedProcess,
    width: int = 28,
) -> bool:
    """Report one break. True when it is a real control that went red."""
    got = counts(result)
    label = name.ljust(width)
    summary = f"{got['failed']} failed, {got['passed']} passed, {got['errors']} errors"

    if got["errors"]:
        print(
            f"  {label} *** ANCHOR NOT FOUND / NOT A CONTROL — {summary}: an ERROR is a suite "
            "that could not run, not a guarantee that went red ***",
            file=sys.stderr,
        )
        return False
    if got["failed"] < 1:
        print(
            f"  {label} *** PASSED WHEN {why.upper()} — the suite is not measuring this ***",
            file=sys.stderr,
        )
        return False
    if got["passed"] < 1:
        print(
            f"  {label} *** NOT A CONTROL — {summary}: the break fails EVERYTHING, so it "
            "cannot separate this guarantee from the harness ***",
            file=sys.stderr,
        )
        return False
    if got["passed"] + got["failed"] != collected:
        print(
            f"  {label} *** ANCHOR NOT FOUND / NOT A CONTROL — {summary} against control A's "
            f"{collected} collected: the break changed what RUNS ***",
            file=sys.stderr,
        )
        return False
    print(f"  {label} fails as required when {why} — {summary}")
    return True
