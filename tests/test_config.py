import math
import re
from pathlib import Path

import pytest

from lane_controller import (
    CameraConfig,
    DecisionCache,
    GateConfig,
    LaneConfig,
    LaneController,
    VehicleIdentity,
)
from lane_controller.config import (
    DEFAULT_ARMING_LOOP_MAX_OCCUPIED_S,
    DEFAULT_COMPLETION_MAX_AGE_S,
    DEFAULT_IDENTITY_HEALTH_TIMEOUT_S,
    DEFAULT_SETTLE_GRACE_S,
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


def test_the_example_config_publishes_every_per_site_duration():
    """The defaults a site copies, and they are the published ones.

    A number in a document is a claim; this reads it out of the file an
    installer actually copies and compares it against the constant the code
    defaults to, so the two cannot drift.
    """
    config = LaneConfig.from_file(EXAMPLE)

    assert config.identity_health_timeout_s == DEFAULT_IDENTITY_HEALTH_TIMEOUT_S == 1.0
    assert config.completion_max_age_s == DEFAULT_COMPLETION_MAX_AGE_S == 120.0
    assert config.settle_grace_s == DEFAULT_SETTLE_GRACE_S == 5.0
    assert config.arming_loop_max_occupied_s == DEFAULT_ARMING_LOOP_MAX_OCCUPIED_S == 600.0


# ---------------------------------------------------------------------------
# A NUMBER THAT IS NOT A NUMBER IS REFUSED, and TOML has two of them.
#
# `nan <= 0` is False and `inf <= 0` is False, so a validator that tests one
# side of zero admits both -- and `completion_max_age_s = nan` made
# `decision_stale` unreachable, because `x > nan` is False for every x. TOML 1.0
# has `nan` and `inf` as float literals, so that was a well-formed configuration
# file that silently removed a refusal on the route that opens a barrier.
# ---------------------------------------------------------------------------

DURATIONS = (
    "identity_health_timeout_s",
    "completion_max_age_s",
    "settle_grace_s",
    "arming_loop_max_occupied_s",
)


@pytest.mark.parametrize("name", DURATIONS)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_duration_that_is_not_finite_is_refused_by_name(name, value):
    with pytest.raises(ValueError, match=name):
        LaneConfig(
            lane_id="lane-1",
            site_id="site-1",
            camera=CameraConfig(camera_id="c", rtsp_url="", frames_per_read=1),
            gate=GateConfig(),
            **{name: value},
        )


@pytest.mark.parametrize("name", DURATIONS)
@pytest.mark.parametrize("value", [0, -1, 0.0, False, True, "120", None])
def test_a_duration_that_is_not_a_positive_number_is_refused_by_name(name, value):
    with pytest.raises(ValueError, match=name):
        LaneConfig(
            lane_id="lane-1",
            site_id="site-1",
            camera=CameraConfig(camera_id="c", rtsp_url="", frames_per_read=1),
            gate=GateConfig(),
            **{name: value},
        )


@pytest.mark.parametrize("name", DURATIONS)
def test_the_control_a_positive_finite_duration_is_accepted(name):
    """Otherwise the refusals above are a constructor that refuses everything."""
    config = LaneConfig(
        lane_id="lane-1",
        site_id="site-1",
        camera=CameraConfig(camera_id="c", rtsp_url="", frames_per_read=1),
        gate=GateConfig(),
        **{name: 7.5},
    )
    assert getattr(config, name) == 7.5


def test_nan_and_inf_reach_the_validator_from_a_real_toml_file(tmp_path):
    """The control on the control: TOML really does parse them.

    Without this the tests above measure a Python constructor and say nothing
    about what a site can write in `lane.toml`.
    """
    import tomllib

    parsed = tomllib.loads("[lane]\na = nan\nb = inf\nc = -inf\n")["lane"]
    assert math.isnan(parsed["a"]) and math.isinf(parsed["b"]) and math.isinf(parsed["c"])

    path = edited(tmp_path, r"^completion_max_age_s\b", "completion_max_age_s = nan")
    with pytest.raises(ValueError, match="completion_max_age_s"):
        LaneConfig.from_file(path)


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
