import pytest

from lane_controller import (
    CameraConfig,
    DecisionCache,
    GateConfig,
    LaneConfig,
    LaneController,
    Rule,
    VehicleIdentity,
)
from lane_controller.simulated import (
    CannedCameraFeed,
    RecordingVendOutput,
    SimulatedLoopInput,
    StubVehicleIdentifier,
)


@pytest.fixture
def config():
    return LaneConfig(
        lane_id="lane-test",
        site_id="site-test",
        camera=CameraConfig(camera_id="sim-cam-1", rtsp_url="", frames_per_read=3),
        gate=GateConfig(),
        confidence_threshold=0.85,
    )


@pytest.fixture
def cache():
    cache = DecisionCache()
    cache.load(
        [
            Rule(plate="SIM-0001", allow=True, rate_plan="monthly"),
            Rule(plate="BANNED-1", allow=False),
        ]
    )
    return cache


def build_lane(config, cache, identities, arrivals=1):
    """A whole lane, wired to simulated hardware. No Jetson, no camera, no barrier."""
    vend = RecordingVendOutput()
    controller = LaneController(
        config,
        loop=SimulatedLoopInput(arrivals=arrivals),
        camera=CannedCameraFeed(),
        vend=vend,
        identifier=StubVehicleIdentifier(identities),
        cache=cache,
    )
    return controller, vend


@pytest.fixture
def lane(config, cache):
    def _build(identities: list[VehicleIdentity], arrivals: int = 1):
        return build_lane(config, cache, identities, arrivals)

    return _build


# ---------------------------------------------------------------------------
# Deliberate breakage, for the offline fail-control.
#
# scripts/offline_fail_control.py sets BREAK_OFFLINE_QUEUE and requires the
# offline suite to FAIL. An offline guarantee that has never been observed
# failing is not known to be a guarantee.
# ---------------------------------------------------------------------------
import os  # noqa: E402

import pytest as _pytest  # noqa: E402


@_pytest.fixture(autouse=True)
def _break_the_queue(monkeypatch):
    mode = os.environ.get("BREAK_OFFLINE_QUEUE")
    if not mode:
        return

    from lane_controller.events import EventQueue
    from lane_controller.sync import PlatformTransport

    if mode == "drop":
        # The queue assumes delivery succeeded and clears regardless. This is
        # what "offline-tolerant" looks like when it is only claimed.
        def lossy_flush(self):
            # Must clear the REAL storage. _queue is a computed view now, so
            # clearing it would clear a temporary list and drop nothing -- the
            # breakage would then be simulating nothing and the control would
            # pass for the wrong reason.
            batch = self._queue
            if self._transport is not None:
                try:
                    self._transport.send(batch)
                except Exception:
                    pass
            self._log.clear()
            self._sessions.clear()
            return len(batch)

        monkeypatch.setattr(EventQueue, "flush", lossy_flush)

    elif mode == "noid":
        # Delivery happens twice -- an acknowledgement lost on the way back,
        # which is ordinary -- and the event id is regenerated each time, so
        # the platform cannot tell the second attempt from new activity.
        import uuid

        original = PlatformTransport.send

        def duplicating_send(self, events):
            def fresh(batch):
                return [
                    type(e)(
                        kind=e.kind,
                        lane_id=e.lane_id,
                        at=e.at,
                        detail=e.detail,
                        event_id=str(uuid.uuid4()),
                    )
                    for e in batch
                ]

            if not original(self, fresh(events)):
                return False
            return original(self, fresh(events))

        monkeypatch.setattr(PlatformTransport, "send", duplicating_send)

    else:
        raise RuntimeError(f"unknown BREAK_OFFLINE_QUEUE mode: {mode}")


