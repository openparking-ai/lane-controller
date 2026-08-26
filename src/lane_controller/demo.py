"""Drive a simulated car through a simulated lane, against the real platform.

    python -m lane_controller.demo --credentials ../platform/.demo-credentials.json

No hardware and no vision model: the loop, the camera, the barrier and the
identifier are all the simulated implementations from `simulated`. The only
real thing in the picture is the platform, over HTTP.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import CameraConfig, GateConfig, LaneConfig
from .controller import LaneController
from .decision import DecisionCache
from .events import EventQueue
from .interfaces import VehicleIdentity
from .platform_client import PlatformClient
from .simulated import (
    CannedCameraFeed,
    RecordingVendOutput,
    SimulatedLoopInput,
    StubVehicleIdentifier,
)
from .sync import PlatformTransport, sync_rules

RULE = "─" * 62


def money(minor: int | None, currency: str) -> str:
    if minor is None:
        return "—"
    return f"{minor // 100}.{minor % 100:02d} {currency}"


def build_lane(credentials: dict, direction: str, plate: str, confidence: float, clock):
    token = credentials["entry_token"] if direction == "entry" else credentials["exit_token"]
    client = PlatformClient(credentials["base_url"], token)

    cache = DecisionCache()
    rules = sync_rules(client, cache)
    if rules is None:
        raise SystemExit(
            f"could not reach the platform at {credentials['base_url']}"
            " — is `npm run demo` running?"
        )

    config = LaneConfig(
        lane_id=f"demo-{direction}",
        site_id=credentials["garage_id"],
        camera=CameraConfig(camera_id="demo-cam", rtsp_url="", frames_per_read=2),
        gate=GateConfig(),
        direction=direction,
        confidence_threshold=0.85,
    )
    controller = LaneController(
        config,
        loop=SimulatedLoopInput(arrivals=1),
        camera=CannedCameraFeed(),
        vend=RecordingVendOutput(),
        identifier=StubVehicleIdentifier(
            [
                VehicleIdentity(
                    plate=plate,
                    plate_region="FL",
                    make="Toyota",
                    model="Corolla",
                    color="silver",
                    confidence=confidence,
                )
            ]
        ),
        cache=cache,
        events=EventQueue(PlatformTransport(client)),
        clock=clock,
    )
    return controller, rules, client


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--credentials",
        default="../platform/.demo-credentials.json",
        help="path to .demo-credentials.json written by the platform's `npm run demo`",
    )
    parser.add_argument("--plate", default="SIM-4271")
    parser.add_argument("--stay-hours", type=float, default=3.5)
    parser.add_argument(
        "--confidence",
        type=float,
        default=0.97,
        help="drop below 0.85 to watch the lane refuse to guess",
    )
    args = parser.parse_args(argv)

    path = Path(args.credentials)
    if not path.exists():
        print(
            f"no credentials at {path} — run `npm run demo` in the platform repo first",
            file=sys.stderr,
        )
        return 1
    credentials = json.loads(path.read_text())
    currency = credentials.get("currency", "USD")

    now = time.time()
    entered_at = now - args.stay_hours * 3600

    print(RULE)
    print(" Open Parking AI — simulated car through a simulated lane")
    print(f" platform {credentials['base_url']}   garage {credentials['garage_id'][:8]}")
    print(RULE)

    # --- entry ------------------------------------------------------------
    entry, rules, _ = build_lane(
        credentials, "entry", args.plate, args.confidence, lambda: entered_at
    )
    print(
        f"\n  rules synced: default={rules['default_action']}, "
        f"{money(rules['hourly_minor'], rules['currency'])}/hour"
    )
    print("\n  [entry lane] a car arms the loop")
    decision = entry.run_once()
    print(f"    identified   {args.plate}  confidence {args.confidence:.2f}")
    print(f"    decision     {decision.outcome.value.upper()}  ({decision.reason})")
    note = "   — the barrier will close on its own loop" if decision.should_vend else ""
    print(f"    gate         {'VENDED' if decision.should_vend else 'NOT OPENED'}{note}")
    if not decision.should_vend:
        fallback_name = decision.fallback.value if decision.fallback else "n/a"
        print(
            f"\n  fallback: {fallback_name} — nothing was guessed."
        )
        return 0

    # --- what the platform now believes -----------------------------------
    time.sleep(0.1)
    print(
        f"\n  [platform]   session opened at {time.strftime('%H:%M', time.localtime(entered_at))}"
    )

    # --- exit -------------------------------------------------------------
    exit_lane, _, client = build_lane(credentials, "exit", args.plate, args.confidence, lambda: now)
    print(f"\n  [exit lane]  {args.stay_hours:g} hours later, the same car arrives")
    exit_decision = exit_lane.run_once()
    print(f"    decision     {exit_decision.outcome.value.upper()}")
    print(f"    gate         {'VENDED' if exit_decision.should_vend else 'not opened'}")

    transport = exit_lane.events._transport
    closed = transport.last_close["session"] if transport.last_close else None
    if closed:
        rate = closed["hourly_minor_applied"]
        hours = closed["fee_minor"] // rate if rate else 0
        print("\n  [platform]   session CLOSED")
        billed = f"{money(rate, currency)}/h × {hours} h"
        print(f"    stay         {args.stay_hours:g} h  →  billed {billed}")
        print(f"    FEE          {money(closed['fee_minor'], currency)}")
    else:
        print("\n  the exit did not produce a closed session — check the platform log")
        return 1

    print(f"\n{RULE}")
    print(" No hardware was involved. No card number went anywhere.")
    print(RULE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
