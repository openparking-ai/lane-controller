"""Open Parking AI lane controller.

Built by 72 Knots Method by 72Knots.ai
"""

from .config import CameraConfig, GateConfig, LaneConfig, LoopConfig
from .contract import (
    CONTRACT_VERSION,
    Capabilities,
    EventPage,
    HealthEntry,
    HealthState,
    LaneDescription,
    LaneHealth,
    LaneState,
    LastDecision,
    MalfunctionCode,
    Source,
    Transit,
    TransitState,
)
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
from .service import InsecureBind, LaneService, make_server
from .sync import PlatformTransport, sync_rules
from .vehicle_id_client import VehicleIdClient

__all__ = [
    "CONTRACT_VERSION",
    "CameraConfig",
    "CameraFeed",
    "Capabilities",
    "ClosingLoops",
    "ClosingSequence",
    "Decision",
    "DecisionCache",
    "EventPage",
    "EventQueue",
    "Fallback",
    "Frame",
    "GateConfig",
    "HealthEntry",
    "HealthState",
    "InsecureBind",
    "LaneConfig",
    "LaneController",
    "LaneDescription",
    "LaneEvent",
    "LaneHealth",
    "LaneService",
    "LaneState",
    "LastDecision",
    "LoopConfig",
    "LoopInput",
    "MalfunctionCode",
    "Outcome",
    "PlatformClient",
    "PlatformRejected",
    "PlatformTransport",
    "PlatformUnreachable",
    "Rule",
    "Source",
    "Transit",
    "TransitState",
    "Unavailable",
    "VehicleIdClient",
    "VehicleIdentifier",
    "VehicleIdentity",
    "VendOutput",
    "decide",
    "make_server",
    "sync_rules",
]

__version__ = "0.1.0"
