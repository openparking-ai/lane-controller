#!/usr/bin/env python3
"""The transit matrix: what each crossing becomes, frozen, so a change can be isolated.

THE METHOD, and why it is a script rather than a green suite: when the thing
that generates the cases is part of what changed, the cases are frozen HERE,
in a file that imports nothing but what `main` already has, and the same file
is run against `main` and against the branch. The rows are then compared. A
suite going green says the branch satisfies the branch's tests; two runs of one
fixed matrix say exactly which rows moved, and a row that moved without being
the one the change was for is a regression with its name printed.

Every row is one lane, built from the simulated seams, driven through
`run_once` a fixed number of times, with a fixed script of crossings on the
loops after the gate. What is printed per row is the sequence of event kinds
the lane recorded (the outbox, so session actions show too), the transit state
it ended in, and how many times the relay was pulsed. Nothing here reads a
clock, so the rows are the same on every machine.

`tests/test_transit_matrix.py` pins these rows. This script is the
human-readable form, and the one to run against a tree the test does not
belong to:

    PYTHONPATH=<other tree>/src python scripts/transit_matrix.py
"""

from __future__ import annotations

import json
import sys

from lane_controller import (
    CameraConfig,
    DecisionCache,
    GateConfig,
    LaneConfig,
    LaneController,
    LoopConfig,
    Rule,
    VehicleIdentity,
)
from lane_controller.interfaces import ClosingSequence
from lane_controller.simulated import (
    CannedCameraFeed,
    RecordingVendOutput,
    ScriptedClosingLoops,
    SimulatedLoopInput,
    StubVehicleIdentifier,
)

WINDOW = 10.0

ALLOWED = VehicleIdentity(plate="SIM-0001", confidence=0.97, presence=True)
BANNED = VehicleIdentity(plate="BANNED-1", confidence=0.97, presence=True)
UNSURE = VehicleIdentity(plate="SIM-0001", confidence=0.42, presence=True)
NOBODY = VehicleIdentity(plate=None, confidence=0.0, presence=False)

F = ClosingSequence.FORWARD
R = ClosingSequence.REVERSE


def build(*, direction="entry", closing_loops=2, crossings=(), identities=(), arrivals=1):
    config = LaneConfig(
        lane_id="lane-matrix",
        site_id="site-matrix",
        camera=CameraConfig(camera_id="sim-cam-1", rtsp_url="", frames_per_read=1),
        gate=GateConfig(),
        direction=direction,
        confidence_threshold=0.85,
        loops=LoopConfig(
            arming_loops=1,
            closing_loops=closing_loops,
            confirmation_window_seconds=WINDOW,
        ),
    )
    cache = DecisionCache()
    cache.load([Rule(plate="BANNED-1", allow=False)], default_action="allow")
    vend = RecordingVendOutput()
    controller = LaneController(
        config,
        loop=SimulatedLoopInput(arrivals=arrivals),
        camera=CannedCameraFeed(),
        vend=vend,
        identifier=StubVehicleIdentifier(list(identities) or [ALLOWED]),
        closing_loops=ScriptedClosingLoops(list(crossings)) if closing_loops == 2 else None,
        cache=cache,
    )
    return controller, vend


#: (name, kwargs for build, how many times run_once is called).
#:
#: `polls` beyond `arrivals` are IDLE polls: the arming loop reports nothing
#: and `run_once` returns None. That is the only way a lane is ever asked to
#: look at the loops after the gate with nothing pending, and it is the axis
#: this matrix exists to hold still.
ROWS: list[tuple[str, dict, int]] = [
    # -- the positive controls: §3h exactly as shipped ---------------------
    ("allow, A>B in window", dict(crossings=[(F, 3.0)]), 1),
    ("allow, B>A", dict(crossings=[(R, 3.0)]), 1),
    ("allow, nothing crosses", dict(crossings=[]), 1),
    ("allow, A>B after the window", dict(crossings=[(F, 30.0)]), 1),
    ("allow at a lane with no closing loops", dict(closing_loops=0), 1),
    # -- nothing pending, and a crossing -----------------------------------
    ("deny, then A>B on an idle poll", dict(identities=[BANNED], crossings=[(F, 3.0)]), 2),
    ("fallback, then A>B on an idle poll", dict(identities=[UNSURE], crossings=[(F, 3.0)]), 2),
    ("no vehicle, then A>B on an idle poll", dict(identities=[NOBODY], crossings=[(F, 3.0)]), 2),
    (
        "allow, held, then A>B on an idle poll",
        dict(crossings=[(ClosingSequence.NONE, 0.0), (F, 3.0)]),
        2,
    ),
    ("allow, A>B, then a second A>B on an idle poll", dict(crossings=[(F, 3.0), (F, 3.0)]), 2),
    ("deny, then B>A on an idle poll", dict(identities=[BANNED], crossings=[(R, 3.0)]), 2),
    ("idle poll, nothing on the loops", dict(crossings=[], arrivals=0), 1),
    (
        "deny at a lane with no closing loops, idle poll",
        dict(identities=[BANNED], closing_loops=0),
        2,
    ),
    # -- the exit side, which this matrix holds still and does not extend --
    ("exit: allow, A>B in window", dict(direction="exit", crossings=[(F, 3.0)]), 1),
    (
        "exit: deny, then A>B on an idle poll",
        dict(direction="exit", identities=[BANNED], crossings=[(F, 3.0)]),
        2,
    ),
]


def run_row(kwargs: dict, polls: int) -> dict:
    controller, vend = build(**kwargs)
    for _ in range(polls):
        controller.run_once()
    loops = controller.closing_loops
    return {
        "events": [event.kind for event in controller.events._queue],
        "transit": controller.transit_state,
        "vends": vend.vend_count,
        # How many scripted crossings NOTHING read. Off the fixture's own
        # counters rather than a property, so the same file runs on a tree
        # whose fixture has no property for it: a crossing left here is a
        # vehicle the loops reported and the lane never asked about.
        "unread_crossings": (len(loops._crossings) - loops._index) if loops is not None else 0,
    }


def matrix() -> dict[str, dict]:
    return {name: run_row(kwargs, polls) for name, kwargs, polls in ROWS}


def main(argv: list[str]) -> int:
    rows = matrix()
    if "--json" in argv:
        json.dump(rows, sys.stdout, indent=1)
        print()
        return 0
    width = max(len(name) for name in rows)
    for name, row in rows.items():
        print(
            f"{name.ljust(width)}  vends={row['vends']}  transit={row['transit']}  "
            f"unread={row['unread_crossings']}"
        )
        print(f"{''.ljust(width)}  {' > '.join(row['events']) or '(nothing)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
