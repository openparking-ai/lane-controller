"""The transit matrix, pinned.

`scripts/transit_matrix.py` is the frozen set of cases; this is what each one
becomes on THIS tree, written out in full so a row that moves is a diff with
its name on it and not a green suite that happens to still pass.

HOW IT WAS ESTABLISHED. The script was written before the change, run against
`main` (`df982e2`) and against the branch, and the two outputs compared row by
row. Eight rows identical. Five rows gained exactly one trailing
`entry_unadmitted` -- every one an idle poll with a forward crossing and
nothing pending -- with transit state and vend count unchanged. Two rows kept
their events and went from one unread crossing to none: a reverse with nothing
pending, and an exit lane's unpended crossing, both read and logged and neither
recorded. No row moved for any other reason. That comparison is in the round's
receipt; this file holds the branch side of it so the next change has a base.

The §3h answers -- A>B promotes, B>A closes, the window elapsing holds, a late
A>B holds, no loops is unconfirmable -- are the first five rows, and they are
the rows that did not move.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "transit_matrix.py"


def _load():
    spec = importlib.util.spec_from_file_location("transit_matrix", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXPECTED = {
    "allow, A>B in window": {
        "events": [
            "armed",
            "frames_captured",
            "vehicle_identified",
            "decision",
            "vended",
            "entry_pending",
            "entry_confirmed",
            "session_open",
        ],
        "transit": "confirmed",
        "vends": 1,
        "unread_crossings": 0,
    },
    "allow, B>A": {
        "events": [
            "armed",
            "frames_captured",
            "vehicle_identified",
            "decision",
            "vended",
            "entry_pending",
            "entry_backed_out",
        ],
        "transit": "backed_out",
        "vends": 1,
        "unread_crossings": 0,
    },
    "allow, nothing crosses": {
        "events": [
            "armed",
            "frames_captured",
            "vehicle_identified",
            "decision",
            "vended",
            "entry_pending",
            "entry_held",
        ],
        "transit": "held",
        "vends": 1,
        "unread_crossings": 0,
    },
    "allow, A>B after the window": {
        "events": [
            "armed",
            "frames_captured",
            "vehicle_identified",
            "decision",
            "vended",
            "entry_pending",
            "entry_held",
        ],
        "transit": "held",
        "vends": 1,
        "unread_crossings": 0,
    },
    "allow at a lane with no closing loops": {
        "events": [
            "armed",
            "frames_captured",
            "vehicle_identified",
            "decision",
            "vended",
            "entry_pending",
            "entry_unconfirmable",
            "session_open",
        ],
        "transit": "unconfirmable",
        "vends": 1,
        "unread_crossings": 0,
    },
    "deny, then A>B on an idle poll": {
        "events": [
            "armed",
            "frames_captured",
            "vehicle_identified",
            "decision",
            "entry_unadmitted",
        ],
        "transit": "none",
        "vends": 0,
        "unread_crossings": 0,
    },
    "fallback, then A>B on an idle poll": {
        "events": [
            "armed",
            "frames_captured",
            "vehicle_identified",
            "decision",
            "fallback_needs_human",
            "entry_unadmitted",
        ],
        "transit": "none",
        "vends": 0,
        "unread_crossings": 0,
    },
    "no vehicle, then A>B on an idle poll": {
        "events": [
            "armed",
            "frames_captured",
            "vehicle_identified",
            "decision",
            "arming_rejected",
            "entry_unadmitted",
        ],
        "transit": "none",
        "vends": 0,
        "unread_crossings": 0,
    },
    "allow, held, then A>B on an idle poll": {
        "events": [
            "armed",
            "frames_captured",
            "vehicle_identified",
            "decision",
            "vended",
            "entry_pending",
            "entry_held",
            "entry_unadmitted",
        ],
        "transit": "held",
        "vends": 1,
        "unread_crossings": 0,
    },
    "allow, A>B, then a second A>B on an idle poll": {
        "events": [
            "armed",
            "frames_captured",
            "vehicle_identified",
            "decision",
            "vended",
            "entry_pending",
            "entry_confirmed",
            "session_open",
            "entry_unadmitted",
        ],
        "transit": "confirmed",
        "vends": 1,
        "unread_crossings": 0,
    },
    "deny, then B>A on an idle poll": {
        "events": ["armed", "frames_captured", "vehicle_identified", "decision"],
        "transit": "none",
        "vends": 0,
        "unread_crossings": 0,
    },
    "idle poll, nothing on the loops": {
        "events": [],
        "transit": "none",
        "vends": 0,
        "unread_crossings": 0,
    },
    "deny at a lane with no closing loops, idle poll": {
        "events": ["armed", "frames_captured", "vehicle_identified", "decision"],
        "transit": "none",
        "vends": 0,
        "unread_crossings": 0,
    },
    "exit: allow, A>B in window": {
        "events": [
            "armed",
            "frames_captured",
            "vehicle_identified",
            "decision",
            "vended",
            "exit_pending",
            "exit_confirmed",
            "session_close",
        ],
        "transit": "confirmed",
        "vends": 1,
        "unread_crossings": 0,
    },
    "exit: deny, then A>B on an idle poll": {
        "events": ["armed", "frames_captured", "vehicle_identified", "decision"],
        "transit": "none",
        "vends": 0,
        "unread_crossings": 0,
    },
}


def test_the_matrix_is_what_it_was_when_the_change_was_isolated():
    rows = _load().matrix()
    assert list(rows) == list(EXPECTED), "the set of cases moved; freeze it again on purpose"
    for name, expected in EXPECTED.items():
        assert rows[name] == expected, f"row moved: {name}"


def test_the_positive_controls_are_the_rows_that_did_not_move():
    """The five §3h rows, by name, and what each still becomes."""
    rows = _load().matrix()
    assert rows["allow, A>B in window"]["events"][-2:] == ["entry_confirmed", "session_open"]
    assert rows["allow, B>A"]["events"][-1] == "entry_backed_out"
    assert rows["allow, nothing crosses"]["events"][-1] == "entry_held"
    assert rows["allow, A>B after the window"]["events"][-1] == "entry_held"
    assert rows["allow at a lane with no closing loops"]["events"][-2:] == [
        "entry_unconfirmable",
        "session_open",
    ]
    for name in (
        "allow, A>B in window",
        "allow, B>A",
        "allow, nothing crosses",
        "allow, A>B after the window",
        "allow at a lane with no closing loops",
    ):
        assert "entry_unadmitted" not in rows[name]["events"], name