# ---------------------------------------------------------------------------
# Deliberate breakage, for the confirmation fail-control.
#
# scripts/confirmation_fail_control.py sets BREAK_CONFIRMATION and requires the
# loop suite to FAIL. Each mode breaks exactly one decision point -- not a
# fixture, not a stub, the code that decides -- so a control that passes is
# telling us the suite measures that decision and not something beside it.
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_confirmation(monkeypatch):
    mode = os.environ.get("BREAK_CONFIRMATION")
    if not mode:
        return

    from lane_controller.controller import LaneController
    from lane_controller.interfaces import ClosingSequence

    if mode == "window":
        # The configured window is ignored, so a crossing at any speed
        # confirms. "Something happened here eventually" is not a vehicle
        # going through the gate.
        monkeypatch.setattr(LaneController, "_confirmation_window", lambda self: float("inf"))

    elif mode == "elapsed":
        # The window is still configured, still published on the event, and no
        # longer COMPARED against: whatever the loops report FORWARD for is
        # accepted however long it took. This is the break the `window` mode
        # cannot make -- that one changes the configured value, which the
        # fixture then honours, so it proves the config reaches the loops and
        # nothing about whether the controller applies it.
        monkeypatch.setattr(
            LaneController, "_within_window", staticmethod(lambda elapsed, window: True)
        )

    elif mode == "direction":
        # B-then-A confirms too, so a vehicle backing out of the gate opens a
        # session. One loop could not tell those apart; this is what having two
        # and not reading the order looks like.
        monkeypatch.setattr(
            LaneController,
            "_confirms",
            staticmethod(lambda crossing: crossing is not ClosingSequence.NONE),
        )

    elif mode == "promote":
        # The window elapsing with nothing at all confirms. This is the phantom
        # occupant: a ticket no car followed, promoted to a billable session.
        monkeypatch.setattr(
            LaneController,
            "_confirms",
            staticmethod(lambda crossing: crossing is not ClosingSequence.REVERSE),
        )

    elif mode == "arming":
        # One arming loop is enough. A person with a piece of metal on a single
        # loop arms the lane again.
        # `arming_complete`, and the name matters: it was `_arming_complete`
        # until the assisted vend needed the same check, and a break anchored
        # on the old name raises AttributeError instead of applying -- which
        # reports "fails as required" for the wrong reason. `raising=True` is
        # the default and is what would have said so; this is the assertion of
        # the same thing, one line up, where a reader will see it.
        assert hasattr(LaneController, "arming_complete"), "the arming break's anchor has moved"
        monkeypatch.setattr(LaneController, "arming_complete", staticmethod(lambda loop_b: True))

    else:
        raise RuntimeError(f"unknown BREAK_CONFIRMATION mode: {mode}")


