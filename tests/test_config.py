from pathlib import Path

from lane_controller import LaneConfig


def test_the_example_config_loads():
    config = LaneConfig.from_file(Path(__file__).parent.parent / "config" / "lane.example.toml")

    assert config.lane_id == "lane-1"
    assert config.camera.frames_per_read == 3
    assert config.confidence_threshold == 0.85


def test_the_gate_config_has_no_close_setting():
    """There is no close because the barrier closes itself. Asserted, not assumed."""
    from lane_controller import GateConfig

    fields = GateConfig.__dataclass_fields__.keys()
    assert not any("close" in f or "lower" in f for f in fields)
