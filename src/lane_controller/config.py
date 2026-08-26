"""Lane configuration, read from TOML."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CameraConfig:
    camera_id: str
    rtsp_url: str
    frames_per_read: int = 3


@dataclass(frozen=True, slots=True)
class GateConfig:
    vend_pulse_ms: int = 500
    # There is no close setting. The barrier closes on its own closing loop.


@dataclass(frozen=True, slots=True)
class LaneConfig:
    lane_id: str
    site_id: str
    camera: CameraConfig
    gate: GateConfig
    # Below this, the lane takes its fallback path rather than deciding. The
    # default is deliberately high: the cost of a fallback is an operator
    # glancing at a screen, and the cost of a wrong open is a stranger's car
    # billed to somebody else.
    confidence_threshold: float = 0.85
    rules_max_age_seconds: float = 86_400.0
    server_url: str | None = None

    @classmethod
    def from_file(cls, path: str | Path) -> LaneConfig:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> LaneConfig:
        lane = raw.get("lane", {})
        camera = raw.get("camera", {})
        gate = raw.get("gate", {})
        return cls(
            lane_id=lane["id"],
            site_id=lane["site_id"],
            confidence_threshold=float(lane.get("confidence_threshold", 0.85)),
            rules_max_age_seconds=float(lane.get("rules_max_age_seconds", 86_400.0)),
            server_url=lane.get("server_url"),
            camera=CameraConfig(
                camera_id=camera.get("id", "cam-1"),
                rtsp_url=camera.get("rtsp_url", ""),
                frames_per_read=int(camera.get("frames_per_read", 3)),
            ),
            gate=GateConfig(vend_pulse_ms=int(gate.get("vend_pulse_ms", 500))),
        )
