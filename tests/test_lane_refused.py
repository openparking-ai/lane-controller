"""OP-5-3: a platform that refuses THIS LANE drops nothing queued.

A 401 (token unknown or revoked), a 403, or a 404 that the lane's own rules
read confirms (the lane or garage gone) is not the item's fault. They used to
go down the dead-letter path like a stale close: every stay open and close in
the outbox was dropped, one after another, while the lane went on serving
cars. Now the outbox is held, the flush stops, and `platform_unreachable`
says why.
"""

from __future__ import annotations

from fake_platform import FakePlatform
from lane_controller.contract import HealthState, MalfunctionCode
from lane_controller.platform_client import PlatformRejected
from lane_controller.service import LaneService
from lane_controller.sync import PlatformTransport
from test_outbox_drain import a_lane


class RefusingPlatform(FakePlatform):
    """A fake whose every write -- and, when told, its rules read -- is
    answered with one status, until `refuse` is set back to None."""

    def __init__(self, status: int, *, rules_too: bool = True) -> None:
        super().__init__()
        self.refuse: int | None = status
        self.rules_too = rules_too

    def _maybe_refuse(self):
        if self.refuse is not None:
            raise PlatformRejected(self.refuse, "refused")

    def open_session(self, *args, **kwargs):
        self._maybe_refuse()
        return super().open_session(*args, **kwargs)

    def post_events(self, *args, **kwargs):
        self._maybe_refuse()
        return super().post_events(*args, **kwargs)

    def get_rules(self):
        if self.rules_too:
            self._maybe_refuse()
        return super().get_rules()


def _health(controller, code):
    return LaneService(controller).derived_states()[code]


def test_a_revoked_token_holds_every_stay_and_says_so():
    for status in (401, 403):
        platform = RefusingPlatform(status)
        transport = PlatformTransport(platform)
        controller, vend = a_lane(transport, arrivals=2)
        controller.run_once()
        controller.run_once()
        assert vend.vend_count == 2, "the barrier is not the outbox's to hold"
        held = controller.events.pending
        assert held >= 2, "the premise: two stays are queued"
        assert controller.events.flush() == 0
        assert controller.events.pending == held, f"HTTP {status} dropped a queued stay"
        assert transport.rejected == 0, "a lane refusal is not a dead letter"
        assert transport.lane_refused is not None
        assert _health(controller, MalfunctionCode.PLATFORM_UNREACHABLE) is HealthState.ACTIVE
        assert (
            _health(controller, MalfunctionCode.SESSION_ACTIONS_DEAD_LETTERED) is HealthState.OK
        )
        # The token is fixed: everything held reaches the platform.
        platform.refuse = None
        controller.events.flush()
        assert controller.events.pending == 0
        assert len({o["event_id"] for o in platform.opened}) == 2, "both stays reached it"
        assert transport.lane_refused is None
        assert _health(controller, MalfunctionCode.PLATFORM_UNREACHABLE) is HealthState.OK


def test_a_404_the_rules_read_confirms_is_the_lane_and_holds_the_outbox():
    platform = RefusingPlatform(404, rules_too=True)
    transport = PlatformTransport(platform)
    controller, _ = a_lane(transport, arrivals=1)
    controller.run_once()
    held = controller.events.pending
    assert controller.events.flush() == 0
    assert controller.events.pending == held
    assert transport.rejected == 0


def test_the_control_a_404_for_the_item_alone_is_still_dropped_and_counted():
    """The lane's rules read answers, so the 404 was the item's: dropped and
    counted as it always was, and the queue behind it keeps draining."""
    platform = RefusingPlatform(404, rules_too=False)
    transport = PlatformTransport(platform)
    controller, _ = a_lane(transport, arrivals=1)
    controller.run_once()
    controller.events.flush()
    assert transport.rejected >= 1
    assert transport.lane_refused is None