# ---------------------------------------------------------------------------
# Deliberate breakage, for the fallback-cause fail-control.
#
# scripts/fallback_cause_fail_control.py sets BREAK_FALLBACK_CAUSE and requires
# the engine-unreachable suite to FAIL. Each mode breaks exactly one point that
# carries the guarantee -- the ordering in `decide`, the classification in the
# client, or the cause reaching the record -- so a control that passes says the
# suite measures that point and not something beside it.
#
# The last mode is the one facing the other way: a change that reported EVERY
# fallback as an unreachable engine would satisfy every assertion about a dead
# engine, and is the same defect with the arrow reversed.
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_fallback_cause(monkeypatch):
    mode = os.environ.get("BREAK_FALLBACK_CAUSE")
    if not mode:
        return

    from dataclasses import replace

    from lane_controller import decision as decision_module
    from lane_controller import vehicle_id_client as client_module
    from lane_controller.decision import Fallback
    from lane_controller.events import EventQueue
    from lane_controller.interfaces import VehicleIdentity

    original_decide = decision_module.decide

    def patch_decide(replacement):
        # `decide` is bound by name in every module that imported it -- the
        # module that defines it, `controller`, the package's `__init__`, and
        # each test module -- and rebinding one of them leaves the others
        # holding the original function object.
        #
        # Found the hard way on this control's first run: patching `decision`
        # and `controller` still turned the suite red, on ONE test, through the
        # lane rather than through the break this describes. A check that
        # passes for the wrong reason has failed. So the bindings are DERIVED
        # -- every one that is the original object is replaced -- rather than
        # listed, because a list cannot notice the next importer.
        import sys

        for module in list(sys.modules.values()):
            if getattr(module, "decide", None) is original_decide:
                monkeypatch.setattr(module, "decide", replacement)

    if mode == "merge":
        # The ordering in `decide` is gone: `unavailable` is set on the
        # identity and nothing looks at it, so the 0.0 left behind by a failed
        # request is compared against a threshold again. This is the defect
        # exactly as it stood.
        def blind_decide(identity, cache, **kwargs):
            return original_decide(replace(identity, unavailable=None), cache, **kwargs)

        patch_decide(blind_decide)

    elif mode == "blind":
        # A different point, and one `merge` cannot reach: the CLIENT stops
        # saying which failure it had, so there is nothing for the ordering to
        # find. Two lines carry this guarantee and each is broken on its own.
        original_identify = client_module.VehicleIdClient.identify

        def forgetful_identify(self, frames):
            return replace(original_identify(self, frames), unavailable=None)

        monkeypatch.setattr(client_module.VehicleIdClient, "identify", forgetful_identify)

    elif mode == "swallow":
        # The arrow reversed: every fallback becomes an unreachable engine, so
        # a plate the engine looked at and would not vouch for is reported as a
        # service being down. Only the control catches this one.
        def greedy_decide(identity, cache, **kwargs):
            outcome = original_decide(identity, cache, **kwargs)
            if outcome.fallback is not None:
                return replace(outcome, fallback=Fallback.ENGINE_UNREACHABLE)
            return outcome

        patch_decide(greedy_decide)

    elif mode == "onecause":
        # One code for what the lane did is the design; one CAUSE for every
        # failure is not. A camera that handed over nothing, a service that is
        # off and a service that is slow are three repairs, and this collapses
        # them into a record nobody can act on.
        monkeypatch.setattr(client_module, "_cause", lambda exc: client_module.CAUSE_UNREACHABLE)

    elif mode == "above_presence":
        # The other ordering the round created, and the one the five breaks
        # above cannot reach: `unavailable` looked at BEFORE presence. Nothing
        # was there, the engine is also down, and the lane issues a ticket for
        # a car that does not exist. Expressed by dropping presence whenever a
        # cause is set, which is what moving the check above it does.
        def presence_last_decide(identity, cache, **kwargs):
            if identity.unavailable is not None:
                return original_decide(replace(identity, presence=None), cache, **kwargs)
            return original_decide(identity, cache, **kwargs)

        patch_decide(presence_last_decide)

    elif mode == "freetext":
        # The seam stops constraining `unavailable`, so an identifier's own
        # string travels into `Decision.reason` and into `events.detail`, which
        # the retention purge cannot reach. This is the state the branch was in
        # before the closed set, and the plate-leak cases that plant a member
        # of that set cannot see it.
        monkeypatch.setattr(VehicleIdentity, "__post_init__", lambda self: None)

    elif mode == "nodetail":
        # The cause is measured and never leaves the process. Whoever answers
        # the intercom sees a code and no reason for it.
        original_record = EventQueue.record

        def silent_record(self, kind, lane_id, **detail):
            if kind == "fallback_needs_human":
                detail.pop("cause", None)
            return original_record(self, kind, lane_id, **detail)

        monkeypatch.setattr(EventQueue, "record", silent_record)

    else:
        raise RuntimeError(f"unknown BREAK_FALLBACK_CAUSE mode: {mode}")


