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

    elif mode == "outbox_not_kept":
        # The session actions are queued in memory only, as they were: a
        # restart during an outage loses every stay the outbox held.
        from lane_controller import durable as durable_module

        monkeypatch.setattr(durable_module.OutboxStore, "add", lambda self, event: None)

    elif mode == "outbox_not_replayed":
        # The file is written and never read back: the restart starts empty.
        from lane_controller import durable as durable_module

        monkeypatch.setattr(durable_module.OutboxStore, "held", lambda self: [])

    elif mode == "outbox_not_wired":
        # `serve` keeps the outbox in memory whatever the cache does.
        from lane_controller import cli as cli_module

        monkeypatch.setattr(cli_module, "OutboxStore", lambda path, **kwargs: None)

    elif mode == "all_or_nothing":
        # The old flush: the whole queue in one send, everything kept on the
        # first call that does not get through, including what was taken.
        def whole(self):
            with self._flushing:
                batch = self._queue
                if self._transport is None or not batch:
                    return 0
                if not self._transport.send(batch):
                    return 0
                for part in (
                    [e for e in batch if e.kind in ("session_open", "session_close")],
                    [e for e in batch if e.kind not in ("session_open", "session_close")],
                ):
                    if part:
                        self._remove(part)
                return len(batch)

        monkeypatch.setattr(EventQueue, "flush", whole)

    elif mode == "skips_a_failed_part":
        # The flush goes on past a part that did not get through and sends the
        # rest: a close queued behind its lost open reaches the platform first,
        # finds no stay and is refused and dropped, and the open that arrives
        # on the next flush is a stay left open for good.
        from lane_controller.events import _parts

        def skipping(self):
            with self._flushing:
                batch = self._queue
                if self._transport is None or not batch:
                    return 0
                settled = 0
                for part in _parts(batch):
                    if not self._transport.send(part):
                        continue
                    self._remove(part)
                    settled += len(part)
                return settled

        monkeypatch.setattr(EventQueue, "flush", skipping)

    elif mode == "one_log_post":
        # Every log event queued goes in one request, however large.
        from lane_controller import events as events_module
        from lane_controller import sync as sync_module

        monkeypatch.setattr(events_module, "LOG_CHUNK", 10**9)
        monkeypatch.setattr(sync_module, "LOG_CHUNK_BYTES", 10**12)

    elif mode == "no_split_on_413":
        # A 413 is an ordinary refusal: the chunk is dropped and counted.
        def unsplit(self, plain):
            self._guarded(lambda: self._client.post_events(plain))

        monkeypatch.setattr(PlatformTransport, "_post_log", unsplit)

    elif mode == "no_last_send":
        # `serve` stops without one last send.
        from lane_controller import cli as cli_module

        monkeypatch.setattr(cli_module, "_last_send", lambda runner: None)

    elif mode == "terminate_kills":
        # SIGTERM keeps its default action: the process ends on the spot.
        import contextlib

        from lane_controller import cli as cli_module

        monkeypatch.setattr(cli_module, "_terminate_is_an_interrupt", contextlib.nullcontext)

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
# Deliberate breakage, for the descriptor fail-control.
#
# scripts/descriptor_fail_control.py sets BREAK_DESCRIPTOR and requires the
# suites that carry the appearance descriptor to FAIL. Each mode breaks exactly
# one of the three points the descriptor crosses on its way to the platform --
# the translation from the service's contract, the session action, and the
# echo the lane requires back -- so a control that passes says the suite
# measures that crossing and not something beside it.
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_descriptor(monkeypatch):
    mode = os.environ.get("BREAK_DESCRIPTOR")
    if not mode:
        return

    from dataclasses import replace as _replace

    from lane_controller import sync as _sync
    from lane_controller.controller import LaneController
    from lane_controller.vehicle_id_client import VehicleIdClient

    if mode == "translation":
        # The client drops the descriptor on translation again, as it did
        # before this round: the service produced one, the lane never held it.
        original = VehicleIdClient.identify

        def identify_without_descriptor(self, frames):
            return _replace(original(self, frames), descriptor=None)

        monkeypatch.setattr(VehicleIdClient, "identify", identify_without_descriptor)

    elif mode == "record":
        # The lane holds it and the session action does not carry it, so the
        # open goes out exactly as it did before the field existed.
        monkeypatch.setattr(
            LaneController, "_session_descriptor", staticmethod(lambda identity: {})
        )

    elif mode == "echo":
        # The lane sends it and does not require it back. A platform older
        # than the column then answers 201, drops it, and the lane counts the
        # open delivered -- the silent loss the echo exists to make loud.
        monkeypatch.setattr(_sync, "require_descriptor_echo", lambda result, sent, **_: None)

    else:
        raise RuntimeError(f"unknown BREAK_DESCRIPTOR mode: {mode}")


