"""The command line, and the module's STANDALONE face.

A lane with no platform and no identification service is a supported product,
not a degraded one, and this is how somebody runs it:

    lane-controller serve --config lane.toml

`serve` binds `127.0.0.1:8090` and publishes the contract described in
`docs/CONTRACT.md`. `--host` off loopback REQUIRES `--auth-token-file` AND
`--act-token-file`, and the service refuses to start without both: the second
is the only credential that authorises `POST /v1/lane/vend`, and without it
anything that can reach the port opens the barrier.

The lane it serves is built from the configuration file and the SIMULATED
seams, because this package ships no drivers -- a real installation constructs
its own `LaneController` with its own hardware and passes it to `LaneService`.
That is stated rather than implied: `serve` is how the contract is exercised
and evaluated, and a lane serving simulated hardware says so on the line it
prints when it starts.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import LaneConfig
from .controller import LaneController
from .service import InsecureBind, LaneService, assert_bind_allowed, make_server
from .simulated import (
    CannedCameraFeed,
    OccupancyLoopInput,
    RecordingVendOutput,
    ScriptedClosingLoops,
    SimulatedLoopInput,
    StubVehicleIdentifier,
)


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
    return LaneController(
        config,
        loop=SimulatedLoopInput(arrivals=0),
        camera=CannedCameraFeed(camera_id=config.camera.camera_id),
        vend=RecordingVendOutput(),
        identifier=StubVehicleIdentifier(),
        arming_loop_b=OccupancyLoopInput() if config.loops.arming_loops == 2 else None,
        closing_loops=ScriptedClosingLoops() if config.loops.confirms_entry else None,
    )


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

    service = LaneService(_simulated_lane(config))
    server = make_server(
        service, host=args.host, port=args.port, token=token, act_token=act_token
    )

    reach = "local only by design" if args.host in ("127.0.0.1", "::1", "localhost") else "EXPOSED"
    print(f"lane-controller on http://{args.host}:{args.port}  ({reach})")
    print(f"  lane {config.lane_id} at site {config.site_id}, direction {config.direction}")
    if service.can_vend():
        print("  POST /v1/lane/vend WILL PULSE THE VEND RELAY on this lane")
    else:
        print("  no act route on this lane: capabilities.can_vend is false")
    # Said out loud at the moment somebody starts it, because a lane answering
    # the contract with no hardware behind it is the one thing an evaluator
    # could otherwise mistake for a working installation.
    print("  seams are SIMULATED: this serves the contract, it does not drive a barrier")
    if args.auth_token_file:
        print("  every read route requires the read bearer token")
    if args.act_token_file:
        print("  the vend route requires the ACT bearer token, which is a different one")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)
    return {"serve": cmd_serve}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