# ---------------------------------------------------------------------------
# Deliberate breakage, for the lane-contract fail-control.
#
# scripts/contract_fail_control.py sets BREAK_LANE_CONTRACT and requires the
# contract suite to FAIL. Each mode breaks exactly one property the contract
# exists to have -- the geometry being the lane's own, the health table being
# complete, `unknown` not being `ok`, the read-only sweep, the derived
# fallback, the cursor's reset flag -- so a control that passes says the suite
# measures that property and not something beside it.
#
# The stub's own breaks live in `tests/third_party_lane/lane.py` under
# BREAK_THIRD_PARTY_LANE, because a fixture that cannot be broken is a fixture
# that measures nothing.
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_lane_contract(monkeypatch):
    mode = os.environ.get("BREAK_LANE_CONTRACT")
    if not mode:
        return

    import test_lane_contract as test_module
    from lane_controller import contract as contract_module
    from lane_controller import service as service_module
    from lane_controller.config import LoopConfig
    from lane_controller.contract import (
        Capabilities,
        HealthEntry,
        HealthState,
        LaneDescription,
        LaneHealth,
        MalfunctionCode,
    )

    if mode == "geometry_copy":
        # The service renders its own geometry instead of publishing the
        # lane's. Identical for a default lane and wrong for every other one,
        # which is exactly how a second copy fails: not at once.
        def copied(self):
            return LaneDescription(
                lane_id=self.controller.config.lane_id,
                site_id=self.controller.config.site_id,
                direction=self.controller.config.direction,
                geometry=LoopConfig().as_published(),
                capabilities=self.capabilities(),
            )

        monkeypatch.setattr(service_module.LaneService, "describe", copied)

    elif mode == "drop_code":
        # One code left out of the payload. A consumer cannot tell an absent
        # code from a healthy one, which is the whole reason the set is closed.
        def short(self):
            return LaneHealth(
                entries=tuple(
                    HealthEntry(code=code.value, state=HealthState.UNKNOWN.value)
                    for code in list(MalfunctionCode)[:-1]
                )
            )

        monkeypatch.setattr(service_module.LaneService, "health", short)

    elif mode == "unknown_is_ok":
        # The invariant removed at the seam that enforces it AND at the seam
        # that produces it: a code nothing measures reports a clean bill of
        # health. This is "wrong silently" in one line.
        def unchecked(self) -> None:
            pass

        monkeypatch.setattr(HealthEntry, "__post_init__", unchecked)

        def cheerful(self):
            return LaneHealth(
                entries=tuple(
                    HealthEntry(code=code.value, state=HealthState.OK.value)
                    for code in MalfunctionCode
                )
            )

        monkeypatch.setattr(service_module.LaneService, "health", cheerful)

    elif mode == "plant_post":
        # A `do_POST` THAT IS NOT THE ROUTE TABLE: it answers every path,
        # including the four reads. The act surface is `ACT_ROUTES` and nothing
        # else, and this is the positive control for that -- a second route
        # that changed something would have to get past this sweep.
        def do_POST(self):  # noqa: N802, N807
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        monkeypatch.setattr(service_module._Handler, "do_POST", do_POST)

    elif mode == "vend_capability":
        # The capability WITHOUT the route: `can_vend: true` on a lane that
        # serves none -- an EXIT lane, where completing an identity would close
        # a stay and freeze a fee. The mirror of the mode above, broken
        # separately because one derivation joins them.
        original = service_module.LaneService.capabilities

        def boastful(self):
            return Capabilities(**{**original(self).to_dict(), "can_vend": True})

        monkeypatch.setattr(service_module.LaneService, "capabilities", boastful)

    elif mode == "stored_fallback":
        # `fallback` stops being derived from `reason` and echoes whatever it
        # is given. A third-party lane's own vocabulary then arrives looking
        # like one of our codes, and a consumer maps a foreign reason onto a
        # branch it has -- the guess the contract exists to prevent.
        monkeypatch.setattr(
            contract_module.LastDecision,
            "fallback",
            property(lambda self: self.reason),
        )

    elif mode == "no_reset":
        # The cursor stops saying it restarted. An empty list then means both
        # "nothing happened" and "you have missed everything".
        original_events = service_module.LaneService.events

        def blind(self, since):
            page = original_events(self, since)
            return contract_module.EventPage(
                cursor=page.cursor, reset=False, dropped=page.dropped, events=page.events
            )

        monkeypatch.setattr(service_module.LaneService, "events", blind)

    elif mode == "extra_field":
        # The code grows a field the document does not show. The doc/contract
        # agreement test is the only thing that can see this, and it is the
        # reason that test exists.
        original_to_dict = contract_module.EventPage.to_dict

        def wider(self):
            return {**original_to_dict(self), "undocumented": True}

        monkeypatch.setattr(contract_module.EventPage, "to_dict", wider)

    elif mode == "session_actions_on_the_wire":
        # THE DEFECT THIS ROUND DELETES. `record` puts every event in the read
        # history, session actions included -- so `GET /v1/lane/events`
        # publishes `session_open {plate: ...}` to every consumer of a READ
        # contract, in a `detail` the contract declares OPAQUE and the
        # retention purge cannot reach.
        from lane_controller.events import EventQueue

        original_record = EventQueue.record

        def indiscriminate(self, kind, lane_id, **detail):
            event = original_record(self, kind, lane_id, **detail)
            from lane_controller.events import SESSION_KINDS

            if kind in SESSION_KINDS:
                self._cursor += 1
                self._history.append((self._cursor, event))
            return event

        monkeypatch.setattr(EventQueue, "record", indiscriminate)

    elif mode == "plate_in_a_log_event":
        # The same exposure by a different door, and the one the mode above
        # cannot prove: an ORDINARY LOG EVENT carrying plate text. The route
        # sweep must find it whether it arrives on a session action or not --
        # otherwise the sweep is a test of `SESSION_KINDS`, not of the surface.
        from lane_controller.controller import LaneController

        original_session = LaneController._record_session

        def photographs_the_plate(self, identity, at, *, confirmation):
            self.events.record("entry_photo_taken", self.config.lane_id, plate=identity.plate)
            return original_session(self, identity, at, confirmation=confirmation)

        monkeypatch.setattr(LaneController, "_record_session", photographs_the_plate)

    elif mode == "evicted_reset":
        # The eviction comparison removed, leaving `since > current` -- exactly
        # the Vehicle ID semantics, which are honest THERE because that
        # contract has push and this one does not. A consumer 50 events behind
        # a 256-deep window is served 256 of them and told `reset: false`.
        original_events = service_module.LaneService.events

        def only_ahead(self, since):
            page = original_events(self, since)
            with self._lock:
                current = self.controller.events.cursor
            return contract_module.EventPage(
                cursor=page.cursor,
                reset=since > current,
                dropped=page.dropped,
                events=page.events,
            )

        monkeypatch.setattr(service_module.LaneService, "events", only_ahead)

    elif mode == "depth_blind":
        # `outbox_depth_growing` reads `dropped` again: the count of events
        # ALREADY LOST rather than the depth its own name promises. Nine
        # thousand undelivered events read `ok`, with `source: measured`, so
        # the HealthEntry guard cannot see it -- the entry genuinely IS
        # measured, of something else.
        def dropping(self):
            return (
                contract_module.HealthState.ACTIVE
                if self.controller.events.dropped
                else contract_module.HealthState.OK
            )

        monkeypatch.setattr(service_module.LaneService, "_outbox_depth_growing", dropping)

    elif mode == "doc_values":
        # The document publishes the OPPOSITE of what the code enforces, in the
        # three places the L3 found it could: a lane that cannot vend saying it
        # can, a version nobody would recognise, and a never-alarm code
        # rewritten into a measured, healthy, page-a-technician one.
        #
        # Applied to the PARSED examples rather than to the file, so it runs in
        # CI without editing a tracked document. The equivalent edits to
        # docs/CONTRACT.md itself turn the same tests red -- the receipt runs
        # them that way.
        from lane_controller.contract import MalfunctionCode as _Code

        original_payloads = test_module.doc_payloads

        def doctored():
            doc = original_payloads()
            doc["lane"]["capabilities"]["can_vend"] = True
            doc["state"]["contract_version"] = 99
            for row in doc["health"]["codes"]:
                if row["code"] == _Code.REFERENCE_NOT_RECOGNISED.value:
                    row["source"] = "measured"
                    row["never_alarm"] = False
                    row["caveat"] = "PAGE A TECHNICIAN. The reference view is not recognised."
            return doc

        monkeypatch.setattr(test_module, "doc_payloads", doctored)

    else:
        raise RuntimeError(f"unknown BREAK_LANE_CONTRACT mode: {mode}")


