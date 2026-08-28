import re
from pathlib import Path

import pytest

from lane_controller import (
    DecisionCache,
    LaneConfig,
    LaneController,
    VehicleIdentity,
)
from lane_controller.interfaces import ClosingSequence
from lane_controller.simulated import (
    CannedCameraFeed,
    OccupancyLoopInput,
    RecordingVendOutput,
    ScriptedClosingLoops,
    SimulatedLoopInput,
    StubVehicleIdentifier,
)
from lane_controller.sync import CONFIRMED, SESSION_OPEN, UNCONFIRMABLE

EXAMPLE = Path(__file__).parent.parent / "config" / "lane.example.toml"


def test_the_example_config_loads():
    config = LaneConfig.from_file(EXAMPLE)

    assert config.lane_id == "lane-1"
    assert config.camera.frames_per_read == 3
    assert config.confidence_threshold == 0.85


# ---------------------------------------------------------------------------
# The loop geometry is DECLARED. There is no default for it at the file
# boundary, and a key spelt wrong is a key that is missing.
# ---------------------------------------------------------------------------


def edited(tmp_path: Path, pattern: str, replacement: str) -> Path:
    """The example file with ONE line rewritten, and the edit proven to have landed.

    Anchored per line because `[loops]` also appears inside a comment in
    `[gate]`, and a substring cut there produces a file in which every case
    including the control reads as having no loops -- a probe that looks like it
    measures the table and measures the comment.
    """
    lines = EXAMPLE.read_text().splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if re.match(pattern, line)]
    assert len(hits) == 1, f"{pattern!r} matched {len(hits)} lines, not 1"
    lines[hits[0]] = replacement + "\n"
    path = tmp_path / "lane.toml"
    path.write_text("".join(lines))
    return path


def test_the_example_config_is_the_standard_installation():
    """The control for every refusal below: the file a site copies declares all
    five keys and loads. Without it a broken parser would pass the whole table."""
    loops = LaneConfig.from_file(EXAMPLE).loops

    assert loops.arming_loops == 2
    assert loops.closing_loops == 2
    assert loops.arming_spacing_m == 1.5
    assert loops.closing_spacing_m == 1.5
    assert loops.confirmation_window_seconds == 10.0
    assert loops.confirms_entry is True


@pytest.mark.parametrize(
    ("pattern", "replacement", "missing"),
    [
        (r"^\[loops\]", "[loop]", "arming_loops"),
        (r"^\[loops\]", "[Loops]", "arming_loops"),
        (r"^arming_loops\b", "arming_loop     = 2", "arming_loops"),
        (r"^closing_loops\b", "closing_loop     = 2", "closing_loops"),
        (r"^closing_loops\b", "closingloops     = 2", "closing_loops"),
        (r"^arming_spacing_m\b", "arming_spacing = 1.5", "arming_spacing_m"),
        (r"^closing_spacing_m\b", "closing_spacing = 1.5", "closing_spacing_m"),
        (
            r"^confirmation_window_seconds\b",
            "confirmation_window_second = 10.0",
            "confirmation_window_seconds",
        ),
    ],
)
def test_a_mistyped_loop_key_refuses_to_start_and_names_the_key(
    tmp_path, pattern, replacement, missing
):
    """Every typo in the table, and each one names the key it could not find.

    A typo used to be silent: the key fell through to a default, the lane ran,
    and every session it wrote said `unconfirmable` -- indistinguishable, in the
    record and to an operator, from a site that never installed the loops. Those
    are different facts about the same garage and only one of them is a
    decision."""
    with pytest.raises(ValueError, match=missing):
        LaneConfig.from_file(edited(tmp_path, pattern, replacement))


def test_a_config_with_no_loops_table_at_all_refuses_to_start(tmp_path):
    text = EXAMPLE.read_text()
    path = tmp_path / "lane.toml"
    path.write_text(text[: text.index("[loops]\n")])

    with pytest.raises(ValueError, match=r"no \[loops\] table"):
        LaneConfig.from_file(path)


def build_from(path: Path, crossings):
    """A whole lane wired from a CONFIG FILE, so the declaration is what drives it."""
    config = LaneConfig.from_file(path)
    cache = DecisionCache()
    cache.load([], default_action="allow")
    vend = RecordingVendOutput()
    controller = LaneController(
        config,
        loop=SimulatedLoopInput(arrivals=1),
        camera=CannedCameraFeed(),
        vend=vend,
        identifier=StubVehicleIdentifier([VehicleIdentity(plate="CFG-1", confidence=0.97)]),
        arming_loop_b=OccupancyLoopInput(True) if config.loops.arming_loops == 2 else None,
        closing_loops=ScriptedClosingLoops(crossings) if config.loops.confirms_entry else None,
        cache=cache,
    )
    return controller, vend


def kinds(controller):
    return [event.kind for event in list(controller.events._queue)]


def detail(controller, kind):
    return next(e for e in list(controller.events._queue) if e.kind == kind).detail


@pytest.mark.parametrize(
    ("crossings", "expected_event", "session"),
    [
        ([(ClosingSequence.FORWARD, 3.0)], "entry_confirmed", CONFIRMED),
        ([(ClosingSequence.REVERSE, 3.0)], "entry_backed_out", None),
        ([], "entry_held", None),
    ],
)
def test_a_declared_two_loop_lane_gets_the_three_outcomes(crossings, expected_event, session):
    controller, vend = build_from(EXAMPLE, crossings)

    controller.run_once()

    assert vend.vend_count == 1
    assert expected_event in kinds(controller)
    if session is None:
        assert SESSION_OPEN not in kinds(controller)
    else:
        assert detail(controller, SESSION_OPEN)["entry_confirmation"] == session


def test_a_site_that_declares_no_closing_loops_gets_unconfirmable_as_before(tmp_path):
    """A lane without the loops is not refused -- it writes the 0 itself, and
    what it gets is exactly what it got before any of this existed."""
    path = edited(tmp_path, r"^closing_loops\b", "closing_loops     = 0")
    controller, vend = build_from(path, [])

    controller.run_once()

    assert vend.vend_count == 1
    assert "entry_unconfirmable" in kinds(controller)
    assert detail(controller, SESSION_OPEN)["entry_confirmation"] == UNCONFIRMABLE


def test_the_gate_config_has_no_close_setting():
    """There is no close because the barrier closes itself. Asserted, not assumed."""
    from lane_controller import GateConfig

    fields = GateConfig.__dataclass_fields__.keys()
    assert not any("close" in f or "lower" in f for f in fields)
