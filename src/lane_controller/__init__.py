"""Open Parking AI lane controller.

Built by 72 Knots Method by 72Knots.ai
"""

from .config import CameraConfig, GateConfig, LaneConfig, LoopConfig
from .controller import LaneController
from .decision import Decision, DecisionCache, Fallback, Outcome, Rule, decide
from .events import EventQueue, LaneEvent
from .interfaces import (
    CameraFeed,
    ClosingLoops,
    ClosingSequence,
    Frame,
    LoopInput,
    Unavailable,
    VehicleIdentifier,
    VehicleIdentity,
    VendOutput,
)
from .platform_client import PlatformClient, PlatformRejected, PlatformUnreachable
from .sync import PlatformTransport, sync_rules
from .vehicle_id_client import VehicleIdClient

__all__ = [
    "CameraConfig",
    "CameraFeed",
    "ClosingLoops",
    "ClosingSequence",
    "Decision",
    "DecisionCache",
    "EventQueue",
    "Fallback",
    "Frame",
    "GateConfig",
    "LaneConfig",
    "LaneController",
    "LaneEvent",
    "LoopConfig",
    "LoopInput",
    "Outcome",
    "PlatformClient",
    "PlatformRejected",
    "PlatformTransport",
    "PlatformUnreachable",
    "Rule",
    "Unavailable",
    "VehicleIdClient",
    "VehicleIdentifier",
    "VehicleIdentity",
    "VendOutput",
    "decide",
    "sync_rules",
]

__version__ = "0.1.0"
