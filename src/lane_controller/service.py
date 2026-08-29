"""The lane's local service. One HTTP contract, and everything is a client of it.

Until this existed, `lane-controller` was a library with no inbound surface:
`LaneController` was an object in a process and nothing outside that process
could ask what the lane had just decided. Our own intercom agent needs to ask,
and so does a third party's -- so it is a contract, not a private path, and our
software is an ordinary client of it.

**READ ONLY, for the whole of this contract version.** Four routes, all `GET`,
and there is deliberately no fifth:

    GET /v1/lane                 who this lane is, and what it can do
    GET /v1/lane/state           the last decision, and the current transit
    GET /v1/lane/health          every malfunction code, with its source
    GET /v1/lane/events?since=N  the event cursor

There is no vend route and no resolve route. `ACT_ROUTES` below is empty, and
`Capabilities.can_vend` is derived from it, so the capability cannot say `false`
while a route that opens a barrier exists. Every method other than `GET` is
answered by one shared refusal.

**Local, always.** It binds loopback by default and is meant to run on the same
device as the lane it describes. Off loopback it REFUSES to start without a
shared token -- `InsecureBind`, the same rule and the same shape the Vehicle ID
service applies, because the exposure is the same: on a lane's own LAN, this
publishes where a vehicle was, when, and what the lane decided about it.

**No state store, and none is added here.** The service reads what the
controller holds in memory. A restart loses the last decision, and
`GET /v1/lane/state` then says `none` with `since: null` rather than reporting
the last thing it happens to remember.

Written on `http.server` rather than a framework, for the reason the engine's
service is: this runs in a gate housing on a Jetson, and a dependency-free
service is one fewer thing to keep patched.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

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
    Transit,
)
from .interfaces import Unavailable
from .sync import to_iso
from .vehicle_id_client import VehicleIdClient

log = logging.getLogger(__name__)

#: The routes this service answers, and every one of them is a GET.
READ_ROUTES: tuple[str, ...] = (
    "/v1/lane",
    "/v1/lane/state",
    "/v1/lane/health",
    "/v1/lane/events",
)

#: Routes that CHANGE something -- a vend, a resolve, anything that moves a
#: barrier or a session. EMPTY, and it is empty by decision: the act surface is
#: a later round, deliberately after the display and the agent, because it is
#: the first thing that can open a barrier.
#:
#: `Capabilities.can_vend` is derived from this tuple. Adding a route here
#: without building the refusals behind it therefore changes what the lane
#: TELLS a consumer it can do, in the same commit, which is the point.
ACT_ROUTES: tuple[str, ...] = ()

MAX_QUERY_CURSOR = 2**63 - 1


class InsecureBind(Exception):
    """A bind that would expose the lane's decisions with nothing in front of them.

    Raised before the socket is created. Copied from the Vehicle ID service
    rather than re-derived: one rule, the same words, so the two surfaces on
    one device cannot come to disagree about when a credential is required.
    """


def is_loopback(host: str) -> bool:
    """Whether binding to `host` keeps the service on this machine.

    Anything this cannot PROVE is loopback counts as not loopback. A hostname
    resolves at bind time, can resolve to more than one address, and can change
    under the service, so guessing here would mean guessing on the one question
    that decides whether a credential is required. `''` is every interface and
    is the widest of the lot.
    """
    if host in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def assert_bind_allowed(host: str, port: int, token: str | None) -> None:
    """Raise unless this host may be bound with this credential.

    One implementation, called by `make_server` -- so the rule holds for every
    caller of this package -- and by the CLI before it builds anything, so a
    misconfiguration is reported in the moment.
    """
    if is_loopback(host) or token:
        return
    raise InsecureBind(
        f"refusing to bind {host or 'every interface'}:{port} with no token. Off loopback "
        "anything that can reach this port can read where a vehicle was, when it was there, "
        "and what this lane decided about it. Configure a shared token with --token-file, "
        "or bind 127.0.0.1."
    )


def bearer(header: str | None) -> str | None:
    """The token out of an Authorization header, or None if there is not one."""
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


class LaneService:
    """A `LaneController`, read through the contract.

    It holds nothing of its own. Every answer below is computed from the
    controller at the moment it is asked, so there is no second copy of the
    lane's state to go stale, and nothing here can report something the lane
    does not currently hold.
    """

    def __init__(self, controller) -> None:
        self.controller = controller
        self._lock = threading.Lock()

    # --- GET /v1/lane -----------------------------------------------------

    def capabilities(self) -> Capabilities:
        controller = self.controller
        return Capabilities(
            # The declared geometry, which is also what the lane writes on
            # every vehicle. Two closing loops confirm; anything else cannot.
            confirms_entry=controller.config.loops.confirms_entry,
            # The WIRING, not a flag. A lane whose identifier is our Vehicle ID
            # client has an identity service; a lane running a stub, or
            # identifying some other way, does not, and says so.
            has_identity_service=isinstance(controller.identifier, VehicleIdClient),
            # Standalone is a supported mode, not a degraded one: a lane with
            # no outbox transport has no platform, and that is an answer rather
            # than a fault.
            has_platform=controller.events.transport is not None,
            # There is no display seam in this package, so nothing sets this
            # attribute and the answer is `false`. Derived from the wiring so
            # that the day a display is wired, this follows it instead of
            # needing to be remembered.
            has_display=getattr(controller, "display", None) is not None,
            # From the route table, not from a constant. See ACT_ROUTES.
            can_vend=bool(ACT_ROUTES),
        )

    def describe(self) -> LaneDescription:
        controller = self.controller
        return LaneDescription(
            lane_id=controller.config.lane_id,
            site_id=controller.config.site_id,
            direction=controller.config.direction,
            # EXACTLY what the events carry, from the one method that produces
            # it. Not re-keyed and not re-built here: a second rendering of the
            # geometry is a second thing to go stale, and this lane already
            # publishes `geometry_assumed` on every vehicle.
            geometry=controller.config.loops.as_published(),
            capabilities=self.capabilities(),
        )

    # --- GET /v1/lane/state -----------------------------------------------

    def state(self) -> LaneState:
        controller = self.controller
        decision = controller.last_decision
        last = None
        if decision is not None and controller.last_decision_at is not None:
            last = LastDecision(
                outcome=decision.outcome.value,
                # The machine-readable reason. Our lane's is the `Fallback`
                # member when it fell back, and the outcome's own name when it
                # did not -- so the field is never empty and never a sentence
                # with a number interpolated into it. `LastDecision.fallback`
                # is derived from this and is null for anything outside our
                # closed set, which is what tells a consumer to escalate.
                reason=(
                    decision.fallback.value
                    if decision.fallback is not None
                    else decision.outcome.value
                ),
                at=controller.last_decision_at,
                presence=decision.identity.presence,
                cause=controller.last_cause,
                read_ref=controller.last_read_ref,
            )
        return LaneState(
            decision=last,
            transit=Transit(
                state=controller.transit_state,
                since=controller.transit_since,
            ),
        )

    # --- GET /v1/lane/health ----------------------------------------------

    def health(self) -> LaneHealth:
        """Every code in the table, with the state this build can stand behind.

        Three codes are DERIVED here and the rest answer `unknown`. That is not
        a placeholder: `HealthEntry` refuses any state but `unknown` for a code
        whose source is not `measured`, so the surface cannot grow a confident
        `ok` for a signal nothing produces. Closing the rest is the monitor
        round, and it is instrumentation, not alerting.
        """
        derived = {
            MalfunctionCode.IDENTITY_SERVICE_DOWN: self._identity_service_down(),
            MalfunctionCode.OUTBOX_DEPTH_GROWING: self._outbox_dropping(),
            MalfunctionCode.SESSION_ACTIONS_DEAD_LETTERED: self._dead_lettered(),
        }
        # Built by walking the enum, so a code added to the contract appears
        # here without anything being remembered, and appears as `unknown`
        # until somebody derives it.
        return LaneHealth(
            entries=tuple(
                HealthEntry(code=code.value, state=derived.get(code, HealthState.UNKNOWN).value)
                for code in MalfunctionCode
            )
        )

    def _identity_service_down(self) -> HealthState:
        """From the last decision's cause. `unknown` until a vehicle arrives.

        `unreachable` is the request that could not be completed at all, which
        at a lane means the engine is not running. A timeout, an error status
        and an unusable body are different repairs with different codes and are
        NOT folded in here -- the whole reason the cause is a closed set.
        """
        controller = self.controller
        if controller.last_decision is None:
            return HealthState.UNKNOWN
        if controller.last_cause == Unavailable.UNREACHABLE.value:
            return HealthState.ACTIVE
        if controller.last_cause is None:
            return HealthState.OK
        # A cause that is not `unreachable` says the engine answered something.
        # It is not evidence that the service is down, and it is not evidence
        # that it is up either.
        return HealthState.UNKNOWN

    def _outbox_dropping(self) -> HealthState:
        """`active` once the bounded log has discarded an event.

        What is measured is exactly the counter: log events dropped because the
        outbox reached its limit. Session actions are never dropped, so this
        never speaks for the money record.
        """
        return HealthState.ACTIVE if self.controller.events.dropped else HealthState.OK

    def _dead_lettered(self) -> HealthState:
        """`active` once the transport has dropped an item the platform refused.

        `unknown` when there is no transport, or when the wired one keeps no
        count. A lane with no platform has nothing to dead-letter, and saying
        `ok` there would be a confident negative about a thing that was never
        asked.
        """
        transport = self.controller.events.transport
        rejected = getattr(transport, "rejected", None)
        if transport is None or not isinstance(rejected, int):
            return HealthState.UNKNOWN
        return HealthState.ACTIVE if rejected else HealthState.OK

    # --- GET /v1/lane/events ----------------------------------------------

    def events(self, since: int) -> EventPage:
        queue = self.controller.events
        with self._lock:
            current = queue.cursor
            items = queue.since(since)
            dropped = queue.dropped
        return EventPage(
            cursor=current,
            # A cursor ahead of ours means this process restarted: the cursor
            # is not durable and the consumer's saved position now refers to
            # nothing. An empty list without this flag is indistinguishable
            # from "nothing happened".
            reset=since > current,
            dropped=dropped,
            events=tuple(
                {
                    "cursor": seq,
                    "event_id": event.event_id,
                    "kind": event.kind,
                    "lane_id": event.lane_id,
                    "occurred_at": to_iso(event.at),
                    "detail": event.detail,
                }
                for seq, event in items
            ),
        )


class _Handler(BaseHTTPRequestHandler):
    service: LaneService
    #: None means no credential is configured, which is the loopback default.
    #: A string means every route requires it.
    token: str | None = None

    server_version = "openparking-lane-controller"
    sys_version = ""

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    # --- the only method that does anything -------------------------------

    def do_GET(self) -> None:  # noqa: N802  (http.server's spelling)
        url = urlparse(self.path)
        if not self._authorised():
            return self._unauthorised()

        if url.path == "/v1/lane":
            return self._json(200, self.service.describe().to_dict())

        if url.path == "/v1/lane/state":
            return self._json(200, self.service.state().to_dict())

        if url.path == "/v1/lane/health":
            return self._json(200, self.service.health().to_dict())

        if url.path == "/v1/lane/events":
            raw = parse_qs(url.query).get("since", ["0"])[0]
            try:
                since = int(raw)
            except ValueError:
                return self._json(400, {"error": f"since must be an integer, got {raw!r}"})
            if since < 0 or since > MAX_QUERY_CURSOR:
                return self._json(400, {"error": f"since is out of range: {raw!r}"})
            return self._json(200, self.service.events(since).to_dict())

        return self._json(404, {"error": "no such route"})

    # --- every other method, through ONE refusal --------------------------

    def _method_not_allowed(self) -> None:
        """The single refusal every non-GET method is answered by.

        Spelled once and shared, so `tests/test_lane_contract.py` can sweep the
        handler and require that every `do_*` other than `do_GET` IS this
        function. A route that mutated something would have to stop being it,
        and the sweep goes red in the same commit.
        """
        self.send_response(405)
        self.send_header("Allow", "GET")
        body = json.dumps(
            {
                "error": "this contract version is read-only; there is no route that changes "
                "anything. See capabilities.can_vend on GET /v1/lane.",
                "contract_version": CONTRACT_VERSION,
            }
        ).encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = _method_not_allowed  # noqa: N815
    do_PUT = _method_not_allowed  # noqa: N815
    do_PATCH = _method_not_allowed  # noqa: N815
    do_DELETE = _method_not_allowed  # noqa: N815

    # --- plumbing ---------------------------------------------------------

    def _authorised(self) -> bool:
        """Compared in constant time, because a token is a secret.

        `hmac.compare_digest` and not `==`: the ordinary comparison returns as
        soon as two bytes differ, and that timing is enough to recover a token
        one character at a time from a machine on the same LAN -- which is
        exactly the machine this credential exists to keep out.
        """
        if self.token is None:
            return True
        presented = bearer(self.headers.get("Authorization"))
        return presented is not None and hmac.compare_digest(presented, self.token)

    def _unauthorised(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", "Bearer")
        body = json.dumps({"error": "a bearer token is required for this route"}).encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_server(
    service: LaneService,
    host: str = "127.0.0.1",
    port: int = 8090,
    token: str | None = None,
):
    """Bound to loopback by default. Exposing it is a deployment decision.

    Off loopback this REFUSES to build a server unless a token is configured.
    The refusal is here rather than in the CLI so it holds for every caller of
    this package, not only for the one that types the flag.
    """
    assert_bind_allowed(host, port, token)
    handler = type("_BoundHandler", (_Handler,), {"service": service, "token": token or None})
    return ThreadingHTTPServer((host, port), handler)


__all__ = [
    "ACT_ROUTES",
    "READ_ROUTES",
    "InsecureBind",
    "LaneService",
    "assert_bind_allowed",
    "bearer",
    "is_loopback",
    "make_server",
]
