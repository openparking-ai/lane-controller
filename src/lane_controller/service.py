"""The lane's local service. One HTTP contract, and everything is a client of it.

Until this existed, `lane-controller` was a library with no inbound surface:
`LaneController` was an object in a process and nothing outside that process
could ask what the lane had just decided. Our own intercom agent needs to ask,
and so does a third party's -- so it is a contract, not a private path, and our
software is an ordinary client of it.

**FOUR READS AND ONE ACT.** Version 2 grows the act surface, and it is one
route:

    GET  /v1/lane                 who this lane is, and what it can do
    GET  /v1/lane/state           the last decision, and the current transit
    GET  /v1/lane/health          every malfunction code, with its source
    GET  /v1/lane/events?since=N  the event cursor
    POST /v1/lane/vend            the assisted vend -- see `vend.py`

`ACT_ROUTES` below is that one route, and `Capabilities.can_vend` is derived
from it and from the lane's direction, so the capability cannot say `false`
while a route that opens a barrier exists, nor `true` at a lane that would
serve none. Every method other than `GET` and `POST` is answered by one shared
refusal, and `POST` to anything but an act route is answered by the same one.

**TWO TOKENS, AND A READ TOKEN NEVER AUTHORISES AN ACT.** The reads take
`--auth-token-file`; the vend takes `--act-token-file`, a SECOND file. Each is
refused on the other's routes with a 403 -- not a 401, because the caller is
known and is asking for something this credential does not buy. A credential in
a QUERY STRING is refused 401 on every route, whichever token it is: a token in
a URL is a token in an access log.

**Local, always.** It binds loopback by default and is meant to run on the same
device as the lane it describes. Off loopback it REFUSES to start without a
shared token -- `InsecureBind`, the same rule and the same shape the Vehicle ID
service applies, because the exposure is the same: on a lane's own LAN, this
publishes where a vehicle was, when, and what the lane decided about it. Off
loopback it refuses to start without the ACT token too, for a larger reason:
the exposed thing is no longer only a description of a lane, it is a barrier.

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

from vehicle_id.contract import SCHEMA_VERSION

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
from .vend import AssistedVend, BadVendRequest
from .vend import parse as parse_vend

log = logging.getLogger(__name__)

#: The routes this service answers, and every one of them is a GET.
READ_ROUTES: tuple[str, ...] = (
    "/v1/lane",
    "/v1/lane/state",
    "/v1/lane/health",
    "/v1/lane/events",
)

#: Routes that CHANGE something -- a vend, a resolve, anything that moves a
#: barrier or a session. ONE, and it is the whole act surface of this contract
#: version: the assisted vend.
#:
#: `Capabilities.can_vend` is derived from this tuple (and from the lane's
#: direction), so a route added here changes what the lane TELLS a consumer it
#: can do, in the same commit, which is the point. The refusals behind it are
#: in `vend.py`; a route added here without them would be a lane announcing an
#: act surface with nothing guarding it.
ACT_ROUTES: tuple[str, ...] = ("/v1/lane/vend",)

#: Query-string parameter names that would be carrying a credential.
#:
#: A token in a URL is a token in an access log, in a proxy's log, in a browser
#: history and in a referrer header -- none of which anybody decided. This lane
#: takes credentials in the `Authorization` header and NOWHERE else, so one that
#: arrives in a query string is answered 401 rather than ignored: ignoring it
#: would let a caller hold a working integration that leaks its own credential
#: on every request, with nothing anywhere saying so.
#:
#: The names are one half of the check. The other half is a VALUE equal to a
#: configured token under any parameter name at all -- see `_credential_in_query`.
CREDENTIAL_QUERY_KEYS: tuple[str, ...] = (
    "token",
    "access_token",
    "auth",
    "auth_token",
    "act_token",
    "bearer",
    "api_key",
    "apikey",
    "key",
)

MAX_QUERY_CURSOR = 2**63 - 1

#: The largest body `POST /v1/lane/vend` will read. A completion is four short
#: fields; anything larger is not one, and reading an unbounded body off a
#: socket into a gate housing's memory is a way to stop a lane serving from the
#: LAN it is on.
MAX_BODY_BYTES = 16 * 1024


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


def assert_bind_allowed(
    host: str, port: int, token: str | None, act_token: str | None = None
) -> None:
    """Raise unless this host may be bound with these credentials.

    One implementation, called by `make_server` -- so the rule holds for every
    caller of this package -- and by the CLI before it builds anything, so a
    misconfiguration is reported in the moment.

    TWO refusals, because there are now two exposures and they are not the same
    size. Without the read token, anything that can reach the port learns where
    a vehicle was and what this lane decided about it. Without the act token,
    anything that can reach the port can OPEN THE BARRIER -- this contract
    version has a route that does -- and refusing at the moment of the bind is
    the only place that can be said before it is true.
    """
    if is_loopback(host):
        return
    if not token:
        raise InsecureBind(
            f"refusing to bind {host or 'every interface'}:{port} with no token. Off loopback "
            "anything that can reach this port can read where a vehicle was, when it was there, "
            "and what this lane decided about it. Configure a shared token with "
            "--auth-token-file, or bind 127.0.0.1."
        )
    if ACT_ROUTES and not act_token:
        raise InsecureBind(
            f"refusing to bind {host or 'every interface'}:{port} with no ACT token. This "
            f"contract version serves {', '.join(ACT_ROUTES)}, which opens a barrier, and the "
            "read token does not authorise it. Configure a SECOND token with --act-token-file, "
            "or bind 127.0.0.1."
        )



def _same(presented: str, configured: str) -> bool:
    """Constant-time comparison of two credentials, ON BYTES.

    `hmac.compare_digest` on `str` RAISES `TypeError: comparing strings with
    non-ASCII characters is not supported`, and both of this handler's callers
    can be handed a non-ASCII string by an unauthenticated request:
    `parse_qs` percent-decodes a query value, and `http.server` decodes header
    bytes as latin-1. The query check is the FIRST thing `do_GET` and `do_POST`
    do -- before authorisation, before the route table -- so
    `GET /v1/lane?x=%C3%A9` with no credential at all closed the connection with
    an unhandled traceback and no response, on the one service in this project
    that can open a barrier. It existed only in the EXPOSED deployment, because
    `configured` is empty on a loopback lane with no tokens and `any()` over
    nothing never reaches the comparison.

    Bytes have no such rule. Encoding cannot fail -- `str` is always encodable
    as UTF-8 -- so this returns True or False and never raises, and a caller
    that presents rubbish gets the named 401 the contract publishes rather than
    a dropped connection.
    """
    return hmac.compare_digest(presented.encode("utf-8"), configured.encode("utf-8"))

def bearer(header: str | None) -> str | None:
    """The token out of an Authorization header, or None if there is not one."""
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


class _BoundedRead:
    """One call to somebody else's health route, with a flag for "it came back".

    `body` is `None` for every failure, exactly as `identity_health` already
    answers for one: the caller has nothing to say either way, and a read that
    raised and a read that could not be parsed are the same fact to it.
    """

    def __init__(self) -> None:
        self.done = threading.Event()
        self.body = None

    def run(self, reader) -> None:
        try:
            self.body = reader()
        except Exception as exc:  # noqa: BLE001
            # An identifier this package did not write may raise anything at
            # all. Letting it out of this thread would kill it silently and
            # leave `done` unset, which reads to the caller as a hang.
            log.warning("the identification service health read raised: %s", exc)
            self.body = None
        finally:
            self.done.set()


class LaneService:
    """A `LaneController`, read through the contract.

    It holds nothing of its own. Every answer below is computed from the
    controller at the moment it is asked, so there is no second copy of the
    lane's state to go stale, and nothing here can report something the lane
    does not currently hold.
    """

    def __init__(self, controller) -> None:
        self.controller = controller
        #: The act surface. It holds the refusals, the order they are applied
        #: in and the idempotency store; this service holds the HTTP.
        self.assisted = AssistedVend(self)
        self._lock = threading.Lock()
        #: The one identification-service health read that may be outstanding.
        #: See `_bounded_identity_health` -- it is what keeps a service that
        #: never answers from leaving one thread behind per poll.
        self._identity_read: _BoundedRead | None = None
        self._identity_read_lock = threading.Lock()
        #: Whether the unreadable-version refusal below has already been said.
        #: It is a property of the service on the other end, not of one request,
        #: and this route is polled -- so it is said once rather than on every
        #: poll for as long as that service stays on a version this build does
        #: not know.
        self._identity_version_refused = False

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
            # From the route table AND the direction, not from a constant. See
            # ACT_ROUTES, and `can_vend` below.
            can_vend=self.can_vend(),
        )

    def can_vend(self) -> bool:
        """Whether this lane serves a route that opens a barrier.

        Two facts, and both are derived: the service has an act route at all,
        and this lane is an ENTRY lane.

        The direction is in it because the act surface COMPLETES AN ENTRY. At an
        exit, completing an identity would close a stay and freeze a fee, which
        is the exit process and is a later round -- so an exit lane serves no
        such route and says `can_vend: false` rather than announcing a
        capability every call to which it would refuse. One answer, one place:
        the handler asks this before it serves, so the capability and the routes
        cannot disagree.
        """
        return bool(ACT_ROUTES) and self.controller.config.direction == "entry"

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
            # The read window's real depth, off the deque that holds it. A
            # consumer polls `GET /v1/lane/events` and this is what tells it
            # how far behind it may fall before it starts being told `reset`
            # instead of being served. Published rather than documented,
            # because a lane built with a different window would otherwise be
            # described by a number in a document that is right about a
            # different lane.
            event_window_depth=controller.events.history_depth,
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
                # WHETHER THIS CASE IS STILL OPEN. Without it the decision, its
                # outcome and its `at` are byte-identical before and after a
                # vend, so a second consumer could not see that the first one
                # had acted.
                completed=controller.last_decision_completed_at is not None,
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

        Some codes are DERIVED here and the rest answer `unknown`. That is not
        a placeholder: `HealthEntry` refuses any state but `unknown` for a code
        whose source is not `measured`, so the surface cannot grow a confident
        `ok` for a signal nothing produces.

        How many is not written down anywhere, here or in the document:
        `contract.SOURCES` is the one copy, and `tests/test_lane_contract.py`
        requires `derived_states()` to answer for exactly the codes it marks
        `measured`. A code promoted in one place and forgotten in the other
        fails there rather than becoming a sentence that is quietly wrong.
        """
        derived = self.derived_states()
        # Built by walking the enum, so a code added to the contract appears
        # here without anything being remembered, and appears as `unknown`
        # until somebody derives it.
        return LaneHealth(
            entries=tuple(
                HealthEntry(code=code.value, state=derived.get(code, HealthState.UNKNOWN).value)
                for code in MalfunctionCode
            )
        )

    def derived_states(self) -> dict[MalfunctionCode, HealthState]:
        """The codes this build derives, and what each of them reads right now.

        Split out of `health()` so the SET is inspectable rather than being an
        expression inside a payload builder. The half of the invariant that
        `HealthEntry` cannot enforce is this one: it refuses a code that claims
        `ok` without being `measured`, but nothing stops the reverse -- a code
        marked `measured` in `SOURCES` that nothing here derives would answer
        `unknown` for ever, and `unknown` from a code labelled `measured` reads
        as "asked, and could not tell" rather than as "never wired up".
        """
        return {
            MalfunctionCode.IDENTITY_SERVICE_DOWN: self._identity_service_down(),
            MalfunctionCode.IDENTITY_SERVICE_DEGRADED: self._identity_service_degraded(),
            MalfunctionCode.OUTBOX_DEPTH_GROWING: self._outbox_depth_growing(),
            MalfunctionCode.SESSION_ACTIONS_DEAD_LETTERED: self._dead_lettered(),
            MalfunctionCode.CLOCK_SKEW_REJECTED: self._clock_skew_rejected(),
            MalfunctionCode.ARMING_LOOP_STUCK_OCCUPIED: self._arming_loop_stuck(),
            MalfunctionCode.CLOSING_LOOPS_NEVER_FIRING: self._closing_loops_never_firing(),
        }

    def _arming_loop_stuck(self) -> HealthState:
        """An arming loop that has read occupied for longer than any dwell.

        MEASURED, and this is the whole of the measurement: this lane samples
        its own arming loop -- on every poll of `run_once`, arrival or not, and
        on every call to this method -- and holds the instant of the FIRST
        observation in the current unbroken run of occupied readings. `active`
        when that run is older than `[lane] arming_loop_max_occupied_s`.

        THE STATE IS NEVER `ok`, and that is not an oversight. `ok` at a lane
        means somebody measured and found nothing; what this observes is one
        way for a loop to be wrong, and a loop that is clear right now has not
        been found healthy -- it may have been stuck a minute ago, or be about
        to be. So the answers are `active` and `unknown`, and the third is not
        available to it.

        WHY IT MATTERS MORE THAN THE OTHER FOUR IN `VEND_BLOCKING`. A stuck loop
        defeats the vend's FIRST refusal rather than being caught by its second:
        `no_vehicle` asks the loop whether a car is there and a stuck loop says
        yes, and `geometry_incomplete` asks the second loop the same question.
        A lane with stuck arming loops would accept every assisted vend with
        nothing in front of it -- the metal-plate fraud arriving through the new
        route with the loops themselves as the accomplice. Adding the code to a
        refusing subset changes nothing until something measures it; this is
        that something.
        """
        dwell = self.controller.observe_arming_loop()
        if dwell is None:
            return HealthState.UNKNOWN
        if dwell > self.controller.config.arming_loop_max_occupied_s:
            return HealthState.ACTIVE
        return HealthState.UNKNOWN

    def _closing_loops_never_firing(self) -> HealthState:
        """A closing-loop driver that did not return inside the settle deadline.

        MEASURED, and from EXACTLY ONE EVENT: an assisted vend whose
        `resolve_transit` had not returned by the confirmation window plus
        `[lane] settle_grace_s`. The code's name suggests an aggregation nobody
        had built -- how many crossings a lane failed to see over some period,
        against a threshold nobody had chosen -- and this is not that. It is one
        named occurrence, and the contract says so rather than letting the name
        imply the other thing.

        LATCHED, and never `ok`. A driver that stops answering is a fault a
        person goes and fixes, so it does not clear itself the next time a
        transit happens to settle: it clears when the lane is restarted, by
        which point somebody has been there. And it is never `ok`, because
        nothing here observes the loops WORKING -- an ordinary arrival's
        crossing wait is not bounded by this deadline, so silence is not
        evidence of health.
        """
        return HealthState.ACTIVE if self.controller.loop_driver_timed_out else HealthState.UNKNOWN

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

    def _identity_service_degraded(self) -> HealthState:
        """The identification service's own `status`, read from its health route.

        A straight read of a field that already exists. The engine sets
        `degraded` when a read was LOST -- it could not even be written to the
        push queue, a full disk or a permissions change -- or when the queue held
        a line it could not read. Those are the two cases where a record was
        answered and then existed nowhere, which is why that service says it out
        loud, and until now nothing on this side listened.

        This CONTACTS the service, on the request. It is a check and not a
        memory: a cached answer from the last vehicle would report a service
        that was fine an hour ago as fine now, and at a lane that has had no
        arrivals since midnight the memory is the whole night old.

        Three answers and none of them guesses:

          * the service could not be read, did not answer within this lane's
            own bound, answered on a `schema_version` this build does not know,
            or answered no `status` this build recognises -- `unknown`. NOT
            `ok`: a service that cannot be asked has not been found healthy.
            Whether it is unreachable is `identity_service_down`, derived from a
            different signal.
          * `degraded` -- `active`.
          * `ok` -- `ok`, and that is a real measurement: it was asked and it
            answered.

        An identifier that is not a health-publishing service -- a stub, a lane
        identifying some other way -- has no method to call and answers
        `unknown`. Asked by capability rather than by type, so a third party's
        identifier that publishes the same route is read the same way ours is.
        """
        reader = getattr(self.controller.identifier, "identity_health", None)
        if not callable(reader):
            return HealthState.UNKNOWN
        body = self._bounded_identity_health(reader)
        if not isinstance(body, dict):
            return HealthState.UNKNOWN
        if not self._known_identity_schema(body.get("schema_version")):
            return HealthState.UNKNOWN
        status = body.get("status")
        if status == "degraded":
            return HealthState.ACTIVE
        if status == "ok":
            return HealthState.OK
        return HealthState.UNKNOWN

    def _known_identity_schema(self, version) -> bool:
        """The version refusal, COPIED from the reader of the same payload.

        `Read.from_dict` refuses an unrecognised `schema_version` rather than
        guessing which fields still mean what they used to, and both contracts
        state that policy in the same words: *an unrecognised version is
        refused, not partially read*. This is the same payload from the same
        service, and it was being read regardless -- with the value taken off it
        published as `measured`. One payload, one repository, two policies.

        A missing version is refused too. A payload that does not say which
        contract it is is not a payload this build can place, and reading it
        anyway is the half-read both contracts forbid.

        A `bool` is refused explicitly, for the reason `Read.from_dict` refuses
        it: `True == 1`, so `schema_version: true` would otherwise read as
        version 1.

        The answer for an unreadable one is the answer this method already has
        for every other unreadable case -- `unknown`, never `ok`.
        """
        if not isinstance(version, bool) and isinstance(version, int) and version == SCHEMA_VERSION:
            return True
        if not self._identity_version_refused:
            self._identity_version_refused = True
            log.warning(
                "the identification service's health declares schema_version %r; this build "
                "understands %d. Refusing to guess which fields still mean what they used to: "
                "identity_service_degraded is unknown.",
                version,
                SCHEMA_VERSION,
            )
        return False

    def _bounded_identity_health(self, reader):
        """The identification service's health, or `None`, within OUR OWN bound.

        **This route must not block on another machine for longer than its own
        bounded read.** The identification service runs in a different process,
        usually on a different box, and a HUNG one -- a socket that accepts and
        never answers -- used to hold this route open for that client's whole
        timeout. The monitor polling this lane gave up first and published
        `lane_unreachable`: a lane that is up, serving, and answering correctly,
        reported as a dead lane, with every real signal it was publishing
        retired at the same moment. A slow third machine became a fault
        attributed to this one.

        So the bound is the LANE'S, `[lane] identity_health_timeout_s`, and it is
        applied here rather than left to whatever the identifier's own client
        happens to use: an identifier this package did not write has a timeout
        this package did not choose, and the route's promise is the route's to
        keep.

        On timeout the answer is `None`, which the caller reads as `unknown` --
        nobody measured. A hung service is not the same fact as a refused one.

        **One read at a time.** The reader runs on a daemon thread so a service
        that never answers cannot hold this route or the interpreter's exit, and
        while one is still outstanding a second is not started: it would be one
        more thread per poll, for ever, against a machine that is not answering.
        A poll that arrives during an outstanding read is told `unknown`, which
        is exactly what it is.
        """
        with self._identity_read_lock:
            outstanding = self._identity_read
            if outstanding is not None and not outstanding.done.is_set():
                log.warning(
                    "the identification service has not answered the previous health read; "
                    "not starting a second"
                )
                return None
            read = _BoundedRead()
            self._identity_read = read

        threading.Thread(
            target=read.run, args=(reader,), name="identity-health", daemon=True
        ).start()
        if not read.done.wait(self.controller.config.identity_health_timeout_s):
            log.warning(
                "the identification service did not answer its health route within %.3fs; "
                "identity_service_degraded is unknown",
                self.controller.config.identity_health_timeout_s,
            )
            return None
        return read.body

    def _clock_skew_rejected(self) -> HealthState:
        """`active` once the platform has refused an item for clock skew.

        The most expensive code on the table, and it is a READ: the platform
        already answers a lane time it will not accept with a `409`, and this
        lane already counts that refusal -- undifferentiated, with six other
        conditions that produce the same status. What was missing was the name,
        which the platform now carries in the refusal body.

        A lane whose clock runs fast has its session opens and closes
        DEAD-LETTERED. The barrier still works, the driver still gets in, and
        the money record silently loses the stay. Nothing anywhere reported it.

        `unknown` in three situations, each of which is a different thing from
        `ok` and none of which is folded into it:

          * no transport -- a standalone lane has no platform to be refused by;
          * nothing has been ATTEMPTED yet, so nothing could have been refused.
            Without this a fresh lane that has never spoken to a platform would
            report a clock it has never had checked as fine;
          * a conflict arrived that the platform did not NAME. That is what a
            platform older than the field answers for every refusal, including
            a skew, so reading the absence as a negative would report a healthy
            clock on exactly the deployment where the failure is invisible.

        AND IT COMES BACK. Both counts are cleared by the next write the
        platform ACCEPTS, so this reads `ok` again once the clock is fixed and
        the lane is being taken. Without that it was a latch that read like a
        state: `active` for the life of the process, the operator's repair
        invisible, and `recovered` unable to fire at whatever is watching.
        """
        transport = self.controller.events.transport
        if transport is None:
            return HealthState.UNKNOWN
        counts = [
            getattr(transport, name, None)
            for name in ("attempted", "skew_rejected", "conflicts_unnamed")
        ]
        if not all(isinstance(count, int) for count in counts):
            # A wired transport that keeps no such counts has not measured this.
            return HealthState.UNKNOWN
        attempted, skew, unnamed = counts
        if skew:
            return HealthState.ACTIVE
        if not attempted or unnamed:
            return HealthState.UNKNOWN
        return HealthState.OK

    def _outbox_depth_growing(self) -> HealthState:
        """`active` while the outbox holds more than this site's threshold.

        The code is called `outbox_depth_growing`, so what it reads is the
        DEPTH: `EventQueue.pending`, which is every undelivered item -- log
        events and session actions both. It used to read `dropped`, which is
        the count of things ALREADY LOST, so a lane with nine thousand
        undelivered events behind a dead platform answered `ok` right up to the
        moment the log hit its bound and began throwing entries away. That is
        the reassuring direction, on the one code an operator would use to find
        out that a lane has stopped reporting.

        `ok` is a real measurement here: the depth was read and it is under the
        threshold. The threshold is a per-site SETTING and an assumption -- see
        `LaneConfig.outbox_depth_threshold` -- not a measured property of
        anything, which is why it is configurable and published rather than
        being a constant in this file.
        """
        pending = self.controller.events.pending
        threshold = self.controller.config.outbox_depth_threshold
        return HealthState.ACTIVE if pending > threshold else HealthState.OK

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
            oldest = queue.oldest
        return EventPage(
            cursor=current,
            # TWO ways a saved position stops referring to anything, and both
            # are `reset` because to a consumer they are the same fact: what
            # you asked for is gone and you did not get it.
            #
            #   * AHEAD of ours -- this process restarted. The cursor is not
            #     durable and the position now names nothing.
            #   * BEHIND the oldest event still held -- the window has evicted
            #     what you asked for. `since` returns what survived, which is
            #     a SHORT answer that looks exactly like a complete one.
            #
            # The second is the one the Vehicle ID service does not need: its
            # contract tells a consumer that needs guaranteed delivery to use
            # push, and it has push. This surface has neither, so the eviction
            # is reported here or it is not reported at all. `dropped` does not
            # cover it -- that counts what the OUTBOX discarded, which is a
            # different bound on a different queue.
            reset=since > current or (oldest is not None and since + 1 < oldest),
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
    #: A string means every READ route requires it.
    token: str | None = None
    #: The SECOND credential, and the only one that authorises the vend. None
    #: is the loopback default; off loopback the bind is refused without it.
    act_token: str | None = None

    server_version = "openparking-lane-controller"
    sys_version = ""

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    # --- the reads --------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802  (http.server's spelling)
        url = urlparse(self.path)
        if self._credential_in_query(url.query):
            return self._credential_in_a_url()
        refusal = self._authorise(
            self.token, self.act_token, "a read route", unconfigured_refuses=False
        )
        if refusal is not None:
            return refusal()

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

    # --- the one act ------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        """The act surface, and it is exactly `ACT_ROUTES`.

        A POST to anything else -- including a read route -- is the same shared
        refusal every other method gets. The route table is what decides, not a
        list somebody remembered here.
        """
        url = urlparse(self.path)
        if self._credential_in_query(url.query):
            return self._credential_in_a_url()
        if url.path not in ACT_ROUTES:
            return self._method_not_allowed()
        # An act route this LANE does not serve is not there, and says so. See
        # `LaneService.can_vend`: an exit lane publishes `can_vend: false` and
        # this is the same fact answered as a status.
        if not self.service.can_vend():
            return self._json(404, {"error": "no such route on this lane"})

        refusal = self._authorise(
            self.act_token, self.token, "the vend route", unconfigured_refuses=True
        )
        if refusal is not None:
            return refusal()

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": "Content-Length is not an integer"})
        if length < 0 or length > MAX_BODY_BYTES:
            return self._json(400, {"error": f"the body must be at most {MAX_BODY_BYTES} bytes"})
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._json(400, {"error": "the body is not JSON"})

        try:
            request = parse_vend(body, self.headers.get("Idempotency-Key"))
        except BadVendRequest as exc:
            return self._json(400, {"error": str(exc)})

        status, payload = self.service.assisted.complete(request)
        return self._json(status, payload)

    # --- every other method, through ONE refusal --------------------------

    def _method_not_allowed(self) -> None:
        """The single refusal every method that is not a route is answered by.

        Spelled once and shared, so `tests/test_lane_contract.py` can sweep the
        handler and require that every `do_*` other than `do_GET` and `do_POST`
        IS this function -- and that `do_POST` serves exactly `ACT_ROUTES` and
        answers this everywhere else. A second route that mutated something
        would have to break one of those two, and the sweep goes red in the
        same commit.
        """
        self.send_response(405)
        self.send_header("Allow", "GET, POST" if ACT_ROUTES else "GET")
        body = json.dumps(
            {
                "error": (
                    "this route does not change anything. The whole act surface of this "
                    f"contract version is {', '.join(ACT_ROUTES) or 'empty'}. See "
                    "capabilities.can_vend on GET /v1/lane."
                ),
                "contract_version": CONTRACT_VERSION,
            }
        ).encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_PUT = _method_not_allowed  # noqa: N815
    do_PATCH = _method_not_allowed  # noqa: N815
    do_DELETE = _method_not_allowed  # noqa: N815

    # --- plumbing ---------------------------------------------------------

    def _authorise(
        self, required: str | None, other: str | None, what: str, *, unconfigured_refuses: bool
    ):
        """`None` when the caller may proceed, or the refusal to send.

        Compared in constant time, because a token is a secret.
        `hmac.compare_digest` and not `==`: the ordinary comparison returns as
        soon as two bytes differ, and that timing is enough to recover a token
        one character at a time from a machine on the same LAN -- which is
        exactly the machine this credential exists to keep out.

        THE OTHER TOKEN IS A 403, NOT A 401. A 401 means "I do not know who you
        are"; this caller is known, holds a real credential of this lane's, and
        is asking for something that credential does not buy. Answering 401
        would invite it to retry with the same token for ever, and would make
        the read token and a wrong guess look identical -- which is the whole
        point of there being two.
        """
        if required is None:
            if other is None:
                # NEITHER credential is configured: the loopback default, and
                # it is the default for the vend too. A lane on loopback with
                # no files at all is the developer's lane and the demo's, and
                # this is the same answer it has always given.
                return None
            if not unconfigured_refuses:
                # No credential configured for this route: the loopback
                # default, which is what the reads have always had. It stays
                # open here even when the OTHER token is configured, because
                # refusing a monitor with a 403 for a credential it was never
                # asked for is a confusing failure and not a safer one -- and
                # off loopback the bind already requires both.
                return None
            # THE ASYMMETRY, and it is deliberate. It holds ONCE A READ TOKEN
            # EXISTS: a lane that has been given one credential and not the
            # other has authorised nothing to vend, and serving the barrier to
            # anything that can reach loopback because the act file was never
            # written is a different size of mistake from an open read.
            #
            # It does NOT say that a lane with no act token never vends. A lane
            # with NEITHER credential configured is the loopback default and the
            # branch above returns `None` for it -- "Loopback takes neither,
            # exactly as the reads do", which is what `docs/CONTRACT.md` says
            # and what this comment used to overstate.
            return lambda: self._forbidden(f"this lane has no credential configured for {what}")
        presented = bearer(self.headers.get("Authorization"))
        if presented is None:
            return self._unauthorised
        if _same(presented, required):
            return None
        if other is not None and _same(presented, other):
            return lambda: self._forbidden(f"that credential does not authorise {what}")
        return self._unauthorised

    def _credential_in_query(self, query: str) -> bool:
        """Whether this request put a credential in the URL. Either token, any name.

        Two halves, and both are needed. The NAMES catch a caller that has
        invented its own way of passing a token; the VALUE comparison catches
        one that used a name nobody thought of, and it is the half that cannot
        be got round.
        """
        if not query:
            return False
        params = parse_qs(query, keep_blank_values=True)
        if any(name.lower() in CREDENTIAL_QUERY_KEYS for name in params):
            return True
        configured = [token for token in (self.token, self.act_token) if token]
        return any(
            _same(value, token)
            for values in params.values()
            for value in values
            for token in configured
        )

    def _credential_in_a_url(self) -> None:
        """401, and the request is not served whatever else was right about it.

        A URL is logged by everything it passes through. Serving this request
        would hand the caller a working integration that publishes its own
        credential on every call, with nothing anywhere saying so.
        """
        log.warning("a credential was presented in a query string; refusing the request")
        self._json(
            401,
            {
                "error": (
                    "a credential in a query string is refused. A token in a URL is a token "
                    "in an access log: present it in the Authorization header."
                )
            },
        )

    def _forbidden(self, why: str) -> None:
        self.send_response(403)
        body = json.dumps({"error": why}).encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
    act_token: str | None = None,
):
    """Bound to loopback by default. Exposing it is a deployment decision.

    Off loopback this REFUSES to build a server unless BOTH tokens are
    configured. The refusal is here rather than in the CLI so it holds for every
    caller of this package, not only for the one that types the flags.
    """
    assert_bind_allowed(host, port, token, act_token)
    handler = type(
        "_BoundHandler",
        (_Handler,),
        {"service": service, "token": token or None, "act_token": act_token or None},
    )
    return ThreadingHTTPServer((host, port), handler)


__all__ = [
    "ACT_ROUTES",
    "CREDENTIAL_QUERY_KEYS",
    "MAX_BODY_BYTES",
    "READ_ROUTES",
    "InsecureBind",
    "LaneService",
    "assert_bind_allowed",
    "bearer",
    "is_loopback",
    "make_server",
]
