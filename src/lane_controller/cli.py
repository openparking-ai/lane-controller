"""The command line, and the module's STANDALONE face.

A lane with no platform and no identification service is a supported product,
not a degraded one, and this is how somebody runs it:

    lane-controller serve --config lane.toml

`serve` binds `127.0.0.1:8090` and publishes the contract described in
`docs/CONTRACT.md`. `--host` off loopback REQUIRES `--auth-token-file` AND
`--act-token-file`, and the service refuses to start without both: the second
is the only credential that authorises `POST /v1/lane/vend`, and without it
anything that can reach the port opens the barrier.

The lane it serves is built from the configuration file. The HARDWARE seams
-- the loops, the camera, the vend relay -- are the SIMULATED ones, because
this package ships no drivers; a real installation constructs its own
`LaneController` with its own hardware. The two SOFTWARE seams are real when
the configuration names them: the platform (`[lane] server_url`, with the
device token in the file `--platform-token-file` names) and the
identification service (`[lane] vehicle_id_url`). A configured platform gets
a real `PlatformClient`, the outbox drains to it, and the PRODUCTION LOOP runs
(`runner`): the lane serves cars on one thread and the cache is refreshed on
two cadences on another. Which seams are real and which are simulated is
printed on the lines the service starts with, per seam, so a lane serving
simulated hardware says so and a lane serving a real platform says that too.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import LaneConfig
from .controller import LaneController
from .decision import DecisionCache
from .durable import CacheDirectoryUnsafe, DurableStore
from .events import EventQueue
from .platform_client import PlatformClient
from .runner import LaneRunner
from .service import InsecureBind, LaneService, assert_bind_allowed, make_server
from .simulated import (
    CannedCameraFeed,
    OccupancyLoopInput,
    RecordingVendOutput,
    ScriptedClosingLoops,
    SimulatedLoopInput,
    StubVehicleIdentifier,
)
from .sync import PlatformTransport
from .vehicle_id_client import VehicleIdClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lane-controller", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="publish this lane's read contract")
    serve.add_argument(
        "--config",
        type=Path,
        required=True,
        help="the lane's TOML configuration. The [loops] geometry is DECLARED, never "
             "defaulted, and a file that does not say is refused here rather than at 3am",
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8090)
    serve.add_argument(
        "--auth-token-file",
        type=Path,
        help="a file holding the shared token every READ route must carry. Required for any "
             "--host that is not loopback. A FILE and not a value, because a value on the "
             "command line is readable by every user on the box for as long as the process runs",
    )
    serve.add_argument(
        "--act-token-file",
        type=Path,
        help="a file holding the SECOND token, the only one that authorises POST "
             "/v1/lane/vend. Also required for any --host that is not loopback, and for a "
             "larger reason than the first: without it, anything that can reach the port "
             "opens the barrier. A read token on the vend route is 403, and so is the act "
             "token on a read route",
    )
    serve.add_argument(
        "--platform-token-file",
        type=Path,
        help="a file holding this lane's DEVICE token for the platform named by [lane] "
             "server_url. Required when server_url is set; refused when it is not, because a "
             "credential with nothing to present it to is a credential lying around. A FILE, "
             "for the same reason as the other two",
    )
    return parser


def _token(path: Path | None) -> str | None:
    """A token, read from the file that holds it.

    ONE function for both credentials, because two copies of this rule would be
    two rules, and the copy is the one that stops refusing.

    An empty or whitespace-only file is not a token and is refused rather than
    read as "no token configured" -- which would be a truncated file silently
    turning the credential off on the one bind that requires one.
    """
    if not path:
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"could not read {path}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    token = raw.strip()
    if not token:
        print(f"{path} holds no token", file=sys.stderr)
        raise SystemExit(2)
    return token


def _simulated_lane(config: LaneConfig) -> LaneController:
    """A lane wired to the simulated seams, honouring the declared geometry.

    The geometry is the config's, not this function's: a file declaring two
    arming loops gets a second arming loop, and one declaring none gets none.
    `LaneController` refuses a lane whose declared geometry and wired hardware
    disagree, so building it any other way would fail here rather than lie.
    """
    return wire_lane(config, platform=None).controller


class WiredLane:
    """What `serve` built, and which of its seams are real."""

    def __init__(self, controller: LaneController, client: PlatformClient | None, seams: dict):
        self.controller = controller
        self.client = client
        #: seam name -> "REAL <what>" or "SIMULATED <why>", printed at start.
        self.seams = seams


def wire_lane(config: LaneConfig, *, platform: PlatformClient | None) -> WiredLane:
    """The lane `serve` runs: real software seams where configured, simulated
    hardware seams always, and a table saying which is which.

    The platform is REAL when a client is handed in: the outbox drains to it
    through `PlatformTransport`, and at an exit lane the close names its
    session through `find_open_session`, which is best effort by its own
    contract (offline, the close goes out without an id). The identifier is
    REAL when `[lane] vehicle_id_url` is set: this lane is then an ordinary
    client of Vehicle ID at that address, and `LaneService` reads the wiring
    to publish `has_identity_service`. Everything the package ships no driver
    for is simulated, and named as such.
    """
    seams: dict[str, str] = {}
    if platform is not None:
        events = EventQueue(PlatformTransport(platform))
        seams["platform"] = (
            f"REAL {platform.base_url} (outbox drains to it; refresh on two cadences)"
        )
        lookup = (
            (lambda plate: platform.find_open_session(plate=plate))
            if config.direction == "exit"
            else None
        )
    else:
        events = EventQueue()
        seams["platform"] = "NONE (standalone: nothing is reported, nothing is refreshed)"
        lookup = None
    if config.vehicle_id_url:
        identifier = VehicleIdClient(config.vehicle_id_url)
        seams["identifier"] = f"REAL Vehicle ID at {config.vehicle_id_url}"
    else:
        identifier = StubVehicleIdentifier()
        seams["identifier"] = "SIMULATED stub (no [lane] vehicle_id_url): every read is a fallback"
    if config.cache_path:
        # Refused, not warned, if the path is not this process's own: a
        # forged row here opens the barrier. `CacheDirectoryUnsafe` reaches
        # `cmd_serve`, which prints it and exits 2 before the port opens.
        cache = DecisionCache(
            max_age_seconds=config.rules_max_age_seconds, store=DurableStore(config.cache_path)
        )
        held = "restored" if cache.default_action is not None or cache.stays_cursor else "empty"
        seams["cache"] = (
            f"DURABLE {config.cache_path} ({held}; bound {config.rules_max_age_seconds:g}s, "
            "the same age at which the lane stops trusting it)"
        )
    else:
        cache = DecisionCache(max_age_seconds=config.rules_max_age_seconds)
        seams["cache"] = "MEMORY ONLY (no [lane] cache_path): a restart holds nothing"
    seams["loops"] = "SIMULATED (this package ships no loop driver)"
    seams["camera"] = "SIMULATED (this package ships no camera driver)"
    seams["barrier"] = (
        "SIMULATED (this package ships no relay driver: nothing here vends a barrier)"
    )
    controller = LaneController(
        config,
        loop=SimulatedLoopInput(arrivals=0),
        camera=CannedCameraFeed(camera_id=config.camera.camera_id),
        vend=RecordingVendOutput(),
        identifier=identifier,
        arming_loop_b=OccupancyLoopInput() if config.loops.arming_loops == 2 else None,
        closing_loops=ScriptedClosingLoops() if config.loops.confirms_entry else None,
        deactivate_loop=OccupancyLoopInput() if config.loops.has_deactivate_loop else None,
        cache=cache,
        events=events,
        session_lookup=lookup,
    )
    return WiredLane(controller, platform, seams)


def platform_client_for(config: LaneConfig, token_file: Path | None) -> PlatformClient | None:
    """The platform, or None for a standalone lane -- and a refusal for the two
    half-configured states, before anything is built.

    `server_url` with no token file is a lane that would report to a platform
    it cannot authenticate to, every send refused 401 and every refresh
    failing, for as long as it runs. A token file with no `server_url` is a
    credential read into memory with nowhere to present it. Neither is a
    configuration somebody meant, and both are said here.
    """
    token = _token(token_file)
    if config.server_url and token is None:
        print(
            f"\n[lane] server_url is set ({config.server_url}) but no --platform-token-file was "
            "given. A lane that reports to a platform needs the device token that platform "
            "issued for it; a lane that reports to nothing leaves server_url unset.\n",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if token is not None and not config.server_url:
        print(
            "\n--platform-token-file was given but [lane] server_url is not set: a credential "
            "with nothing to present it to. Set server_url, or drop the token.\n",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if token is None:
        return None
    return PlatformClient(config.server_url, token)


def cmd_serve(args) -> int:
    # The refusal BEFORE anything is built, so a configuration no file would
    # fix is reported in the moment rather than after a lane has been wired.
    token = _token(args.auth_token_file)
    act_token = _token(args.act_token_file)
    if token is not None and act_token is not None and token == act_token:
        # ONE file used twice is one credential, and the whole point of the
        # second one is that holding the reads does not buy the barrier. It
        # would pass every other check in this package silently.
        print(
            "\nthe read token and the act token are the same value. Two credentials that are "
            "one credential give a reader the barrier; use two different files.\n",
            file=sys.stderr,
        )
        return 2
    try:
        assert_bind_allowed(args.host, args.port, token, act_token)
    except InsecureBind as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2

    try:
        config = LaneConfig.from_file(args.config)
    except (OSError, ValueError, KeyError) as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2

    platform = platform_client_for(config, args.platform_token_file)
    try:
        wired = wire_lane(config, platform=platform)
    except CacheDirectoryUnsafe as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2
    service = LaneService(wired.controller)
    server = make_server(
        service, host=args.host, port=args.port, token=token, act_token=act_token
    )
    runner = LaneRunner(
        wired.controller,
        client=wired.client,
        rules_refresh_s=config.rules_refresh_seconds,
        stays_refresh_s=config.stays_refresh_seconds,
    )

    reach = "local only by design" if args.host in ("127.0.0.1", "::1", "localhost") else "EXPOSED"
    print(f"lane-controller on http://{args.host}:{args.port}  ({reach})")
    print(f"  lane {config.lane_id} at site {config.site_id}, direction {config.direction}")
    if service.can_vend():
        print("  POST /v1/lane/vend WILL PULSE THE VEND RELAY on this lane")
    else:
        print("  no act route on this lane: capabilities.can_vend is false")
    # Said out loud at the moment somebody starts it, PER SEAM, because a lane
    # answering the contract with no hardware behind it is the one thing an
    # evaluator could otherwise mistake for a working installation -- and a
    # lane with a real platform behind it is now a thing that exists, and the
    # line has to say which this is.
    for seam, what in wired.seams.items():
        print(f"  {seam:10} {what}")
    if wired.client is not None:
        print(
            f"  refresh    rules every {config.rules_refresh_seconds:g}s, "
            f"stays every {config.stays_refresh_seconds:g}s (the second is the size of the "
            "class of cars that cannot be priced at the barrier)"
        )
    if args.auth_token_file:
        print("  every read route requires the read bearer token")
    if args.act_token_file:
        print("  the vend route requires the ACT bearer token, which is a different one")
    # The production loop, beside the contract: the lane thread serves cars,
    # the refresh thread keeps the cache fresh, and this thread serves HTTP.
    runner.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        runner.stop()
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)
    return {"serve": cmd_serve}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