# ---------------------------------------------------------------------------
# Deliberate breakage, for the unadmitted-entry fail-control.
#
# scripts/unadmitted_fail_control.py sets BREAK_UNADMITTED and requires
# tests/test_unadmitted.py to FAIL. Each mode breaks one decision point in the
# controller -- a gate on the idle read, the lock a gate is taken under, a
# name, a reason, the flush, or what the idle read writes -- and never the
# fixture that drives it.
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_unadmitted_entry(monkeypatch):
    mode = os.environ.get("BREAK_UNADMITTED")
    if not mode:
        return

    from lane_controller import controller as controller_module
    from lane_controller.controller import LaneController
    from lane_controller.sync import (
        ENTRY_CONFIRMED,
        REASON_NO_PENDING_ENTRY,
    )

    if mode == "pending":
        # The idle read no longer stands down while a vend is pending, so the
        # crossing that arrives between `begin_transit` and the settle's read
        # is recorded as unadmitted -- and then the settle, reading the same
        # loops, gets nothing and holds an entry for a car that went through.
        assert hasattr(LaneController, "_nothing_pending"), "the pending gate's anchor has moved"
        monkeypatch.setattr(LaneController, "_nothing_pending", lambda self: True)

    elif mode == "outstanding":
        # The idle read no longer stands down while a settle's read of the
        # loops is outstanding: two readers of one board.
        assert hasattr(LaneController, "_nothing_reading"), (
            "the outstanding gate's anchor has moved"
        )
        monkeypatch.setattr(LaneController, "_nothing_reading", lambda self: True)

    elif mode == "starve":
        # THE MUTEX IS PUT BACK. A settle's read waits until no other read is
        # outstanding -- which is what a lock held across the read did. One
        # loop driver that never returns then holds every later settle behind
        # it until its own deadline (unconfirmable, no session) and holds the
        # next ordinary arrival's `run_once` with no deadline at all.
        import time as _time

        assert hasattr(LaneController, "_read_closing_loops"), "the counted read's anchor has moved"
        original_read = LaneController._read_closing_loops

        def read_behind_the_others(self, window, at):
            while not self._nothing_reading():
                _time.sleep(0.005)
            return original_read(self, window, at)

        monkeypatch.setattr(LaneController, "_read_closing_loops", read_behind_the_others)

    elif mode == "overlap":
        # The board's bookkeeping lock is swapped for one that always says
        # yes at construction, so the poll's check and its read are two
        # moments again and a vend's read no longer waits for a poll's read in
        # flight: the two reads overlap on one board, and a crossing that
        # completes inside the overlap goes to whichever driver call wins.
        class _Yes:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            # The board is a `Condition` since the vend route stopped waiting
            # on the poll's read: a lock that always opens also never waits
            # for a poll's claim to be released, and wakes nobody.
            def wait_for(self, predicate, timeout=None):
                return True

            def notify_all(self):
                return None

        original_init = LaneController.__init__

        def init_with_a_board_lock_that_always_opens(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            assert hasattr(self, "_board"), "the board lock's anchor has moved"
            self._board = _Yes()

        monkeypatch.setattr(LaneController, "__init__", init_with_a_board_lock_that_always_opens)

    elif mode == "unlocked_transit":
        # HALF OF THE STRADDLE FIX REVERTED: the transit state is published
        # outside `_board` again. The poll's "nothing pending" check is still
        # taken under the lock, but a vend can now begin between that check
        # and the poll's read, and the crossing that follows the vend is read
        # by the poll -- a car admitted, recorded as one nothing admitted.
        assert hasattr(LaneController, "_transit"), "the transit publisher's anchor has moved"

        def publish_outside_the_lock(self, state, at):
            self.transit_state = state.value
            self.transit_since = at

        monkeypatch.setattr(LaneController, "_transit", publish_outside_the_lock)

    elif mode == "split_check":
        # THE OTHER HALF REVERTED: the "nothing pending" check is taken OUTSIDE
        # `_board` and the read inside it -- two moments, the shape the re-gate
        # of 2026-09-19 blocked. `_transit` still publishes under the lock, and
        # it does not help: the check is stale by the time the board is read.
        assert hasattr(LaneController, "observe_closing_loops"), "the poll's anchor has moved"
        original_poll = LaneController.observe_closing_loops

        def check_outside_then_read(self):
            if self.closing_loops is None:
                return None
            if not LaneController._nothing_pending(self):  # outside the lock
                return None
            self._nothing_pending = lambda: True  # the check under the lock is now a formality
            try:
                return original_poll(self)
            finally:
                del self._nothing_pending

        monkeypatch.setattr(LaneController, "observe_closing_loops", check_outside_then_read)

    elif mode == "route_waits":
        # THE POLL'S READ IS PUT BACK UNDER THE BOARD LOCK -- the shape the
        # outside review of 2026-09-19 measured. `_transit` publishes PENDING
        # under that lock, so a poll driver that hangs holds `begin_transit`,
        # and with it `POST /v1/lane/vend` after the relay has pulsed: no 202
        # for as long as the driver hangs. The board is a re-entrant
        # `Condition`, so the poll's own claim and release still work inside.
        assert hasattr(LaneController, "observe_closing_loops"), "the poll's anchor has moved"
        original_poll = LaneController.observe_closing_loops

        def read_under_the_lock(self):
            with self._board:
                return original_poll(self)

        monkeypatch.setattr(LaneController, "observe_closing_loops", read_under_the_lock)

    elif mode == "unbound_slot":
        # THE BINDING REMOVED: the vend's read takes whatever is in the handed
        # slot without asking whose it is -- the shape the gate of 2026-09-19
        # blocked. A transit that begins after the slot's vend has timed out
        # takes that vend's crossing and is confirmed before its own car has
        # crossed; the car's own crossing then lands as `entry_unadmitted`.
        assert hasattr(LaneController, "_read_closing_loops"), "the counted read's anchor has moved"

        def take_whatever_is_in_the_slot(self, window, at):
            with self._board:
                self._board.wait_for(lambda: not self._poll_reading)
                handed = None
                if self._handed is not None:
                    handed, self._handed = self._handed[0], None
                self._reads_outstanding += 1
            try:
                if handed is not None:
                    return handed
                return self.closing_loops.wait_for_sequence(window)
            finally:
                with self._board:
                    self._reads_outstanding -= 1

        monkeypatch.setattr(LaneController, "_read_closing_loops", take_whatever_is_in_the_slot)

    elif mode == "reason":
        # An ordinary promotion is answered with the new reason: the record
        # of a vend's car going through says a car went through that no vend
        # let in.
        monkeypatch.setattr(controller_module, "REASON_FORWARD", REASON_NO_PENDING_ENTRY)

    elif mode == "folded":
        # The new case is recorded under the ordinary name. A stream of
        # `entry_confirmed` with no vend behind any of them is a broken boom
        # that looks like business as usual.
        monkeypatch.setattr(controller_module, "ENTRY_UNADMITTED", ENTRY_CONFIRMED)

    elif mode == "bypass":
        # The idle read records and does not flush, which every other path
        # that writes a record does. The record sits on a box whose next
        # arrival may never vend.
        assert hasattr(LaneController, "_unadmitted_leaves_the_box"), (
            "the flush seam's anchor has moved"
        )
        monkeypatch.setattr(LaneController, "_unadmitted_leaves_the_box", lambda self: None)

    elif mode == "session":
        # A crossing the camera says no car made opens a stay: the phantom
        # occupant, wearing a different hat.
        from lane_controller.interfaces import VehicleIdentity

        original_crossing = LaneController._crossing_identity

        def anything_is_a_car(self, frames):
            identity = original_crossing(self, frames)
            return identity if identity is not None else VehicleIdentity(plate=None)

        monkeypatch.setattr(LaneController, "_crossing_identity", anything_is_a_car)

    elif mode == "no_stay":
        # The car that crossed with no open command opens no stay, as before
        # the owner's rule: inside the garage, and in no money record.
        monkeypatch.setattr(LaneController, "_crossing_identity", lambda self, frames: None)

    else:
        raise RuntimeError(f"unknown BREAK_UNADMITTED mode: {mode}")


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
        # Left out of what is SERVED, past `LaneHealth`'s own refusal: a short
        # table built directly is refused at construction, the server drops the
        # connection, and no test ever reads a payload with a code missing.
        original_health = service_module.LaneService.health

        class Short(LaneHealth):
            def to_dict(inner):
                payload = LaneHealth.to_dict(inner)
                return {**payload, "codes": payload["codes"][:-1]}

        def short(self):
            return Short(entries=original_health(self).entries)

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

        # Whatever else `_record_session` takes is passed through untouched: a
        # break whose signature is behind the method's raises TypeError on every
        # call, and a body that never runs breaks nothing.
        def photographs_the_plate(self, identity, at, **kwargs):
            self.events.record("entry_photo_taken", self.config.lane_id, plate=identity.plate)
            return original_session(self, identity, at, **kwargs)

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

    elif mode == "history_read_unlocked":
        # The read window iterated in place, without the lock `record` takes:
        # a record between two items raises "deque mutated during iteration".
        from lane_controller.events import EventQueue

        def unlocked(self, cursor):
            return [(seq, event) for seq, event in self._history if seq > cursor]

        monkeypatch.setattr(EventQueue, "since", unlocked)

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

    elif mode == "no_flush_after_the_vend":
        # THE RECORD NEVER LEAVES THE BOX. The order is still right, the
        # identity is still written before the relay, and the platform holds
        # nothing about any of it until some later ordinary arrival flushes --
        # which at a lane using the intercom may never come, and a restart
        # before it loses the record entirely.
        from lane_controller.events import EventQueue

        original_complete = LaneController.complete_vend
        original_flush = EventQueue.flush

        def unflushed(self, identity, *, authorised_by, assisted):
            monkeypatch.setattr(EventQueue, "flush", lambda self: 0)
            try:
                return original_complete(
                    self, identity, authorised_by=authorised_by, assisted=assisted
                )
            finally:
                monkeypatch.setattr(EventQueue, "flush", original_flush)

        monkeypatch.setattr(LaneController, "complete_vend", unflushed)

    elif mode == "decision_not_consumed":
        # THE DECISION IS NOT CONSUMED. One arrival, any number of completions:
        # two idempotency keys, two relay pulses, two tickets, TWO BILLABLE
        # STAYS for one car. Every other refusal passes -- the car is on the
        # loop, the decision is seconds old, the outcome is `fallback`.
        original_complete = LaneController.complete_vend

        def unconsumed(self, identity, *, authorised_by, assisted):
            at = original_complete(self, identity, authorised_by=authorised_by, assisted=assisted)
            self.last_decision_completed_at = None
            return at

        monkeypatch.setattr(LaneController, "complete_vend", unconsumed)

    elif mode == "compare_digest_on_str":
        # The comparison goes back to `str`. `hmac.compare_digest` raises on a
        # non-ASCII one, and the query check is the first thing every request
        # reaches -- so an unauthenticated caller gets a dropped connection and
        # an unhandled traceback instead of the named 401.
        import hmac

        monkeypatch.setattr(
            service_module, "_same", lambda a, b: hmac.compare_digest(a, b)
        )

    elif mode == "key_recorded_on_the_event":
        # The caller's raw `Idempotency-Key` goes back onto `assisted_identity`,
        # which reaches `GET /v1/lane/events` and the platform's append-only
        # `events` table. Whatever a caller wrote there could never be removed.
        original_accept = AssistedVend._accept

        def with_the_key(self, request):
            original_record = LaneController.complete_vend

            def recording(controller, identity, *, authorised_by, assisted):
                assisted = {**assisted, "idempotency_key": request.idempotency_key}
                return original_record(
                    controller, identity, authorised_by=authorised_by, assisted=assisted
                )

            monkeypatch.setattr(LaneController, "complete_vend", recording)
            try:
                return original_accept(self, request)
            finally:
                monkeypatch.setattr(LaneController, "complete_vend", original_record)

        monkeypatch.setattr(AssistedVend, "_accept", with_the_key)

    elif mode == "no_settle_deadline":
        # The settle waits on the loop driver for ever again. A driver that does
        # not return leaves the lane refusing every assisted vend `busy` until a
        # restart, with `transit` reading `pending` -- which is also what a
        # legitimate in-window transit reads.
        def unbounded(self, identity, at):
            controller = self._service.controller
            try:
                controller.resolve_transit(identity, at)
            finally:
                with self._state:
                    self._in_progress = False

        assert hasattr(AssistedVend, "_settle"), "the settle break's anchor has moved"
        monkeypatch.setattr(AssistedVend, "_settle", unbounded)

    elif mode == "subset_widened_to_all_codes":
        # Refusal 2 goes back to refusing on every measured code. The lane then
        # refuses the driver whose reading failed BECAUSE the thing that failed
        # to read them is broken -- the module refusing the case it exists for.
        from lane_controller.contract import MalfunctionCode

        monkeypatch.setattr(vend_module, "VEND_BLOCKING", tuple(MalfunctionCode))

    elif mode == "no_arming_dwell_measure":
        # The dwell is never measured, so `arming_loop_stuck_occupied` can never
        # be `active` and a lane whose arming loops are stuck accepts every
        # assisted vend with nothing in front of the barrier.
        assert hasattr(LaneController, "observe_arming_loop"), "the dwell break's anchor has moved"
        monkeypatch.setattr(LaneController, "observe_arming_loop", lambda self: None)

    elif mode == "finite_check_removed":
        # The duration validator goes back to testing one side of zero. `nan`
        # and `inf` are TOML float literals, `nan <= 0` is False, and
        # `x > nan` is False for every x -- so `completion_max_age_s = nan` is a
        # well-formed configuration file with no staleness refusal in it.
        from lane_controller import config as config_module

        def unchecked(value, name):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be a positive number of seconds, got {value!r}")

        monkeypatch.setattr(config_module, "positive_finite", unchecked)

    else:
        raise RuntimeError(f"unknown BREAK_VEND mode: {mode}")


# ---------------------------------------------------------------------------
# Deliberate breakage, for the deactivate-loop fail-control.
#
# scripts/deactivate_fail_control.py sets BREAK_DEACTIVATE and requires
# tests/test_deactivate_loop.py to FAIL. Each mode breaks one decision point --
# the level, an end of the interval, the record, the vend route's check, the
# stuck measurement, the bound -- and never the fixture that drives it.
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_deactivate_loop(monkeypatch):
    mode = os.environ.get("BREAK_DEACTIVATE")
    if not mode:
        return

    from lane_controller import config as config_module
    from lane_controller import vend as vend_module
    from lane_controller.controller import LaneController
    from lane_controller.service import LaneService

    if mode == "never_held":
        # The loop is read and ignored: the lane arms with a car on it.
        monkeypatch.setattr(
            LaneController, "held_by_deactivate_loop", staticmethod(lambda _: False)
        )

    elif mode == "edge":
        # THE EDGE. The idle turn no longer re-reads the level, so a driver
        # that reported the held car once never gets it served: two queued
        # cars wait for each other. Reverts the idle-turn release only.
        original = LaneController.run_once

        def run_once(self, timeout=None):
            if self._suppressed_since is not None and not self.loop.wait_for_vehicle(
                timeout=timeout
            ):
                self.observe_arming_loop()
                self.observe_deactivate_loop()
                self.observe_closing_loops()
                return None
            return original(self, timeout=timeout)

        assert hasattr(LaneController, "_arm"), "the arming tail's anchor has moved"
        monkeypatch.setattr(LaneController, "run_once", run_once)

    elif mode == "silent_start":
        # The hold happens and nothing is written: the silent non-event.
        def hold(self, geometry):
            if self._suppressed_since is None:
                self._suppressed_since = self._clock()

        monkeypatch.setattr(LaneController, "_hold_arming", hold)

    elif mode == "silent_end":
        # The interval closes in state and never in the record.
        def release(self, ended_by, geometry):
            self._suppressed_since = None

        monkeypatch.setattr(LaneController, "_release_arming", release)

    elif mode == "repeat_start":
        # Every held turn writes the start again: a log nobody can read.
        original_hold = LaneController._hold_arming

        def hold(self, geometry):
            self._suppressed_since = None
            original_hold(self, geometry)

        monkeypatch.setattr(LaneController, "_hold_arming", hold)

    elif mode == "wrong_end":
        # Both ends are reported as the same end.
        from lane_controller.sync import SUPPRESSION_ENDED_ARMED

        original_release = LaneController._release_arming
        monkeypatch.setattr(
            LaneController,
            "_release_arming",
            lambda self, ended_by, geometry: original_release(
                self, SUPPRESSION_ENDED_ARMED, geometry
            ),
        )

    elif mode == "vend_door":
        # The assisted vend no longer applies the check: the intercom is a
        # door around the loop.
        original_complete = vend_module.AssistedVend.complete

        def complete(self, request):
            controller = self._service.controller
            saved = controller.deactivate_loop
            controller.deactivate_loop = None
            try:
                return original_complete(self, request)
            finally:
                controller.deactivate_loop = saved

        assert hasattr(vend_module, "AssistedVend"), "the vend route's anchor has moved"
        monkeypatch.setattr(vend_module.AssistedVend, "complete", complete)

    elif mode == "unmeasured":
        # The stuck code is never derived: `unknown` for ever.
        from lane_controller.contract import HealthState

        monkeypatch.setattr(LaneService, "_deactivate_loop_stuck", lambda self: HealthState.UNKNOWN)

    elif mode == "no_bound":
        # The rule removed: a loop under the longest stopped vehicle's tail is
        # accepted.
        assert hasattr(config_module, "clears_the_tail"), "the rule's anchor has moved"
        monkeypatch.setattr(config_module, "clears_the_tail", lambda spacing_m, tail_m: True)

    elif mode == "floor":
        # The 6.0 m floor from the arming loop put back, as at `9de50c0`: the
        # drawings' lane, whose third loop is 4.12 m from the arming loop and
        # clear of a stopped F-150, is refused.
        assert hasattr(config_module, "clears_the_tail"), "the rule's anchor has moved"
        monkeypatch.setattr(
            config_module, "clears_the_tail", lambda spacing_m, tail_m: spacing_m >= 6.0
        )

    else:
        raise RuntimeError(f"unknown BREAK_DEACTIVATE mode: {mode}")


# ---------------------------------------------------------------------------
# Deliberate breakage, for the production-loop fail-control.
#
# scripts/refresh_fail_control.py sets BREAK_REFRESH and requires
# tests/test_production_loop.py to FAIL. A loop nobody has seen fail to keep
# the cache fresh is not known to keep it fresh.
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_refresh(monkeypatch):
    mode = os.environ.get("BREAK_REFRESH")
    if not mode:
        return

    from lane_controller import cli as cli_module
    from lane_controller import decision as decision_module
    from lane_controller import runner as runner_module

    if mode == "outage_empties":
        # A module the platform could not read is replaced with nothing: the
        # outage becomes an empty register, and every pass holder pays.
        original = decision_module.DecisionCache.load_payload

        def emptying(self, payload, *, now=None):
            original(self, payload, now=now)
            for module, fact in (payload.get("entitlements") or {}).items():
                if isinstance(fact, dict) and fact.get("consulted") and "register" not in fact:
                    self.entitlements[module] = {**fact, "register": {"registrations": []}}

        monkeypatch.setattr(decision_module.DecisionCache, "load_payload", emptying)

    elif mode == "closed_kept":
        # The delta never drops a car that left: the exit lane holds every
        # stay the garage ever had.
        def keeping(self, changes, cursor, *, now=None):
            for stay in changes:
                self.stays[stay["session_id"]] = stay
            self.stays_cursor = str(cursor)

        monkeypatch.setattr(decision_module.DecisionCache, "apply_stay_changes", keeping)

    elif mode == "no_fast_cadence":
        # The refresh thread only ever reads the rules: the stays arrive
        # every five minutes, never every five seconds.
        original_tick = runner_module.LaneRunner.refresh_tick

        def rules_only(self, now):
            if now >= self._next_rules:
                return original_tick(self, now)
            return None

        monkeypatch.setattr(runner_module.LaneRunner, "refresh_tick", rules_only)

    elif mode == "silent_failure":
        # A refresh that could not run is not counted and not said.
        def quiet(self, cadence, name, read):
            try:
                read()
            except Exception:
                pass
            return True

        monkeypatch.setattr(runner_module.LaneRunner, "_refresh", quiet)

    elif mode == "thread_dies":
        # A turn that raises kills the lane thread: the health route keeps
        # answering over a lane that serves nobody.
        def unguarded(self):
            self.state.lane_turns += 1
            self.controller.run_once(timeout=runner_module.LANE_POLL_S)

        monkeypatch.setattr(runner_module.LaneRunner, "lane_turn", unguarded)

    elif mode == "half_configured":
        # `serve` accepts a server_url with no token: a lane that reports to a
        # platform it cannot authenticate to, every send refused, for ever.
        original_for = cli_module.platform_client_for

        def lenient(config, token_file):
            if config.server_url and token_file is None:
                return None
            return original_for(config, token_file)

        monkeypatch.setattr(cli_module, "platform_client_for", lenient)

    elif mode == "redirect_followed":
        # The client's opener follows a redirect, the device token with it.
        import urllib.request

        from lane_controller import platform_client as platform_client_module

        monkeypatch.setattr(platform_client_module, "_OPENER", urllib.request.build_opener())

    elif mode == "http_accepted":
        # Any server_url will do, plain http to another host included.
        from lane_controller import config as config_module

        monkeypatch.setattr(config_module, "server_url_fault", lambda url: None)

    elif mode == "token_file_any_mode":
        # A token file is read whoever else can read it.
        monkeypatch.setattr(cli_module, "SECRET_FORBIDDEN_MODE", 0)

    else:
        raise RuntimeError(f"unknown BREAK_REFRESH mode: {mode}")


# ---------------------------------------------------------------------------
# Deliberate breakage, for the durable-cache fail-control.
#
# scripts/durable_fail_control.py sets BREAK_DURABLE and requires
# tests/test_durable_cache.py to FAIL. A cache nobody has seen fail to survive
# a restart is not known to survive one.
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_store(monkeypatch):
    mode = os.environ.get("BREAK_DURABLE")
    if not mode:
        return

    from lane_controller import decision as decision_module
    from lane_controller import durable as durable_module

    if mode == "not_persisted":
        # Writes go nowhere: a restart finds an empty store.
        monkeypatch.setattr(decision_module.DecisionCache, "_persist", lambda self: None)

    elif mode == "restart_resets_age":
        # A restored cache reads as refreshed NOW: two days old looks fresh.
        original = decision_module.DecisionCache._restore

        def young(self, *, now=None):
            restored = original(self, now=now)
            if restored:
                import time as _time

                self._refreshed_at = _time.time()
            return restored

        monkeypatch.setattr(decision_module.DecisionCache, "_restore", young)

    elif mode == "bound_not_enforced":
        # Personal data past its bound is held for ever.
        monkeypatch.setattr(
            decision_module.DecisionCache, "expire_if_past_bound", lambda self, *, now=None: False
        )

    elif mode == "world_readable":
        # The file is left at the umask: anyone on the box reads the plates.
        monkeypatch.setattr(durable_module, "CACHE_FILE_MODE", 0o644)

    elif mode == "any_directory":
        # Any directory will do: a forged row opens the barrier.
        monkeypatch.setattr(
            durable_module, "cache_directory_fault", lambda mode, owner, process, *, leaf=True: None
        )

    elif mode == "half_persisted":
        # Only the rules are written; the stays and the registers do not survive.
        original_state = decision_module.DecisionCache.state

        def rules_only(self):
            state = original_state(self)
            return {**state, "stays": {}, "entitlements": {}, "plans": []}

        monkeypatch.setattr(decision_module.DecisionCache, "state", rules_only)

    else:
        raise RuntimeError(f"unknown BREAK_DURABLE mode: {mode}")


# ---------------------------------------------------------------------------
# Deliberate breakage, for the exit-decision fail-control.
#
# scripts/exit_decision_fail_control.py sets BREAK_EXIT_DECISION and requires
# tests/test_exit_decision.py to FAIL. An exit that has never been seen to
# decide wrongly is not known to decide rightly.
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_exit_decision(monkeypatch):
    mode = os.environ.get("BREAK_EXIT_DECISION")
    if not mode:
        return

    from lane_controller import controller as controller_module
    from lane_controller import decision as decision_module
    from lane_controller import exit_pricing as pricing_module

    if mode == "day_is_utc":
        # The registers are read against UTC's day, not the garage's.
        original = pricing_module.local_day

        def utc_day(now, timezone):
            return original(now, "UTC")[0], "UTC"

        monkeypatch.setattr(pricing_module, "local_day", utc_day)

    elif mode == "revoked_covers":
        # A pass's state is not read: revoked and suspended cover.
        original = pricing_module.row_covers

        def state_blind(module, row, head, day):
            if module == "garage_pass" and head:
                head = {**head, "state": "active"}
            return original(module, row, head, day)

        monkeypatch.setattr(pricing_module, "row_covers", state_blind)

    elif mode == "uncached_guessed":
        # A transient with no cached entry is priced as if it entered now.
        original = pricing_module.price_exit

        def guessing(identity_text, cache, *, now):
            answer = original(identity_text, cache, now=now)
            if answer.status == pricing_module.NO_CACHED_ENTRY:
                return pricing_module.ExitPricing(
                    status=pricing_module.PRICED, fee_minor=0, currency=cache.currency,
                    plan_version="guessed", computed_from=answer.computed_from,
                )
            return answer

        monkeypatch.setattr(controller_module, "price_exit", guessing)

    elif mode == "platform_on_the_path":
        # The exit asks the platform for its rules before it decides.
        original = pricing_module.price_exit

        def asking(identity_text, cache, *, now):
            import urllib.request

            try:
                urllib.request.urlopen("http://127.0.0.1:9/api/v1/lane/rules", timeout=0.2)
            except Exception:
                pass
            return original(identity_text, cache, now=now)

        monkeypatch.setattr(controller_module, "price_exit", asking)

    elif mode == "plate_in_the_record":
        # The decision event names the plate it decided about.
        original = pricing_module.ExitPricing.to_detail

        def naming(self):
            return {**original(self), "identity": "PASS-1"}

        monkeypatch.setattr(pricing_module.ExitPricing, "to_detail", naming)

    elif mode == "no_folding":
        # The camera's spelling and the registrar's must match byte for byte.
        monkeypatch.setattr(decision_module, "normalise_identity", lambda text: text)

    elif mode == "slow_identifier":
        # The identification service answers slower than the one-second bound.
        # Applied by the stand-in service in tests/test_exit_decision.py itself.
        pass

    elif mode == "o_and_i_not_folded":
        # Case and punctuation folded, O and I not: a registered GO4IT never
        # meets the G041T the reader reads.
        monkeypatch.setattr(
            decision_module,
            "normalise_identity",
            lambda text: "".join(ch for ch in text.upper() if ch.isalnum()),
        )

    elif mode == "monthly_days_ignored":
        # The register's days are not read: a monthly parker unpaid past
        # grace, paused, not started or blocked leaves covered.
        monkeypatch.setattr(pricing_module, "monthly_days_cover", lambda head, day: True)

    elif mode == "deny_at_the_exit":
        # The garage's default "deny" is applied at the exit too: every car
        # with no rule is refused there, pass holders and payers alike.
        original_decide = controller_module.decide

        def denying(identity, cache, **kwargs):
            decision = original_decide(identity, cache, **kwargs)
            if (
                kwargs.get("direction") == "exit"
                and cache.default_action == "deny"
                and decision.should_vend
                and cache.lookup(identity.plate or "") is None
            ):
                return decision_module.Decision(
                    outcome=decision_module.Outcome.DENY,
                    reason="no rule for plate; garage default is deny",
                    identity=identity,
                )
            return decision

        monkeypatch.setattr(controller_module, "decide", denying)

    else:
        raise RuntimeError(f"unknown BREAK_EXIT_DECISION mode: {mode}")


# ---------------------------------------------------------------------------
# scripts/close_decision_fail_control.py sets BREAK_CLOSE_DECISION and requires
# tests/test_close_decision.py to FAIL. A close that has never been seen to
# carry the wrong decision is not known to carry the right one.
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_close_decision(monkeypatch):
    mode = os.environ.get("BREAK_CLOSE_DECISION")
    if not mode:
        return

    from dataclasses import replace as _replace

    from lane_controller import controller as controller_module
    from lane_controller import platform_client as client_module
    from lane_controller import sync as sync_module

    def _rewritten(edit):
        """`PlatformTransport._close_session` handed a COPY of the event whose
        `local_decision` was edited on the way out; the event itself, which the
        lane recorded, is untouched -- so the break is in transit, not in the
        record."""
        original = sync_module.PlatformTransport._close_session

        def in_transit(self, event):
            detail = dict(event.detail)
            if detail.get("local_decision") is not None:
                detail = edit(detail)
            return original(self, _replace(event, detail=detail))

        monkeypatch.setattr(sync_module.PlatformTransport, "_close_session", in_transit)

    if mode == "close_carries_nothing":
        # The close records no decision at all.
        original = controller_module.LaneController._record_session

        def bare(self, identity, at, *, confirmation, exit_pricing=None):
            return original(self, identity, at, confirmation=confirmation)

        monkeypatch.setattr(controller_module.LaneController, "_record_session", bare)

    elif mode == "last_decision_read":
        # The settle reads whatever the lane last decided, not what it was handed.
        original = controller_module.LaneController._record_session

        def from_last(self, identity, at, *, confirmation, exit_pricing=None):
            last = self.last_decision.exit_pricing if self.last_decision else None
            return original(self, identity, at, confirmation=confirmation, exit_pricing=last)

        monkeypatch.setattr(controller_module.LaneController, "_record_session", from_last)

    elif mode == "transport_drops_it":
        # The event carries it; the transport does not hand it to the route.
        _rewritten(lambda detail: {k: v for k, v in detail.items() if k != "local_decision"})

    elif mode == "fee_edited_in_transit":
        # The lane sends a fee other than the one it showed.
        def plus_one(detail):
            decision = dict(detail["local_decision"])
            if decision.get("fee_minor") is not None:
                decision["fee_minor"] = decision["fee_minor"] + 1
            return {**detail, "local_decision": decision}

        _rewritten(plus_one)

    elif mode == "client_drops_it":
        # The client never puts it on the wire.
        original = client_module.PlatformClient.close_session

        def without(self, *, local_decision=None, **kwargs):
            return original(self, **kwargs)

        monkeypatch.setattr(client_module.PlatformClient, "close_session", without)

    elif mode == "null_not_absent":
        # A close with no decision says `null` instead of nothing.
        original = client_module.PlatformClient.close_session

        def nulled(self, *, local_decision=None, **kwargs):
            if local_decision is None:
                bodies = []
                real = self._request

                def capture(method, path, body=None):
                    body = {**(body or {}), "local_decision": None}
                    bodies.append(body)
                    return real(method, path, body)

                self._request = capture
                try:
                    return original(self, local_decision=None, **kwargs)
                finally:
                    self._request = real
            return original(self, local_decision=local_decision, **kwargs)

        monkeypatch.setattr(client_module.PlatformClient, "close_session", nulled)

    elif mode == "close_on_its_own_clock":
        # The close is stamped from a second reading of the clock, taken
        # after the reader and the relay, not with the price's exit time.
        from lane_controller.sync import to_iso

        original = controller_module.LaneController._settle_transit

        def own_clock(self, identity, at, *, exit_pricing=None):
            return original(self, identity, to_iso(self._clock()), exit_pricing=exit_pricing)

        monkeypatch.setattr(controller_module.LaneController, "_settle_transit", own_clock)

    elif mode == "closed_stay_kept":
        # Recording a close leaves the stay in the lane's list: a car that
        # pulls forward again is priced again for it.
        from lane_controller import decision as decision_module

        monkeypatch.setattr(
            decision_module.DecisionCache, "drop_stay", lambda self, session_id: False
        )

    else:
        raise RuntimeError(f"unknown BREAK_CLOSE_DECISION mode: {mode}")


# ---------------------------------------------------------------------------
# scripts/drain_fail_control.py sets BREAK_OUTBOX_DRAIN and requires
# tests/test_outbox_drain.py to FAIL. A drain that has never been seen to hold
# the lane thread is not known to leave it alone.
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_outbox_drain(monkeypatch):
    mode = os.environ.get("BREAK_OUTBOX_DRAIN")
    if not mode:
        return

    from lane_controller import runner as runner_module
    from lane_controller.events import EventQueue

    if mode == "drain_on_the_lane_thread":
        # The runner installs no drain: `deliver()` is the flush, on whatever
        # thread recorded -- the lane thread, after the vend. Measured after
        # PR #28; this is that lane put back.
        monkeypatch.setattr(LaneController, "set_drain", lambda self, drain: None)

    elif mode == "signal_never_flushes":
        # The signal is taken and nothing is sent.
        monkeypatch.setattr(runner_module.LaneRunner, "drain_turn", lambda self: None)

    elif mode == "no_retry":
        # The drain waits for a signal and only a signal: an outbox the
        # platform refused sits until the next car arrives.
        original = runner_module.LaneRunner.start

        def without_retry(self):
            self.drain_retry_s = None
            return original(self)

        monkeypatch.setattr(runner_module.LaneRunner, "start", without_retry)

    elif mode == "clear_after_send":
        # The old flush: the deques are CLEARED after the send returns, so
        # every event recorded during the send is thrown away with the batch.
        def clearing(self):
            with self._flushing:
                batch = self._queue
                if self._transport is None or not batch:
                    return 0
                if not self._transport.send(batch):
                    return 0
                with self._state:
                    self._log.clear()
                    self._sessions.clear()
                return len(batch)

        monkeypatch.setattr(EventQueue, "flush", clearing)

    elif mode == "close_lookup_on_the_lane_thread":
        # The exit asks the platform which session it is closing while it
        # records the close -- on the lane thread, after the vend, with the
        # client's timeout behind it.
        original = LaneController._record_session

        def asking(self, identity, at, **kwargs):
            lookup = getattr(self.events.transport, "session_lookup", None)
            if self.config.direction == "exit" and lookup is not None and identity.plate:
                lookup(identity.plate)
            return original(self, identity, at, **kwargs)

        monkeypatch.setattr(LaneController, "_record_session", asking)

    else:
        raise RuntimeError(f"unknown BREAK_OUTBOX_DRAIN mode: {mode}")


# ---------------------------------------------------------------------------
# scripts/reader_fail_control.py sets BREAK_READER_FEE and requires
# tests/test_reader_fee.py to FAIL. A reader that has never been seen to show
# a recomputed or a stale fee is not known to show the stored one.
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_reader_fee(monkeypatch):
    mode = os.environ.get("BREAK_READER_FEE")
    if not mode:
        return

    from lane_controller import reader as reader_module
    from lane_controller import runner as runner_module

    original_cart_for = reader_module.cart_for
    original_show = LaneController.show_reader

    if mode == "total_resummed":
        # The total added up from the lines instead of read from the record:
        # agrees with the engine every day it writes a fee that is its sum.
        def resummed(record):
            cart = original_cart_for(record)
            if cart is not None:
                cart = {**cart, "total": sum(item["amount"] for item in cart["line_items"])}
            return cart

        monkeypatch.setattr(reader_module, "cart_for", resummed)

    elif mode == "lines_reworded":
        # Each line described by its code rather than the engine's own words.
        def reworded(record):
            cart = original_cart_for(record)
            if cart is not None:
                codes = [line.get("code") or "" for line in record["breakdown"]]
                cart = {**cart, "line_items": [
                    {**item, "description": code or item["description"]}
                    for item, code in zip(cart["line_items"], codes, strict=True)
                ]}
            return cart

        monkeypatch.setattr(reader_module, "cart_for", reworded)

    elif mode == "zero_fee_shows_a_cart":
        # A priced stay of zero is put up as a cart of nothing to pay.
        def zero_cart(record):
            if isinstance(record, dict) and record.get("status") == "priced" \
                    and record.get("fee_minor") == 0:
                return original_cart_for({**record, "fee_minor": 1}) | {"total": 0}
            return original_cart_for(record)

        monkeypatch.setattr(reader_module, "cart_for", zero_cart)

    elif mode == "cart_forced_first":
        # The lane builds the cart and hands THAT over: every reader is a cart
        # reader, and a screen that asks for an input first cannot be plugged in.
        def as_cart(self, record):
            return original_show(self, original_cart_for(record) if record else None)

        monkeypatch.setattr(LaneController, "show_reader", as_cart)

    elif mode == "fee_shown_after_the_vend":
        # The fee reaches the reader only once the barrier has been told to move.
        held: list = []
        original_vend = RecordingVendOutput.vend

        def deferred(self, record):
            if record is not None:
                held.append((self, record))
                return None
            return original_show(self, record)

        def vend_then_show(self, reason):
            original_vend(self, reason)
            while held:
                controller, record = held.pop(0)
                original_show(controller, record)

        monkeypatch.setattr(LaneController, "show_reader", deferred)
        monkeypatch.setattr(RecordingVendOutput, "vend", vend_then_show)

    elif mode == "stale_fee_left_up":
        # Nothing takes the fee down: the next driver reads the last car's.
        def never_cleared(self, record):
            if record is None:
                return None
            return original_show(self, record)

        monkeypatch.setattr(LaneController, "show_reader", never_cleared)

    elif mode == "reader_on_the_lane_thread":
        # The runner installs no hand-off: the reader is called on the lane
        # thread, before the vend, and the barrier waits on a network device.
        monkeypatch.setattr(LaneController, "set_reader_hand", lambda self, hand: None)

    elif mode == "clear_erases_an_unshown_fee":
        # The old slot: whatever was handed last replaces what was waiting,
        # a clear included -- so a close recorded straight after the vend
        # takes down a fee the reader never showed.
        def overwriting(self, record):
            with self._reader_lock:
                self._reader_pending = [record]
            self._reader_signal.set()

        monkeypatch.setattr(runner_module.LaneRunner, "hand_to_reader", overwriting)

    elif mode == "a_raising_reader_stops_the_lane":
        # No guard: a reader off the network raises through the arrival.
        def unguarded(self, record):
            if self.reader is None:
                return None
            if self._reader_hand is not None:
                return self._reader_hand(record)
            return self.reader.present_exit(record)

        monkeypatch.setattr(LaneController, "show_reader", unguarded)

    elif mode == "engine_units_on_the_cart":
        # The engine's minor units handed to the reader as they are: an ISK
        # or UGX fee goes up as a hundredth of itself.
        monkeypatch.setattr(
            reader_module, "stripe_amount", lambda amount_minor, currency: amount_minor
        )

    else:
        raise RuntimeError(f"unknown BREAK_READER_FEE mode: {mode}")


# ---------------------------------------------------------------------------
# scripts/validation_prompt_fail_control.py sets BREAK_VALIDATION_PROMPT and
# requires tests/test_reader_validation.py to FAIL. A prompt that has never
# been seen to show the undiscounted amount first, or to log a number, is not
# known not to.
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_validation_prompt(monkeypatch):
    mode = os.environ.get("BREAK_VALIDATION_PROMPT")
    if not mode:
        return

    from lane_controller import reader as reader_module
    from lane_controller.platform_client import PlatformClient

    original_with = reader_module.with_validation

    if mode == "shown_before_the_claim":
        # The fee as priced goes up first and the discount corrects it after:
        # two numbers, the second after the first.
        def present(self, record):
            self._inner.present_exit(record)
            prompt = reader_module.prompt_for(record)
            if prompt is None:
                return
            phone = self._prompt.ask(prompt)
            if phone:
                shown = reader_module.with_validation(record, self._claim(record, phone))
                if shown is not None:
                    self._inner.present_exit(shown)

        monkeypatch.setattr(reader_module.ValidatingScreen, "present_exit", present)

    elif mode == "fee_worked_out_by_the_lane":
        # The discounted fee added up here from the line, not read.
        def worked_out(record, answer):
            shown = original_with(record, answer)
            if shown is not None:
                shown = {**shown, "fee_minor": record["fee_minor"] + answer["line"]["delta_minor"]}
            return shown

        monkeypatch.setattr(reader_module, "with_validation", worked_out)

    elif mode == "skip_still_claims":
        original_present = reader_module.ValidatingScreen.present_exit

        def present(self, record):
            if reader_module.prompt_for(record) is not None and not self._prompt.typed:
                self._claim(record, "")
            original_present(self, record)

        monkeypatch.setattr(reader_module.ValidatingScreen, "present_exit", present)

    elif mode == "failure_logged_with_its_message":
        # The failure logged with the exception's own words -- which can carry
        # what was sent.
        def present(self, record):
            prompt = reader_module.prompt_for(record)
            shown = record
            if prompt is not None:
                phone = self._prompt.ask(prompt)
                if phone:
                    try:
                        answer = self._claim(record, phone)
                        shown = reader_module.with_validation(record, answer) or record
                    except Exception as err:  # noqa: BLE001
                        reader_module.log.warning("the validation could not be claimed: %s", err)
            self._inner.present_exit(shown)

        monkeypatch.setattr(reader_module.ValidatingScreen, "present_exit", present)

    elif mode == "another_fees_answer_shown":
        def any_fee(record, answer):
            if not isinstance(answer, dict) or answer.get("outcome") != "held":
                return None
            return {**record, "fee_minor": answer["fee_minor"],
                    "breakdown": [*record["breakdown"], answer["line"]]}

        monkeypatch.setattr(reader_module, "with_validation", any_fee)

    elif mode == "no_prompt":
        monkeypatch.setattr(reader_module, "prompt_for", lambda record: None)

    elif mode == "receipt_only_wording":
        monkeypatch.setattr(reader_module, "PROMPT_TEXT", "Text receipt?")

    elif mode == "number_in_the_url":
        from urllib.parse import quote

        def in_url(self, session_id, phone, local_decision):
            answer = self._request(
                "POST",
                f"/api/v1/lane/sessions/{session_id}/validation?phone={quote(phone)}",
                {"phone": phone, "local_decision": local_decision},
            )
            return answer.get("validation") if isinstance(answer, dict) else None

        monkeypatch.setattr(PlatformClient, "claim_validation", in_url)

    elif mode == "shown_not_carried":
        # The close forgets what the reader showed.
        from lane_controller.controller import LaneController

        monkeypatch.setattr(LaneController, "_reader_shown", lambda self, exit_pricing: {})

    elif mode == "discount_after_seal":
        # A late answer is put up after the close was recorded.
        original_seal = reader_module.ValidatingScreen.seal

        def seal_forgetting(self, session_id):
            shown = original_seal(self, session_id)
            self._sealed.pop(session_id, None)
            return shown

        monkeypatch.setattr(reader_module.ValidatingScreen, "seal", seal_forgetting)

    elif mode == "seal_reports_the_record":
        # The seal says the fee as priced, whatever went up.
        original_seal_for_the_record = reader_module.ValidatingScreen.seal

        def seal_record(self, session_id):
            shown = original_seal_for_the_record(self, session_id)
            return None if shown is None else {"fee_minor": 500, "currency": shown["currency"]}

        monkeypatch.setattr(reader_module.ValidatingScreen, "seal", seal_record)

    else:
        raise RuntimeError(f"unknown BREAK_VALIDATION_PROMPT mode: {mode}")


# ---------------------------------------------------------------------------
# scripts/exit_fee_fail_control.py sets BREAK_EXIT_FEE and requires
# tests/test_exit_fee_published.py to FAIL. A fee published for a second screen
# that has never been seen to differ from the reader's is not known to match it.
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_exit_fee(monkeypatch):
    mode = os.environ.get("BREAK_EXIT_FEE")
    if not mode:
        return

    from lane_controller import reader as reader_module
    from lane_controller import service as service_module

    original_fee_for = reader_module.exit_fee_for
    original_show = LaneController.show_reader

    if mode == "fee_resummed":
        # The published figure added up from the record's lines.
        def resummed(decision_at, record, shown=None):
            fee = original_fee_for(decision_at, record, shown)
            if fee is not None and fee.get("fee_minor") and isinstance(record, dict):
                fee = {**fee, "fee_minor": sum(line["delta_minor"] for line in record["breakdown"])}
            return fee

        monkeypatch.setattr(service_module, "exit_fee_for", resummed)
        monkeypatch.setattr(reader_module, "exit_fee_for", resummed)

    elif mode == "digits_guessed":
        # Two decimals for every currency.
        def two(decision_at, record, shown=None):
            fee = original_fee_for(decision_at, record, shown)
            if fee is not None and "minor_unit_digits" in fee:
                fee = {**fee, "minor_unit_digits": 2}
            return fee

        monkeypatch.setattr(service_module, "exit_fee_for", two)

    elif mode == "discount_not_followed":
        # The fee as priced, whatever the reader was given after a validation.
        monkeypatch.setattr(service_module, "exit_fee_for",
                            lambda decision_at, record, shown=None:
                            original_fee_for(decision_at, record, None))

    elif mode == "left_up_after_close":
        # Nothing takes the published fee down once the car has gone.
        def show_keeping(self, record):
            kept = self.exit_screen
            original_show(self, record)
            if record is None:
                self.exit_screen = kept

        monkeypatch.setattr(LaneController, "show_reader", show_keeping)

    elif mode == "figure_without_a_cart":
        # A figure published for a record the reader shows no cart for.
        def any_figure(decision_at, record, shown=None):
            fee = original_fee_for(decision_at, record, shown)
            if fee is not None and fee.get("status") == "priced" and "fee_minor" not in fee:
                fee = {**fee, "fee_minor": record.get("fee_minor"),
                       "currency": record.get("currency"), "minor_unit_digits": 2}
            return fee

        monkeypatch.setattr(reader_module, "exit_fee_for", any_figure)
        monkeypatch.setattr(service_module, "exit_fee_for", any_figure)

    elif mode == "no_reader_no_fee":
        # Published only where a reader is fitted.
        def show_only_with_reader(self, record):
            original_show(self, record)
            if self.reader is None:
                self.exit_screen = None

        monkeypatch.setattr(LaneController, "show_reader", show_only_with_reader)

    elif mode == "stay_on_the_wire":
        # The stay's session id rides along with the fee.
        from lane_controller.contract import ExitFee

        original_exit_fee = service_module.LaneService.exit_fee

        def leaky(self):
            fee = original_exit_fee(self)
            if fee is None:
                return None
            session = self.controller.exit_screen[1].get("session_id")

            class Leaky(ExitFee):
                def to_dict(inner):
                    return {**ExitFee.to_dict(inner), "session_id": session}

            return Leaky(**ExitFee.to_dict(fee))

        monkeypatch.setattr(service_module.LaneService, "exit_fee", leaky)

    elif mode == "fee_as_priced_at_the_close":
        # The seal drops what the reader showed, so between the seal and the
        # reader being cleared the fee as priced is published.
        original_seal = reader_module.ValidatingScreen.seal

        def seal_dropping(self, session_id):
            shown = original_seal(self, session_id)
            if session_id:
                self._shown.pop(session_id, None)
            return shown

        monkeypatch.setattr(reader_module.ValidatingScreen, "seal", seal_dropping)

    elif mode == "figure_outlives_its_hand_over":
        # What went up is answered for the stay, whichever hand-over of it the
        # lane is showing now: a second presentation publishes the first one's.
        def shown_for_the_stay(self, record):
            session_id = record.get("session_id") if isinstance(record, dict) else None
            with self._lock:
                kept = self._shown.get(session_id) if session_id else None
                return None if kept is None else dict(kept[1])

        monkeypatch.setattr(reader_module.ValidatingScreen, "shown_for", shown_for_the_stay)

    elif mode == "left_up_for_the_next_car":
        # An exit that makes no money decision hands the reader nothing -- and
        # nothing is what takes the car before's fee down. Skipped there (and
        # only there: the close still clears), the last car's fee stays up.
        import sys

        def show_unless_nothing_was_decided(self, record):
            if record is None and sys._getframe(1).f_code.co_name == "handle_arrival":
                return
            original_show(self, record)

        monkeypatch.setattr(LaneController, "show_reader", show_unless_nothing_was_decided)

    else:
        raise RuntimeError(f"unknown BREAK_EXIT_FEE mode: {mode}")


# ---------------------------------------------------------------------------
# scripts/exit_tax_fail_control.py sets BREAK_EXIT_TAX and requires
# tests/test_exit_tax.py to FAIL. A tax that has never been seen to be wrong
# is not known to be right (platform 0023).
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_exit_tax(monkeypatch):
    mode = os.environ.get("BREAK_EXIT_TAX")
    if not mode:
        return

    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    from lane_controller import decision as decision_module
    from lane_controller import exit_pricing as pricing_module
    from lane_controller import reader as reader_module

    if mode == "untaxed":
        # The barrier draws the quote alone: no tax is taken.
        monkeypatch.setattr(
            pricing_module, "run_tax",
            lambda request: (200, {"schema_version": 3, "lines": [], "total_minor": 0}),
        )

    elif mode == "chosen_by_the_clock":
        # The set is chosen by the box's real clock, not by the exit instant.
        original = pricing_module.run_tax

        def by_the_clock(request):
            return original({**request, "at": _datetime.now(_UTC).isoformat()})

        monkeypatch.setattr(pricing_module, "run_tax", by_the_clock)

    elif mode == "empty_taxed_at_zero":
        # A cache holding no tax sets prices anyway, at no tax.
        original = pricing_module.price_exit

        def at_zero(identity_text, cache, *, now):
            if not cache.tax_sets:
                cache.tax_sets = [{"effective_from": "2000-01-01T00:00:00Z", "rules": []}]
            return original(identity_text, cache, now=now)

        monkeypatch.setattr(pricing_module, "price_exit", at_zero)

    elif mode == "newest_by_text":
        # The newest set is the latest SPELLING, not the latest instant.
        def by_text(tax_sets):
            newest = max(s["effective_from"] for s in tax_sets)
            return {"count": len(tax_sets), "newest_effective_from": newest}

        monkeypatch.setattr(pricing_module, "tax_sets_held", by_text)

    elif mode == "sets_merged":
        # A refresh merges the payload's sets into the cached ones.
        original = decision_module.DecisionCache.load_payload

        def merging(self, payload, *, now=None):
            before = list(self.tax_sets)
            original(self, payload, now=now)
            self.tax_sets = before + [s for s in self.tax_sets if s not in before]

        monkeypatch.setattr(decision_module.DecisionCache, "load_payload", merging)

    elif mode == "sets_not_kept":
        # The sets are not written to the disk the cache restores from.
        original = decision_module.DecisionCache.state

        def without_sets(self):
            state = original(self)
            state.pop("tax_sets", None)
            return state

        monkeypatch.setattr(decision_module.DecisionCache, "state", without_sets)

    elif mode == "reader_keeps_lane_tax":
        # A held validation is appended after the lane's own tax lines.
        original = reader_module.with_validation

        def keeping(record, answer):
            shown = original(record, answer)
            if shown is not None:
                shown["breakdown"] = [*record["breakdown"], answer["line"], *answer["tax_lines"]]
            return shown

        monkeypatch.setattr(reader_module, "with_validation", keeping)

    elif mode == "reader_claims_on_taxed":
        # The reader takes an answer made on the TAXED fee as one about this record.
        original = reader_module.with_validation

        def on_taxed(record, answer):
            taxed = record.get("fee_minor")
            if isinstance(answer, dict) and answer.get("fee_before_minor") == taxed:
                record = {**record, "subtotal_minor": record.get("fee_minor")}
            return original(record, answer)

        monkeypatch.setattr(reader_module, "with_validation", on_taxed)

    else:
        raise RuntimeError(f"unknown BREAK_EXIT_TAX mode: {mode}")


# ---------------------------------------------------------------------------
# Deliberate breakage, for the closing-and-board fail-control (U4c).
#
# scripts/closing_fail_control.py sets BREAK_CLOSING and requires
# tests/test_lane_closing.py to FAIL. A lane nobody has seen open for a car it
# was closed to is not known to keep it out.
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_closing(monkeypatch):
    mode = os.environ.get("BREAK_CLOSING")
    if not mode:
        return

    from lane_controller import board as board_module
    from lane_controller import controller as controller_module
    from lane_controller import decision as decision_module
    from lane_controller import exit_pricing as pricing_module
    from lane_controller import vend as vend_module
    from lane_controller.contract import VendRefusal, VendRefused

    original_closed_to = decision_module.closed_to

    if mode == "closed_test_dropped":
        # 1: the decision no longer asks whether the lane is closed.
        monkeypatch.setattr(decision_module, "closed_to", lambda cache, direction: None)

    elif mode == "display_code_opens":
        # 1: the vend route lets a display code complete at a closed lane.
        monkeypatch.setattr(vend_module, "closed_to", lambda cache, direction: None)

    elif mode == "full_lets_uncovered_in":
        # 2: `full` is read as open, so a car no register covers gets in.
        monkeypatch.setattr(
            decision_module, "closed_to",
            lambda cache, direction: None
            if original_closed_to(cache, direction) == decision_module.CLOSED_FULL
            else original_closed_to(cache, direction),
        )

    elif mode == "full_refuses_register":
        # 2: `full` is read as everyone, so a register car is turned away.
        monkeypatch.setattr(
            decision_module, "closed_to",
            lambda cache, direction: decision_module.CLOSED_EVERYONE
            if original_closed_to(cache, direction) is not None
            else None,
        )

    elif mode == "person_refused":
        # 3: a person's word is refused at a closed lane too.
        original_refuse = vend_module.AssistedVend._refuse

        def refuse(self, request):
            if vend_module.closed_to(self._service.controller.cache, None) is not None:
                return VendRefused(code=VendRefusal.NOT_COMPLETABLE.value, error="closed")
            return original_refuse(self, request)

        monkeypatch.setattr(vend_module.AssistedVend, "_refuse", refuse)

    elif mode == "closed_exit_publishes_fee":
        # 4: a closed exit puts the fee on the reader anyway.
        original_show = controller_module.LaneController.show_reader

        def show(self, record):
            identity = getattr(self.last_decision, "identity", None)
            if record is None and self.config.direction == "exit" and identity is not None \
                    and identity.plate:
                record = pricing_module.price_exit(identity.plate, self.cache,
                                                   now=self.now()).to_detail()
            return original_show(self, record)

        monkeypatch.setattr(controller_module.LaneController, "show_reader", show)

    elif mode == "slow_read_only":
        # 5: the closing is carried on the slow read only -- the platform's
        # half of the guarantee, planted where this suite's platform serves it.
        import fake_platform

        original_fields = fake_platform.FakePlatform._lane_fields
        monkeypatch.setattr(
            fake_platform.FakePlatform, "_lane_fields",
            lambda self, *, fast: {} if fast else original_fields(self, fast=False),
        )

    elif mode == "fast_read_ignored":
        # 5: the lane's half -- the fast read carries the closing and the lane
        # drops it on the floor.
        monkeypatch.setattr(decision_module.DecisionCache, "apply_lane", lambda self, answer: None)

    elif mode == "closing_not_persisted":
        # 6: the closing and the board are left out of what goes to disk.
        original_state = decision_module.DecisionCache.state

        def without_the_lane(self):
            state = original_state(self)
            return {k: v for k, v in state.items() if k not in ("lane", "board")}

        monkeypatch.setattr(decision_module.DecisionCache, "state", without_the_lane)

    elif mode == "plate_in_the_refusal":
        # 11: the refusal names the car it turned away.
        original_decide = decision_module.decide

        def decide(identity, cache, **kwargs):
            decision = original_decide(identity, cache, **kwargs)
            if decision.fallback is decision_module.Fallback.LANE_CLOSED:
                from dataclasses import replace

                return replace(decision, reason=f"{decision.reason}: {identity.plate}")
            return decision

        monkeypatch.setattr(controller_module, "decide", decide)

    elif mode == "end_time_ignored":
        # 13: a message stays up past the end the owner gave it.
        def in_force(message, now):
            return board_module.__dict__["_original_in_force"]({**message, "ends_at": None}, now)

        monkeypatch.setitem(board_module.__dict__, "_original_in_force", board_module.in_force)
        monkeypatch.setattr(board_module, "in_force", in_force)

    elif mode == "board_untaxed":
        # 14: the board shows the quote without the tax.
        original_charge = board_module.charge

        def untaxed(cache, entry_at, exit_at):
            status, quote, tax = original_charge(cache, entry_at, exit_at)
            return status, quote, ({**tax, "total_minor": 0} if status == 200 else tax)

        monkeypatch.setattr(board_module, "charge", untaxed)

    elif mode == "plan_not_in_force":
        # 14: the board prices from the newest plan, in force or not.
        original_charge = board_module.charge

        def newest_plan(cache, entry_at, exit_at):
            held = cache.plans
            cache.plans = held[-1:]
            try:
                return original_charge(cache, entry_at, exit_at)
            finally:
                cache.plans = held

        monkeypatch.setattr(board_module, "charge", newest_plan)

    elif mode == "pass_only_ignored":
        # 15: the owner's "pass holders only" is not read: the garage's
        # default admits every car it reads.
        original_decide = controller_module.decide

        def taking_anyone(identity, cache, **kwargs):
            said, cache.transient_available = cache.transient_available, None
            try:
                return original_decide(identity, cache, **kwargs)
            finally:
                cache.transient_available = said

        monkeypatch.setattr(controller_module, "decide", taking_anyone)

    elif mode == "pass_only_only_where_allow":
        # 15: the old quirk back -- "pass holders only" read only where the
        # garage's default is allow; a garage whose default is deny, or that
        # has none, decides as if it had never said.
        original_decide = controller_module.decide

        def only_where_allow(identity, cache, **kwargs):
            if cache.transient_available is not False or cache.default_action == "allow":
                return original_decide(identity, cache, **kwargs)
            said, cache.transient_available = cache.transient_available, None
            try:
                return original_decide(identity, cache, **kwargs)
            finally:
                cache.transient_available = said

        monkeypatch.setattr(controller_module, "decide", only_where_allow)

    elif mode == "pass_only_to_a_person":
        # 15: a car no register covers at a pass-only garage handed to a
        # person, as before the owner's rule, instead of the gate kept shut.
        original_decide = controller_module.decide

        def to_a_person(identity, cache, **kwargs):
            decision = original_decide(identity, cache, **kwargs)
            if decision.reason == decision_module.REASON_NOT_REGISTERED:
                from dataclasses import replace

                return replace(
                    decision,
                    outcome=decision_module.Outcome.FALLBACK,
                    fallback=decision_module.Fallback.UNKNOWN_VEHICLE,
                )
            return decision

        monkeypatch.setattr(controller_module, "decide", to_a_person)

    elif mode == "closing_wiped_with_the_cache":
        # 6: the closing goes with the personal data at the bound, so a lane
        # closed `full` reopens itself two days into an outage.
        original_clear = decision_module.DecisionCache.clear

        def wipe_everything(self, **kwargs):
            original_clear(self, **kwargs)
            self.lane = dict(decision_module.OPEN_LANE)
            self._persist()

        monkeypatch.setattr(decision_module.DecisionCache, "clear", wipe_everything)

    else:
        raise RuntimeError(f"unknown BREAK_CLOSING mode: {mode}")


# ---------------------------------------------------------------------------
# Deliberate breakage, for the one-closing-loop fail-control.
#
# scripts/closing_loop_fail_control.py sets BREAK_CLOSING_LOOP and requires
# tests/test_closing_loop.py to FAIL. Each mode breaks one decision about the
# barrier's own closing loop, read through its detector's second output --
# whether it confirms at all, what it confirms, what it may report, and the
# drawing's lane counted inside -- and never the fixture that drives it.
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_closing_loop(monkeypatch):
    mode = os.environ.get("BREAK_CLOSING_LOOP")
    if not mode:
        return

    from lane_controller.config import LaneConfig, LoopConfig
    from lane_controller.controller import LaneController
    from lane_controller.events import EventQueue
    from lane_controller.interfaces import ClosingSequence
    from lane_controller.sync import ENTRY_CONFIRMED, ENTRY_UNADMITTED

    if mode == "unconfirmable":
        # One loop treated as no loops, as before U7e: a lane wired with the
        # closing loop's second output is told it can confirm nothing.
        assert isinstance(LoopConfig.confirms_entry, property), "confirms_entry's anchor has moved"
        monkeypatch.setattr(
            LoopConfig, "confirms_entry", property(lambda self: self.closing_loops == 2)
        )

    elif mode == "occupancy":
        # The closing loop occupied no longer confirms: only A-then-B does, so
        # every car at a one-loop lane is held.
        assert hasattr(LaneController, "_confirms"), "_confirms' anchor has moved"
        monkeypatch.setattr(
            LaneController,
            "_confirms",
            staticmethod(lambda crossing: crossing is ClosingSequence.FORWARD),
        )

    elif mode == "no_vend":
        # Confirmed without a vend: the closing loop occupied with nothing
        # pending is written down as a confirmed entry instead of one nothing
        # admitted.
        original_record = EventQueue.record

        def record(self, kind, *args, **kwargs):
            if kind == ENTRY_UNADMITTED:
                kind = ENTRY_CONFIRMED
            return original_record(self, kind, *args, **kwargs)

        monkeypatch.setattr(EventQueue, "record", record)

    elif mode == "geometry":
        # Every answer accepted whatever the lane declared: a one-loop lane
        # takes a direction no loop there can see, a two-loop lane is confirmed
        # on one occupancy.
        assert hasattr(LaneController, "_as_declared"), "_as_declared's anchor has moved"
        monkeypatch.setattr(LaneController, "_as_declared", lambda self, crossing: crossing)

    elif mode == "drawing":
        # The drawing's lane declared with no closing loops, as a lane built to
        # the drawing had to be before U7e: every car it admits is missing from
        # the count of cars inside.
        original_from_dict = LaneConfig.from_dict.__func__

        def from_dict(cls, raw):
            loops = dict(raw.get("loops", {}))
            loops["closing_loops"] = 0
            return original_from_dict(cls, {**raw, "loops": loops})

        monkeypatch.setattr(LaneConfig, "from_dict", classmethod(from_dict))

    else:
        raise RuntimeError(f"unknown BREAK_CLOSING_LOOP mode: {mode}")


# ---------------------------------------------------------------------------
# scripts/tailgater_fail_control.py sets BREAK_TAILGATER and requires
# tests/test_tailgater.py to FAIL. Each mode breaks one decision of U7f --
# pictures kept with an entry, the tailgater read, the late admitted car, the
# banned car refused -- and never the fixture that drives it.
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_tailgater(monkeypatch):
    mode = os.environ.get("BREAK_TAILGATER")
    if not mode:
        return

    from lane_controller.controller import LaneController
    from lane_controller.events import EventQueue

    if mode == "no_pictures":
        # Nothing kept with an entry: the pictures are taken and dropped.
        assert hasattr(EventQueue, "queue_pictures"), "queue_pictures' anchor has moved"
        monkeypatch.setattr(EventQueue, "queue_pictures", lambda self, event_id, frames: 0)

    elif mode == "read_left_unadmitted":
        # The tailgater read at the closing loop is left an entry nothing
        # admitted, as before U7f: no stay, nothing paid at the exit.
        assert hasattr(LaneController, "_crossing_identity"), (
            "_crossing_identity's anchor has moved"
        )
        original_crossing = LaneController._crossing_identity

        def unread_only(self, frames):
            identity = original_crossing(self, frames)
            return None if identity is not None and identity.plate else identity

        monkeypatch.setattr(LaneController, "_crossing_identity", unread_only)

    elif mode == "late_admitted_listed":
        # The car this lane admitted and held, crossing late, is taken for a
        # tailgater -- and would be banned for being slow.
        assert hasattr(LaneController, "_late_admitted"), "_late_admitted's anchor has moved"
        monkeypatch.setattr(LaneController, "_late_admitted", lambda self, identity: False)

    elif mode == "banned_vends":
        # The car on the banned list is let in: the vend goes ahead.
        assert hasattr(LaneController, "_banned"), "_banned's anchor has moved"
        monkeypatch.setattr(LaneController, "_banned", lambda self, identity: None)

    elif mode == "exit_refuses_banned":
        # F1: the direction guard gone -- an exit keeps a banned car inside.
        assert hasattr(LaneController, "_banned"), "_banned's anchor has moved"

        def everywhere(self, identity):
            if self.banned_matcher is None or not identity.descriptor or not self.cache.banned:
                return None
            return self.banned_matcher.match(identity.descriptor, self.cache.banned)

        monkeypatch.setattr(LaneController, "_banned", everywhere)

    elif mode == "any_car_after_a_held_vend_is_late":
        # F2: the plate is not compared -- any car crossing after a held vend
        # is let off as the late admitted one, and a real tailgater never listed.
        from lane_controller.contract import TransitState

        assert hasattr(LaneController, "_late_admitted"), "_late_admitted's anchor has moved"
        monkeypatch.setattr(
            LaneController,
            "_late_admitted",
            lambda self, identity: (
                self.transit_state == TransitState.HELD.value
                and self.last_decision is not None
                and self.last_decision.should_vend
            ),
        )

    elif mode == "list_not_kept_on_disk":
        # F3: the list is not in what the store holds -- lost on a restart.
        from lane_controller.decision import DecisionCache

        original_state = DecisionCache.state
        monkeypatch.setattr(
            DecisionCache,
            "state",
            lambda self: {k: v for k, v in original_state(self).items() if k != "banned"},
        )

    elif mode == "pictures_dropped_in_an_outage":
        # F4: a send that fails clears the queue -- the pictures are lost.
        original_flush = EventQueue.flush_pictures

        def dropping(self):
            sent = original_flush(self)
            if sent == 0:
                with self._state:
                    self._pictures.clear()
            return sent

        monkeypatch.setattr(EventQueue, "flush_pictures", dropping)

    elif mode == "thresholds_defaulted":
        # G1: a site that states no thresholds gets made-up ones -- cars are
        # refused as banned on an operating point nobody measured.
        from lane_controller import cli
        from lane_controller.vehicle_id_client import VehicleIdSearch

        assert hasattr(cli, "wire_lane"), "wire_lane's anchor has moved"
        original_wire = cli.wire_lane

        def defaulted(config, *, platform):
            wired = original_wire(config, platform=platform)
            if config.vehicle_id_url and wired.controller.banned_matcher is None:
                wired.controller.banned_matcher = VehicleIdSearch(
                    config.vehicle_id_url, structure=0.5, colour=0.5
                )
                wired.seams["banned"] = f"REAL search at {config.vehicle_id_url}"
            return wired

        monkeypatch.setattr(cli, "wire_lane", defaulted)

    else:
        raise RuntimeError(f"unknown BREAK_TAILGATER mode: {mode}")


# ---------------------------------------------------------------------------
# Deliberate breakage, for the fill-rule fail-control (platform 0034).
#
# scripts/fill_rule_fail_control.py sets BREAK_FILL and requires
# tests/test_fill_rule.py to FAIL. A lane nobody has seen price a stay on the
# wrong figure is not known to price it on the right one.
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_fill_figure(monkeypatch):
    mode = os.environ.get("BREAK_FILL")
    if not mode:
        return

    from lane_controller import board as board_module
    from lane_controller import decision as decision_module
    from lane_controller import exit_pricing as pricing_module

    if mode == "figure_dropped":
        # The exit prices the stay without the figure the platform froze on it.
        original = pricing_module.charge

        def without_figure(cache, entry_at, exit_at, occupancy_at_entry_bp=None):
            return original(cache, entry_at, exit_at)

        monkeypatch.setattr(pricing_module, "charge", without_figure)

    elif mode == "figure_not_recorded":
        # The record does not say which figure the stay was priced on.
        original = pricing_module.ExitPricing.to_detail

        def unrecorded(self):
            detail = original(self)
            detail.pop("occupancy_at_entry_bp", None)
            return detail

        monkeypatch.setattr(pricing_module.ExitPricing, "to_detail", unrecorded)

    elif mode == "board_without_figure":
        # The board prices a car coming in now with no figure.
        original = board_module.charge

        def blind(cache, entry_at, exit_at, occupancy_at_entry_bp=None):
            return original(cache, entry_at, exit_at)

        monkeypatch.setattr(board_module, "charge", blind)

    elif mode == "board_guesses_empty":
        # With no number of spaces, the board prices the garage as empty.
        original = decision_module.DecisionCache.occupancy_now_bp

        def empty(self):
            figure = original(self)
            return 0 if figure is None else figure

        monkeypatch.setattr(decision_module.DecisionCache, "occupancy_now_bp", empty)

    elif mode == "not_clamped":
        # More cars than spaces is more than full.
        def unclamped(self):
            if self.spaces is None:
                return None
            return (decision_module.FULL_BP * len(self.stays)) // self.spaces

        monkeypatch.setattr(decision_module.DecisionCache, "occupancy_now_bp", unclamped)

    elif mode == "spaces_not_kept":
        # The number of spaces is not written to the disk the cache restores from.
        original = decision_module.DecisionCache.state

        def forgetful(self):
            state = original(self)
            state.pop("spaces", None)
            return state

        monkeypatch.setattr(decision_module.DecisionCache, "state", forgetful)

    elif mode == "spaces_unchecked":
        # Any value is taken as the number of spaces.
        monkeypatch.setattr(decision_module, "spaces_from_payload", lambda raw: raw)

    else:
        raise RuntimeError(f"unknown BREAK_FILL mode: {mode}")


# ---------------------------------------------------------------------------
# Deliberate breakage, for the entry fail-control (the owner's rule, 2026-10-26).
#
# scripts/entry_stay_fail_control.py sets BREAK_ENTRY_STAY and requires
# tests/test_entry_always_a_stay.py to FAIL. Each mode breaks one guarantee of
# the entry -- every car through both loops a stay, the pictures kept, a
# garage that takes transients opening for every car, a pass-only garage
# keeping the gate shut, no disk on the car's path -- and never the fixture
# that drives it.
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_entry_stay(monkeypatch):
    mode = os.environ.get("BREAK_ENTRY_STAY")
    if not mode:
        return

    from dataclasses import replace

    from lane_controller import controller as controller_module
    from lane_controller import decision as decision_module
    from lane_controller import durable as durable_module
    from lane_controller import events as events_module
    from lane_controller.controller import LaneController
    from lane_controller.events import EventQueue
    from lane_controller.platform_client import PlatformClient
    from lane_controller.sync import CONFIRMED, SESSION_OPEN

    if mode == "no_stay_without_open_command":
        # 1: as before the rule -- a car through the gate with no open command
        # is a stay only if its plate was read; an unread car is in no record.
        original = LaneController._crossing_identity

        def read_only(self, frames):
            identity = original(self, frames)
            read = (
                decision_module.plate_read(identity, self.config.confidence_threshold)
                if identity is not None
                else None
            )
            return identity if read else None

        monkeypatch.setattr(LaneController, "_crossing_identity", read_only)

    elif mode == "not_flagged":
        # 1: the stay no open command let in is not flagged: every one of them
        # is taken for the car this lane admitted.
        monkeypatch.setattr(LaneController, "_late_admitted", lambda self, identity: True)

    elif mode == "held_is_a_stay":
        # 1: an open command with nothing crossing in time opens a stay -- the
        # phantom occupant the loops exist to stop.
        from lane_controller.contract import TransitState

        original = LaneController.resolve_transit

        def holding_is_entering(self, identity, at, **kwargs):
            original(self, identity, at, **kwargs)
            if self.transit_state == TransitState.HELD.value and self.config.direction == "entry":
                self._record_session(identity, at, confirmation=CONFIRMED)

        monkeypatch.setattr(LaneController, "resolve_transit", holding_is_entering)

    elif mode == "stay_as_a_log_event":
        # 1: the stay with no open command is recorded as a log event, not a
        # session action -- bounded, dropped first, and lost on a restart.
        original = EventQueue.record

        def logged(self, kind, lane_id, **detail):
            if kind == SESSION_OPEN and detail.get("tailgater"):
                kind = "entry_stay"
            return original(self, kind, lane_id, **detail)

        monkeypatch.setattr(EventQueue, "record", logged)

    elif mode == "unreadable_takes_the_plate":
        # 1, 3: a plate read below the lane's confidence becomes the stay's plate.
        monkeypatch.setattr(LaneController, "_stay_identity", lambda self, identity: identity)

    elif mode == "unreadable_sent_as_a_plate":
        # 1: "unreadable" goes on the wire as a plate -- one vehicle on the
        # platform for every car nobody could read.
        original = PlatformClient._identity

        def as_a_plate(plate, ticket_ref, plate_unreadable=False):
            if plate_unreadable:
                return {"plate": "UNREADABLE"}
            return original(plate, ticket_ref)

        monkeypatch.setattr(PlatformClient, "_identity", staticmethod(as_a_plate))
        import fake_platform

        original_open = fake_platform.FakePlatform.open_session

        def open_as_sent(self, **kwargs):
            if kwargs.pop("plate_unreadable", False):
                kwargs["plate"] = "UNREADABLE"
            return original_open(self, **kwargs)

        monkeypatch.setattr(fake_platform.FakePlatform, "open_session", open_as_sent)

    elif mode == "arming_read_dropped":
        # 2: the frames the car was identified from are read and thrown away.
        original = LaneController._settle_transit

        def without_arming(self, identity, at, *, exit_pricing=None, arming=()):
            return original(self, identity, at, exit_pricing=exit_pricing)

        monkeypatch.setattr(LaneController, "_settle_transit", without_arming)

    elif mode == "pictures_not_on_disk":
        # 2: the pictures are never written: a restart in an outage loses them.
        monkeypatch.setattr(durable_module.OutboxStore, "add_picture", lambda self, picture: None)

    elif mode == "pictures_capped":
        # 2: the old cap, on disk: past 200 pictures, the rest are not kept.
        original = durable_module.OutboxStore.add_picture

        def capped(self, picture):
            if self.picture_count() < 200:
                return original(self, picture)
            return 1

        monkeypatch.setattr(durable_module.OutboxStore, "add_picture", capped)

    elif mode == "pictures_all_or_nothing":
        # 2: the old send -- every picture in one go, all kept on a failure,
        # the ones already taken included.
        def whole(self):
            send = getattr(self._transport, "send_pictures", None)
            if send is None:
                return 0
            if self._store is not None:
                self.wait_written()
                batch = [picture for _, picture in self._store.held_pictures(10**6)]
                if not batch or not send(batch):
                    return 0
                self._store.remove_pictures([p.picture_ref for p in batch])
                return len(batch)
            with self._state:
                batch = list(self._pictures)
            if not batch or not send(batch):
                return 0
            with self._state:
                self._pictures.clear()
            return len(batch)

        monkeypatch.setattr(EventQueue, "flush_pictures", whole)

    elif mode == "transients_fall_back":
        # 3: the garage's word is not read: every unread car goes to a person.
        monkeypatch.setattr(decision_module, "garage_admits", lambda cache: None)

    elif mode == "no_vehicle_opens":
        # 3: nothing present opens at a garage that takes transients.
        original = controller_module.decide

        def opens_for_nothing(identity, cache, **kwargs):
            decision = original(identity, cache, **kwargs)
            if decision.outcome is decision_module.Outcome.NO_VEHICLE:
                return replace(decision, outcome=decision_module.Outcome.ALLOW)
            return decision

        monkeypatch.setattr(controller_module, "decide", opens_for_nothing)

    elif mode == "full_ignored":
        # 3: a garage that takes transients opens even when it is full.
        original = decision_module._decide_entry

        def never_full(identity, cache, **kwargs):
            return original(identity, cache, **{**kwargs, "full": False})

        monkeypatch.setattr(decision_module, "_decide_entry", never_full)

    elif mode == "low_read_looked_up":
        # 3, 4: a plate read below the lane's confidence is looked up -- and
        # matches a pass that is not this car's.
        def any_read(identity, confidence_threshold):
            return identity.plate if identity.presence is not False else None

        monkeypatch.setattr(decision_module, "plate_read", any_read)
        monkeypatch.setattr(controller_module, "plate_read", any_read)

    elif mode == "settings_wiped":
        # 3: the garage's entry settings go with the personal data at the
        # bound: a day into an outage every car goes to a person.
        original = decision_module.DecisionCache.clear

        def wipe_everything(self, **kwargs):
            original(self, **kwargs)
            self.default_action = None
            self.transient_available = None
            self.lane = dict(decision_module.OPEN_LANE)
            if self._store is not None:
                self._store.wipe()

        monkeypatch.setattr(decision_module.DecisionCache, "clear", wipe_everything)

    elif mode == "pass_only_to_a_person":
        # 4: a car no register covers at a pass-only garage handed to a person.
        original = controller_module.decide

        def to_a_person(identity, cache, **kwargs):
            decision = original(identity, cache, **kwargs)
            if decision.reason == decision_module.REASON_NOT_REGISTERED:
                return replace(
                    decision,
                    outcome=decision_module.Outcome.FALLBACK,
                    fallback=decision_module.Fallback.UNKNOWN_VEHICLE,
                )
            return decision

        monkeypatch.setattr(controller_module, "decide", to_a_person)

    elif mode == "disk_on_the_lane_thread":
        # 5: the stay's open is written on the thread that records it -- the
        # lane's, so the next car waits on the disk.
        def write_now(self, event):
            if self._store is not None:
                self._store_add(event)

        monkeypatch.setattr(EventQueue, "_keep", write_now)

    elif mode == "pictures_disk_on_the_lane_thread":
        # 5: the pictures are written on the thread that takes them.
        original = EventQueue.queue_pictures

        def write_now(self, event_id, frames):
            if self._store is None:
                return original(self, event_id, frames)
            kept = [
                events_module.Picture(event_id=event_id, taken_at=f.captured_at,
                                      image_bytes=f.image_bytes)
                for f in frames
                if f.image_bytes
            ]
            for picture in kept:
                self._store.add_picture(picture)
            return len(kept)

        monkeypatch.setattr(EventQueue, "queue_pictures", write_now)

    elif mode == "unread_is_a_tailgater":
        # B: every car no open command let in is marked a tailgater, read or
        # not -- the dirty-plate monthly ends up on a banned list.
        original = LaneController._record_session

        def everyone_tails(self, identity, at, **kwargs):
            if kwargs.get("no_open_command"):
                kwargs["tailgater"] = True
            return original(self, identity, at, **kwargs)

        monkeypatch.setattr(LaneController, "_record_session", everyone_tails)

    elif mode == "no_mark":
        # B: "no open command let it in" is not said at all.
        original = LaneController._record_session

        def unmarked(self, identity, at, **kwargs):
            kwargs["no_open_command"] = False
            return original(self, identity, at, **kwargs)

        monkeypatch.setattr(LaneController, "_record_session", unmarked)

    elif mode == "unsure_plate_dropped":
        # A: what was read below the confidence is thrown away.
        monkeypatch.setattr(LaneController, "_unsure_plate", lambda self, identity: {})

    elif mode == "pass_only_unread_denied":
        # C: a pass-only car whose pass cannot be confirmed is denied.
        original = controller_module.decide

        def denied(identity, cache, **kwargs):
            decision = original(identity, cache, **kwargs)
            if "the pass could not be confirmed" in decision.reason:
                return replace(decision, outcome=decision_module.Outcome.DENY, fallback=None)
            return decision

        monkeypatch.setattr(controller_module, "decide", denied)

    elif mode == "no_register_denied":
        # C: a pass-only garage holding no register denies every car.
        original = controller_module.decide

        def denied(identity, cache, **kwargs):
            decision = original(identity, cache, **kwargs)
            if "no register is held" in decision.reason:
                return replace(decision, outcome=decision_module.Outcome.DENY, fallback=None)
            return decision

        monkeypatch.setattr(controller_module, "decide", denied)

    elif mode == "pass_only_full_to_a_person":
        # C: a pass-only garage closed full hands an uncovered car to a person.
        original = controller_module.decide

        def to_a_person(identity, cache, **kwargs):
            decision = original(identity, cache, **kwargs)
            if (
                decision.outcome is decision_module.Outcome.DENY
                and decision.reason.startswith("this lane is closed full")
            ):
                return replace(
                    decision,
                    outcome=decision_module.Outcome.FALLBACK,
                    fallback=decision_module.Fallback.LANE_CLOSED,
                )
            return decision

        monkeypatch.setattr(controller_module, "decide", to_a_person)

    elif mode == "display_code_opens_pass_only":
        # C: a display code completes a pass-only garage's person case.
        from lane_controller import vend as vend_module

        monkeypatch.setattr(vend_module, "person_only", lambda controller: False)

    elif mode == "registers_wiped_at_the_bound":
        # 4: a pass-only garage's registers go with the cache a day into an
        # outage, and every monthly waits for a person.
        original = decision_module.DecisionCache.clear

        def wipe_registers(self, **kwargs):
            self.entitlements = {}
            self._reindex_coverage()
            original(self, **kwargs)

        monkeypatch.setattr(decision_module.DecisionCache, "clear", wipe_registers)

    elif mode == "registers_kept_on_reconnect":
        # 4: registers kept through an outage outlive it: the platform answers
        # and a module it could not read keeps what the lane held from before.
        original = decision_module.DecisionCache.load_payload

        def keeping(self, payload, *, now=None):
            self._registers_past_bound = False
            return original(self, payload, now=now)

        monkeypatch.setattr(decision_module.DecisionCache, "load_payload", keeping)

    elif mode == "pictures_uncapped":
        # F: no ceiling on the pictures' share of the outbox file.
        original = durable_module.OutboxStore.add_picture

        def uncapped(self, picture):
            self.pictures_max_bytes = 1 << 62
            return original(self, picture)

        monkeypatch.setattr(durable_module.OutboxStore, "add_picture", uncapped)

    elif mode == "pictures_dropped_unpublished":
        # F: the drops are counted and never published.
        from lane_controller import service as service_module

        original = service_module.LaneService.events

        def unsaid(self, since):
            return replace(original(self, since), pictures_dropped=0)

        monkeypatch.setattr(service_module.LaneService, "events", unsaid)

    elif mode == "stuck_flush":
        # F: the flush reads "the oldest few" again instead of the next page:
        # a page whose removal failed holds every picture behind it.
        original = durable_module.OutboxStore.held_pictures

        def from_the_top(self, limit, *, after=0):
            return original(self, limit)

        monkeypatch.setattr(durable_module.OutboxStore, "held_pictures", from_the_top)

    elif mode == "taken_sent_again":
        # F: a picture whose removal failed is sent again on every flush.
        original = EventQueue._flush_disk_pictures

        def forgetful(self, send):
            with self._state:
                self._sent_unremoved.clear()
            return original(self, send)

        monkeypatch.setattr(EventQueue, "_flush_disk_pictures", forgetful)

    elif mode == "exit_stay_identity":
        # E1: an exit's pending close is made "unreadable" like an entry's.
        original = LaneController.begin_transit

        def stripped(self, identity, at):
            return original(self, self._stay_identity(identity), at)

        monkeypatch.setattr(LaneController, "begin_transit", stripped)

    elif mode == "closing_reopens_at_the_bound":
        # E2: the closing goes with the personal data, as before this round.
        original = decision_module.DecisionCache.clear

        def reopening(self, **kwargs):
            original(self, **kwargs)
            self.lane = dict(decision_module.OPEN_LANE)

        monkeypatch.setattr(decision_module.DecisionCache, "clear", reopening)

    elif mode == "stale_rule_applied":
        # E3: an owner's rule is applied from a cache past its age.
        original = decision_module._decide_entry

        def trusting(identity, cache, **kwargs):
            return original(identity, cache, **{**kwargs, "stale": False})

        monkeypatch.setattr(decision_module, "_decide_entry", trusting)

    elif mode == "assisted_arming_dropped":
        # D: the car a person lets in keeps none of its arming read.
        monkeypatch.setattr(LaneController, "take_completion_arming", lambda self, at: [])

    elif mode == "refused_arming_dropped":
        # D: a refused car that drives in anyway keeps none of its arming read.
        monkeypatch.setattr(LaneController, "_refused_arming_for", lambda self, identity: [])

    elif mode == "refused_arming_any_car":
        # D: the refused car's arming read goes with whatever crosses, even a
        # car whose plate says it is another.
        def any_car(self, identity):
            if self._held_arming is None or self.last_decision is None:
                return []
            return self._take_held_arming()

        monkeypatch.setattr(LaneController, "_refused_arming_for", any_car)

    elif mode == "refusal_keeps_registers":
        # Round 2: a platform that refuses this lane outright (a revoked
        # token) leaves the registers on the box for ever.
        from lane_controller import sync as sync_module

        monkeypatch.setattr(sync_module, "LANE_REFUSED", frozenset())

    elif mode == "no_registers_ceiling":
        # Round 2: no ceiling on a pass-only garage's kept registers.
        monkeypatch.setattr(
            decision_module.DecisionCache, "_registers_past_ceiling", lambda self, current: False
        )
        original_init = decision_module.DecisionCache.__init__

        def endless(self, **kwargs):
            original_init(self, **kwargs)
            self._registers_offline_max = float("inf")

        monkeypatch.setattr(decision_module.DecisionCache, "__init__", endless)

    elif mode == "refused_arming_no_age":
        # Round 2: a refused car's pictures wait for a crossing for ever.
        original = LaneController._refused_arming_for

        def ageless(self, identity):
            if self._held_arming is not None:
                decision_at, frames, _held_at = self._held_arming
                self._held_arming = (decision_at, frames, self._clock())
            return original(self, identity)

        monkeypatch.setattr(LaneController, "_refused_arming_for", ageless)

    elif mode == "refused_arming_unsure_ignored":
        # Round 2: only plates read with confidence are compared, so two
        # different unsure reads are taken for one car.
        original = LaneController._refused_arming_for

        def sure_only(self, identity):
            def strip(read):
                if decision_module.plate_read(read, self.config.confidence_threshold):
                    return read
                return replace(read, plate=None)

            held = self.last_decision
            if held is not None:
                self.last_decision = replace(held, identity=strip(held.identity))
            try:
                return original(self, strip(identity))
            finally:
                self.last_decision = held

        monkeypatch.setattr(LaneController, "_refused_arming_for", sure_only)

    elif mode == "refused_arming_look_ignored":
        # Round 2: the appearance descriptors are not compared.
        monkeypatch.setattr(LaneController, "_same_look", lambda self, refused, identity: True)

    elif mode == "pass_only_cover_before_rule":
        # Round 2: a pass opens a plate the owner denied by rule.
        from lane_controller.exit_pricing import local_day, register_cover

        original = controller_module.decide

        def cover_first(identity, cache, **kwargs):
            decision = original(identity, cache, **kwargs)
            if (
                decision.reason == "plate is denied by rule"
                and decision_module.garage_admits(cache) == decision_module.ADMITS_PASSES_ONLY
                and register_cover(
                    identity.plate, cache, local_day(kwargs["day_at"], cache.timezone)[0]
                )[0]
            ):
                return replace(decision, outcome=decision_module.Outcome.ALLOW)
            return decision

        monkeypatch.setattr(controller_module, "decide", cover_first)

    else:
        raise RuntimeError(f"unknown BREAK_ENTRY_STAY mode: {mode}")


# ---------------------------------------------------------------------------
# Deliberate breakage, for the exit-settled fail-control (the owner's rule for
# the exit, 2026-10-27: the gate opens only when settled).
#
# scripts/exit_settled_fail_control.py sets BREAK_EXIT_SETTLED and requires
# tests/test_exit_case.py to FAIL. An exit nobody has seen open for an unpaid
# car is not known to stay down for one.
# ---------------------------------------------------------------------------


@_pytest.fixture(autouse=True)
def _break_the_settled_exit(monkeypatch):
    mode = os.environ.get("BREAK_EXIT_SETTLED")
    if not mode:
        return

    from lane_controller import exit_case as case_module
    from lane_controller.controller import LaneController
    from lane_controller.exit_pricing import PRICED

    if mode == "priced_opens":
        # A car with a fee to pay is let out on the lane's copy, unpaid.
        original = LaneController._exit_settlement

        def priced_settles(self, decision, identity, frames):
            pricing = decision.exit_pricing
            if decision.should_vend and pricing is not None and pricing.status == PRICED:
                return decision, None
            return original(self, decision, identity, frames)

        monkeypatch.setattr(LaneController, "_exit_settlement", priced_settles)

    elif mode == "unanswered_opens":
        # A platform that cannot be reached is taken as a settled case.
        original = case_module.advance

        def hopeful(case, platform, display, *, now):
            outcome = original(case, platform, display, now=now)
            return case_module.SETTLED if outcome == case_module.UNANSWERED else outcome

        monkeypatch.setattr(case_module, "advance", hopeful)

    elif mode == "no_platform_opens":
        # A lane with no platform wired opens for the car it could not settle.
        def settles_alone(self, case):
            case.outcome = case_module.SETTLED
            return case.outcome

        original = LaneController._step

        def step(self, case):
            return settles_alone(self, case) if self.exits is None else original(self, case)

        monkeypatch.setattr(LaneController, "_step", step)

    elif mode == "plate_in_case":
        # The case carries the plate text.
        original = case_module.id_fields

        def with_plate(identity, *, plate_confident):
            return {**original(identity, plate_confident=plate_confident), "plate": identity.plate}

        monkeypatch.setattr(case_module, "id_fields", with_plate)

    elif mode == "unsure_plate_sent":
        # A plate read below the lane's confidence is sent as the car's ID.
        original = case_module.id_fields
        monkeypatch.setattr(
            case_module, "id_fields",
            lambda identity, *, plate_confident: original(identity, plate_confident=True),
        )

    elif mode == "pictures_after_case":
        # The case is opened before its pictures are sent.
        original = LaneController._exit_settlement

        def case_first(self, decision, identity, frames):
            self.deliver = lambda: None
            try:
                return original(self, decision, identity, frames)
            finally:
                del self.deliver

        monkeypatch.setattr(LaneController, "_exit_settlement", case_first)

    elif mode == "polled_too_fast":
        # The case is asked about on every turn, faster than Stripe is read.
        monkeypatch.setattr(case_module, "EXIT_POLL_SECONDS", 0.0)

    elif mode == "pay_every_turn":
        # The fee is handed to the reader again on every answer.
        original = case_module._act

        def again(case, platform, display, *, now):
            case.sent_at = None
            return original(case, platform, display, now=now)

        monkeypatch.setattr(case_module, "_act", again)

    elif mode == "decline_not_resent":
        # After a decline the fee is never handed to the reader again.
        original = case_module._act

        def once(case, platform, display, *, now):
            if case.needs == case_module.NEEDS_PAYMENT and case.sent_at is not None:
                return None
            return original(case, platform, display, now=now)

        monkeypatch.setattr(case_module, "_act", once)

    elif mode == "reader_driven_after_intent":
        # The lane drives its reader screen for a car whose case has handed
        # its fee to the reader, taking the PaymentIntent off it.
        original = LaneController._note_exit_screen

        def drives(self, record):
            original(self, record)
            if self.reader is not None:
                self.reader.present_exit(record)

        monkeypatch.setattr(LaneController, "_note_exit_screen", drives)

    elif mode == "left_payment_stays_on_reader":
        # A case the lane lets go of keeps its payment on the reader.
        monkeypatch.setattr(case_module, "cancel_on_reader", lambda case, platform: False)

    elif mode == "person_close_unnamed":
        # A person's vend leaves its case off the car's close.
        monkeypatch.setattr(LaneController, "take_completion_exit_id", lambda self, at: None)

    elif mode == "same_decline_not_resent":
        # The fee goes back on the reader only on a new failure code: a second
        # identical decline, or a reader that timed out, is never offered again.
        monkeypatch.setattr(case_module, "EXIT_RESEND_SECONDS", float("inf"))

    elif mode == "attempts_not_counted":
        # The fee reaching the reader is never counted, so a person is never
        # told the driver has tried and failed.
        monkeypatch.setattr(case_module, "EXIT_PAY_ATTEMPTS", 10**9)

    elif mode == "busy_reader_refused":
        # A reader still showing the fee is taken as the platform refusing it.
        monkeypatch.setattr(case_module, "_PAY_NOT_REFUSED", frozenset())

    elif mode == "vend_claims_outside_lock":
        # `complete_vend` takes the case without checking `completed` under the
        # exit lock: a case that settled between the route's check and the
        # vend has moved the barrier, and the person's vend moves it again.
        original = LaneController.complete_vend

        def unclaimed(self, identity, **kwargs):
            self.last_decision_completed_at = None
            return original(self, identity, **kwargs)

        monkeypatch.setattr(LaneController, "complete_vend", unclaimed)

    elif mode == "entry_without_offset_sent":
        # The driver's entry time goes to the platform without its offset.
        monkeypatch.setattr(case_module, "has_offset", lambda entry_at: True)

    elif mode == "refusal_settles":
        # A platform refusal (4xx) of the case is taken as settled.
        original = case_module.advance

        def refused_opens(case, platform, display, *, now):
            outcome = original(case, platform, display, now=now)
            return case_module.SETTLED if case.refused else outcome

        monkeypatch.setattr(case_module, "advance", refused_opens)

    elif mode == "settled_truthy":
        # `settled` is read as truthy, not as `True`.
        monkeypatch.setattr(
            case_module.ExitCase, "settled",
            property(lambda self: bool(self.answer and self.answer.get("settled"))),
        )

    elif mode == "let_out_ignores_person":
        # `_let_out` opens whether or not a person took the case first.
        original = LaneController._let_out

        def regardless(self, case):
            with self._exit_lock:
                self._exit_case = case
            return original(self, case)

        monkeypatch.setattr(LaneController, "_let_out", regardless)

    elif mode == "other_case_answer":
        # An answer naming another case is taken for this one.
        monkeypatch.setattr(case_module, "_own", lambda case, answer: answer)

    elif mode == "receipt_before_vend":
        # The text receipt goes before the barrier opens, adding its network
        # call to a paid car's wait.
        original = LaneController._let_out

        def receipt_first(self, case):
            case_module.send_receipt(case, self.exits, self.exit_display)
            return original(self, case)

        monkeypatch.setattr(LaneController, "_let_out", receipt_first)

    elif mode == "loop_flicker_drops":
        # One clear sample of the arming loop drops the waiting car's case.
        monkeypatch.setattr(case_module, "EXIT_LEFT_SECONDS", 0.0)

    elif mode == "left_case_opens":
        # A car that left the loop keeps its case, and the barrier opens for nobody.
        def regardless(self):
            if self._exit_case is not None:
                self._advance_exit_case(self._exit_case)

        monkeypatch.setattr(LaneController, "observe_exit_case", regardless)

    elif mode == "person_said_every_turn":
        # A case that needs a person says so on every turn.
        original = LaneController._tell_exit_case

        def again(self, case):
            case.told = None
            return original(self, case)

        monkeypatch.setattr(LaneController, "_tell_exit_case", again)

    elif mode == "person_and_case_both_open":
        # A person opens for the car and its case opens the barrier again.
        original = LaneController.complete_vend

        def keeps_case(self, identity, **kwargs):
            case = self._exit_case
            at = original(self, identity, **kwargs)
            self._exit_case = case
            return at

        monkeypatch.setattr(LaneController, "complete_vend", keeps_case)

    elif mode == "close_not_named":
        # The close does not name the case the car left on.
        original = LaneController._settle_transit

        def unnamed(self, identity, at, **kwargs):
            kwargs.pop("exit_id", None)
            return original(self, identity, at, **kwargs)

        monkeypatch.setattr(LaneController, "_settle_transit", unnamed)

    elif mode == "no_receipt":
        # The text receipt the driver asked for is never sent.
        monkeypatch.setattr(case_module, "send_receipt", lambda case, platform, display: False)

    elif mode == "rolled_on_car_let_out":
        # A clear of any length under the two seconds is taken for a flicker:
        # a car that rolls on unarmed after the paid car backed off leaves on
        # that car's case.
        monkeypatch.setattr(case_module, "EXIT_FLICKER_SECONDS", float("inf"))

    elif mode == "flicker_ends_case":
        # Any clear sample ends the case's right to open, flicker or not.
        monkeypatch.setattr(case_module, "EXIT_FLICKER_SECONDS", 0.0)

    elif mode == "loop_sampled_slowly":
        # The loop is sampled at the caller's turn while a car waits, too
        # seldom to tell a flicker from a car that backed off.
        monkeypatch.setattr(case_module, "EXIT_SAMPLE_SECONDS", float("inf"))

    elif mode == "busy_reader_never_told":
        # A reader that answers `reader_busy` for good keeps the car waiting
        # and nobody is told.
        monkeypatch.setattr(case_module, "EXIT_READER_BUSY", 10**9)

    elif mode == "unpriced_case_reader_driven":
        # The lane drives its reader screen for a case car it could not
        # price, taking the platform's PaymentIntent off the reader.
        original = LaneController._note_exit_screen

        def drives_unpriced(self, record):
            original(self, record)
            case = self._exit_case
            if case is not None and case.session_id is None and self.reader is not None:
                self.reader.present_exit(record)

        monkeypatch.setattr(LaneController, "_note_exit_screen", drives_unpriced)

    elif mode == "pay_answer_not_owned" or mode == "first_answer_not_owned":
        # The answer to the /pay (or to the case's opening) is taken without
        # checking it is this case's, under this car's own event.
        import sys

        original = case_module._own
        unchecked = "_act" if mode == "pay_answer_not_owned" else "advance"

        def owned_elsewhere(case, answer):
            if sys._getframe(1).f_code.co_name == unchecked:
                return answer
            return original(case, answer)

        monkeypatch.setattr(case_module, "_own", owned_elsewhere)

    elif mode == "route_busy_after_already_completed":
        # The vend route stays `busy` for good after it refused
        # `already_completed`: no person can open for the next car.
        from lane_controller.contract import VendRefusal
        from lane_controller.vend import AssistedVend

        original = AssistedVend._accept

        def stays_taken(self, request):
            status, body = original(self, request)
            if body.get("code") == VendRefusal.ALREADY_COMPLETED.value:
                self._in_progress = True
            return status, body

        monkeypatch.setattr(AssistedVend, "_accept", stays_taken)

    elif mode == "cancel_before_next_frames":
        # The last car's payment comes off the reader before the next car's
        # frames are taken, so a slow platform holds up the read.
        original = LaneController._drop_exit_case

        def at_once(self, reason):
            original(self, reason)
            self._free_reader()

        monkeypatch.setattr(LaneController, "_drop_exit_case", at_once)

    elif mode == "left_during_call_let_out":
        # A settled answer opens whether or not the car is still on the loop
        # after the platform was asked.
        monkeypatch.setattr(LaneController, "_still_there", lambda self, watch: True)

    elif mode == "loop_unwatched_during_call":
        # The loop is read before and after a platform call and never during
        # it, so a car swapped on while the call ran is not seen.
        monkeypatch.setattr(case_module.LoopWatch, "_run", lambda self: None)

    elif mode == "flicker_doubled":
        # A clear of up to a second is taken for a flicker.
        monkeypatch.setattr(case_module, "EXIT_FLICKER_SECONDS", 1.0)

    elif mode == "busy_not_reset":
        # The busy count is not reset when the reader takes the fee, so busy
        # answers from before the take are counted towards telling a person.
        original = case_module._act

        def keeps_count(case, platform, display, *, now):
            before = case.busy
            original(case, platform, display, now=now)
            if before and case.busy == 0:
                case.busy = before

        monkeypatch.setattr(case_module, "_act", keeps_count)

    elif mode == "word_answer_not_owned":
        # The answer to the driver's word is taken without checking it is
        # this case's, under this car's own event.
        import sys

        original = case_module._own

        def word_unchecked(case, answer):
            caller = sys._getframe(1)
            if (caller.f_code.co_name == "_act"
                    and caller.f_locals.get("needs") == case_module.NEEDS_DRIVER_WORD):
                return answer
            return original(case, answer)

        monkeypatch.setattr(case_module, "_own", word_unchecked)

    elif mode == "watcher_error_trusted":
        # A loop read that raises on the watcher's thread kills it, and the
        # watch then vouches from its last good sample.
        def dies(self, err):
            raise err

        monkeypatch.setattr(case_module.LoopWatch, "_fail", dies)

    elif mode == "loop_read_under_lock":
        # The loop is read holding `_exit_lock`, so a stalled read holds up
        # a person's vend.
        original = LaneController._let_out
        there = LaneController._still_there

        def reads_under_lock(self, case, watch):
            with self._exit_lock:
                seen = there(self, watch)
            self._still_there = lambda watch: seen
            try:
                return original(self, case, watch)
            finally:
                del self._still_there

        monkeypatch.setattr(LaneController, "_let_out", reads_under_lock)

    elif mode == "stop_flicker_drops":
        # The watch's reading as the call returns is not read again when
        # clear, so one flicker then drops a paid car.
        def stops_once(self):
            self._stop.set()
            if self._thread.is_alive():
                self._thread.join()
            self._sample()

        monkeypatch.setattr(case_module.LoopWatch, "stop", stops_once)

    elif mode == "decide_flicker_drops":
        # The lane's reading as it decides is not read again when clear.
        monkeypatch.setattr(LaneController, "_loop_now", lambda self: self.loop.is_occupied())

    elif mode == "three_clear_samples":
        # Three clear samples in a row are needed for a car gone, not two.
        monkeypatch.setattr(case_module, "EXIT_CLEAR_SAMPLES", 3)

    elif mode == "watch_not_stopped_on_raise":
        # The watch is stopped only when the platform call returns, so a
        # call that raises leaves its watcher thread running.
        def unguarded(self, case):
            watch = case_module.LoopWatch(self.loop, self._clock).start()
            settled = self._step(case) == case_module.SETTLED
            watch.stop()
            return settled, watch

        monkeypatch.setattr(LaneController, "_watched_step", unguarded)

    elif mode == "decide_not_read_now":
        # The lane decides from the watch alone, without reading the loop
        # at that moment.
        monkeypatch.setattr(LaneController, "_loop_now", lambda self: True)

    elif mode == "no_final_sample":
        # The watch stops without reading the loop as the call returns.
        def stops_blind(self):
            self._stop.set()
            if self._thread.is_alive():
                self._thread.join()

        monkeypatch.setattr(case_module.LoopWatch, "stop", stops_blind)

    else:
        raise RuntimeError(f"unknown BREAK_EXIT_SETTLED mode: {mode}")