# ---------------------------------------------------------------------------
# Deliberate breakage, for the measured-codes fail-control.
#
# scripts/measured_codes_fail_control.py sets BREAK_MEASURED_CODE and requires
# the contract suite to FAIL. Each mode removes exactly one thing that makes a
# code a MEASUREMENT rather than a label -- not a fixture and not a stub -- so a
# control that passes says the suite measures the derivation and not something
# beside it.
#
# The reassuring direction is the one that matters here. Every break below turns
# an honest `unknown` or a real `active` into a confident `ok`, which is a lane
# reporting a clock nobody checked and an identification service nobody asked.
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_measured_codes(monkeypatch):
    mode = os.environ.get("BREAK_MEASURED_CODE")
    if not mode:
        return

    from lane_controller.contract import SOURCES, HealthState, MalfunctionCode, Source
    from lane_controller.platform_client import PlatformRejected
    from lane_controller.service import LaneService
    from lane_controller.sync import PlatformTransport

    if mode == "degraded_is_ok":
        # A service that cannot be READ reports a clean bill of health. This is
        # the shape the whole surface exists to refuse: `unknown` folded into
        # `ok` at the one seam an operator trusts.
        original = LaneService._identity_service_degraded

        def optimistic(self):
            state = original(self)
            return HealthState.OK if state is HealthState.UNKNOWN else state

        monkeypatch.setattr(LaneService, "_identity_service_degraded", optimistic)

    elif mode == "degraded_never_fires":
        # It asks, and it never reports what it was told. A label with a
        # derivation behind it that cannot answer `active` is not a measurement
        # of anything -- and it reads exactly like a healthy service.
        monkeypatch.setattr(
            LaneService, "_identity_service_degraded", lambda self: HealthState.OK
        )

    elif mode == "degraded_is_remembered":
        # The service is asked ONCE and the answer is kept. At a lane with no
        # arrivals since midnight the memory is the whole night old, and a
        # service that went degraded an hour ago still reads `ok`.
        seen = {}

        def cached(self):
            reader = getattr(self.controller.identifier, "identity_health", None)
            if not callable(reader):
                return HealthState.UNKNOWN
            if "body" not in seen:
                seen["body"] = reader()
            body = seen["body"]
            if not isinstance(body, dict):
                return HealthState.UNKNOWN
            return HealthState.ACTIVE if body.get("status") == "degraded" else HealthState.OK

        monkeypatch.setattr(LaneService, "_identity_service_degraded", cached)

    elif mode == "skew_from_the_message":
        # The refusal is classified by matching text in the message instead of
        # reading the field. A platform that rewords its error goes silent, and
        # a platform that mentions the words in another refusal goes loud.
        def by_message(self, err: PlatformRejected) -> None:
            if "clock" in err.body.lower():
                self.skew_rejected += 1

        monkeypatch.setattr(PlatformTransport, "_classify", by_message)

    elif mode == "unnamed_is_not_a_skew":
        # An unnamed conflict is treated as "not a skew". A platform that
        # predates the field refuses a skew exactly as it refuses everything
        # else, so this reports a healthy clock on the one deployment where the
        # failure is invisible.
        def named_only(self, err: PlatformRejected) -> None:
            if err.status == 409 and err.code == "clock_skew":
                self.skew_rejected += 1

        monkeypatch.setattr(PlatformTransport, "_classify", named_only)

    elif mode == "silence_is_ok":
        # A lane that has attempted nothing reports its clock as fine. Nothing
        # was sent, so nothing could have been refused: this is a confident
        # negative about a question nobody asked.
        original_skew = LaneService._clock_skew_rejected

        def optimistic_skew(self):
            state = original_skew(self)
            if state is HealthState.UNKNOWN and self.controller.events.transport is not None:
                return HealthState.OK
            return state

        monkeypatch.setattr(LaneService, "_clock_skew_rejected", optimistic_skew)

    elif mode == "labelled_not_derived":
        # A code marked `measured` in SOURCES with nothing deriving it. It then
        # answers `unknown` for ever, which reads as "asked, and could not tell"
        # rather than as "never wired up" -- the failure `HealthEntry` cannot
        # see, because it only refuses the other direction.
        monkeypatch.setitem(SOURCES, MalfunctionCode.DISK_NEARLY_FULL, Source.MEASURED)

    elif mode == "doc_stale_list":
        # The document still lists a code this build now READS among the ones
        # waiting to be read. It was true when it was written, which is exactly
        # why a stale list survives review.
        #
        # Applied to the PARSED paragraph, so no tracked document is edited by a
        # script -- the same way the doc/value control does it.
        import test_lane_contract

        stale = (
            "Where the `not_measured` signals live today, so nobody has to go looking:\n\n"
            "- `clock_skew_rejected` -- the platform answers 409 and this lane counts it,\n"
            "  undifferentiated, with every other refusal."
        )
        monkeypatch.setattr(test_lane_contract, "_not_measured_paragraph", lambda: stale)

    elif mode == "health_blocks_on_the_service":
        # The route waits on the identification service for as long as THAT
        # client is willing to, instead of for as long as this lane said. A hung
        # third machine then holds this route open past the patience of whatever
        # polls it, and a lane that is up and serving is published as a dead one.
        monkeypatch.setattr(
            LaneService, "_bounded_identity_health", lambda self, reader: reader()
        )

    elif mode == "version_is_not_checked":
        # The health payload is read whatever version it declares. The value
        # taken off it is then published as `measured` -- the half-read both
        # contracts refuse, on the one payload two other readers refuse.
        monkeypatch.setattr(LaneService, "_known_identity_schema", lambda self, version: True)

    elif mode == "skew_never_recovers":
        # The counts only ever go up. One skew and the code is `active` until
        # the process restarts, however long ago the clock was fixed: a latch
        # that reads like a state, and no `recovered` for a monitor to send.
        original_guard = PlatformTransport._guarded

        def never_resets(self, call):
            skew, unnamed = self.skew_rejected, self.conflicts_unnamed
            result = original_guard(self, call)
            self.skew_rejected = max(self.skew_rejected, skew)
            self.conflicts_unnamed = max(self.conflicts_unnamed, unnamed)
            return result

        monkeypatch.setattr(PlatformTransport, "_guarded", never_resets)

    elif mode == "never_alarm_as_a_string":
        # The flag ships as a string. Every non-empty string is truthy, so a
        # reader that has not been told to require a boolean silences the code
        # it is told `"false"` about -- the reassuring direction, with nothing
        # anywhere reporting it.
        from lane_controller.contract import HealthEntry

        original_dict = HealthEntry.to_dict

        def stringly(self):
            row = original_dict(self)
            row["never_alarm"] = str(row["never_alarm"]).lower()
            return row

        monkeypatch.setattr(HealthEntry, "to_dict", stringly)

    elif mode in ("doc_set_missing_a_member", "enum_gained_a_member"):
        # The two directions of the published closed sets. One drops a member
        # from the DOCUMENT -- a lane an implementer cannot write. The other
        # adds one to the CODE without adding it to the document, which is what
        # every future round does the day it adds a malfunction code.
        #
        # Applied to the parsed block and to the derivation, so no tracked
        # document is edited by a script.
        import test_lane_contract

        if mode == "doc_set_missing_a_member":
            original_payloads = test_lane_contract.doc_payloads

            def doctored():
                doc = original_payloads()
                doc["sets"]["malfunction_codes"] = doc["sets"]["malfunction_codes"][:-1]
                return doc

            monkeypatch.setattr(test_lane_contract, "doc_payloads", doctored)
        else:
            monkeypatch.setitem(
                test_lane_contract.PUBLISHED_SETS,
                "malfunction_codes",
                lambda: [code.value for code in MalfunctionCode] + ["hatch_left_open"],
            )

    else:
        raise RuntimeError(f"unknown BREAK_MEASURED_CODE mode: {mode}")


# ---------------------------------------------------------------------------
# Deliberate breakage, for the ASSISTED VEND fail-control.
#
# `scripts/vend_fail_control.py` sets BREAK_VEND and requires the vend suite to
# FAIL. This is the round's most important control set: every mode below is a
# barrier that opens when it should not, or a record that is missing when it
# does, and each one is a line that carries a guarantee rather than a fixture.
#
# The seven named refusals share ONE mechanism -- the refusal is computed and
# then suppressed -- so a refusal added to the contract is broken here without
# anything being remembered, and a control that passes says the suite measures
# THAT refusal and not something beside it.
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_vend(monkeypatch):
    mode = os.environ.get("BREAK_VEND")
    if not mode:
        return

    from lane_controller import controller as controller_module
    from lane_controller import service as service_module
    from lane_controller import vend as vend_module
    from lane_controller.contract import VendRefusal
    from lane_controller.controller import LaneController
    from lane_controller.vend import AssistedVend

    suppressible = {f"suppress_{refusal.value}": refusal.value for refusal in VendRefusal}

    if mode in suppressible and mode != "suppress_busy":
        # The refusal is computed and thrown away. The lane still knows it
        # should have refused, which is what makes this a break of the ROUTE
        # rather than of the thing it consults.
        code = suppressible[mode]
        original = AssistedVend._refuse

        def without(self, request):
            refusal = original(self, request)
            return None if refusal is not None and refusal.code == code else refusal

        monkeypatch.setattr(AssistedVend, "_refuse", without)

    elif mode == "suppress_busy":
        # A lane that will take a second vend while the first is still
        # settling. Two relays pulsed for one arrival.
        #
        # The flag is CLEARED at the moment the check reads it, rather than
        # replaced with a constant: the rest of the object still works, so the
        # suite fails on the barrier moving twice and not on a broken fixture.
        original_accept = AssistedVend._accept

        def anyway(self, request):
            with self._state:
                self._in_progress = False
            return original_accept(self, request)

        monkeypatch.setattr(AssistedVend, "_accept", anyway)

    elif mode == "presence_not_decides":
        # The refusal stops going through `decide()` and reads the loop
        # itself -- A SECOND COPY of the presence rule. It answers identically
        # today, and it is the shape that drifts: `decide()` moves and the
        # route does not. Only the test that perturbs `decide()` can see it.
        original = AssistedVend._refuse

        def own_copy(self, request):
            from lane_controller.contract import VendRefused

            if not self._service.controller.loop.is_occupied():
                return VendRefused(
                    code=VendRefusal.NO_VEHICLE.value, error="the arming loop reads unoccupied"
                )
            refusal = original(self, request)
            if refusal is not None and refusal.code == VendRefusal.NO_VEHICLE.value:
                return None
            return refusal

        monkeypatch.setattr(AssistedVend, "_refuse", own_copy)

    elif mode == "relay_before_identity":
        # THE ORDER SWAPPED. The barrier opens and the record of who said so is
        # written afterwards -- which, on a lane that loses power between the
        # two, is written never.
        def relay_first(self, identity, *, authorised_by, assisted):
            lane = self.config.lane_id
            self.vend.vend(authorised_by)
            self.last_assisted = dict(assisted)
            self.events.record("assisted_identity", lane, **assisted)
            self.events.record("vended", lane, reason=authorised_by)
            at = controller_module.to_iso(self._clock())
            self.begin_transit(identity, at)
            return at

        monkeypatch.setattr(LaneController, "complete_vend", relay_first)

    elif mode == "replay_vends_again":
        # The idempotency store stops answering, so a caller's retry is a
        # second vend -- and the second one happens after the car has gone.
        monkeypatch.setattr(AssistedVend, "_held", lambda self, key: None)

    elif mode == "read_token_vends":
        # The two tokens become one. Anything that may read this lane may open
        # its barrier, which is the whole reason there are two.
        original = service_module._Handler._authorise

        def either(self, required, other, what, *, unconfigured_refuses):
            return original(
                self, required or other, other, what, unconfigured_refuses=unconfigured_refuses
            )

        monkeypatch.setattr(service_module._Handler, "_authorise", either)

    elif mode == "query_credential_served":
        # A credential in a URL is accepted, and the request is served -- so a
        # working integration publishes its own token on every call, into every
        # access log it passes through.
        monkeypatch.setattr(
            service_module._Handler, "_credential_in_query", lambda self, query: False
        )

    elif mode == "ticket_on_the_pending_event":
        # The reference on the READ contract and in the platform's append-only
        # `events` table: the one identity nothing could ever redact.
        original = LaneController.begin_transit

        def loud(self, identity, at):
            original(self, identity, at)
            if identity.ticket_ref:
                self.events.record(
                    "entry_pending", self.config.lane_id, ticket_ref=identity.ticket_ref, at=at
                )

        monkeypatch.setattr(LaneController, "begin_transit", loud)

    elif mode == "ticket_in_a_log_line":
        # The same exposure through the other door, and the one the payload
        # sweep cannot see.
        original = LaneController.complete_vend

        def chatty(self, identity, *, authorised_by, assisted):
            controller_module.log.warning("completing ticket %s", identity.ticket_ref)
            return original(self, identity, authorised_by=authorised_by, assisted=assisted)

        monkeypatch.setattr(LaneController, "complete_vend", chatty)

    elif mode == "identifier_ticket_kept":
        # The seam stops stripping a ticket an IDENTIFIER supplied, so a
        # third-party identifier mints a parking identity through the interface
        # that exists to report what a camera saw.
        monkeypatch.setattr(controller_module, "replace", lambda identity, **kwargs: identity)

    elif mode == "vend_says_opened":
        # The answer claims the barrier opened. Nothing in this system measures
        # the boom, and this lane's own health surface says so one route away.
        from lane_controller.contract import VendCommanded

        original = VendCommanded.to_dict
        monkeypatch.setattr(
            VendCommanded, "to_dict", lambda self: {**original(self), "opened": True}
        )

    elif mode == "ticket_shape_unchecked":
        # Any string at all is a ticket. The value is a lookup key that is
        # echoed into a platform's responses and read out over a telephone.
        monkeypatch.setattr(vend_module, "is_ticket_ref", lambda value: isinstance(value, str))

    elif mode == "plate_asserted_by_a_caller":
        # The identity kinds open up, so a caller may hand this lane a PLATE --
        # a measurement it did not make, on the record that prices the stay.
        monkeypatch.setattr(vend_module, "VEND_IDENTITY_KINDS", ("ticket", "plate"))

    else:
        raise RuntimeError(f"unknown BREAK_VEND mode: {mode}")
