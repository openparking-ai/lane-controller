"""U4c: the lane obeys a closing its owner made, and publishes its board.

Each guarantee is paired with the case that falsifies it, and
`scripts/closing_fail_control.py` breaks the lane once per guarantee and
requires this file to go red:

  1  closed to everyone, entry and exit: no car opens by itself -- not a
     covered car, not a priced one, not a car a rule allows, not a display code
  2  full, at an entry: a car a register covers today gets in; an uncovered
     car does not; a car the lane cannot identify goes to a person as today
  3  a person's word -- either human authority -- opens a closed entry, for
     both reasons
  4  an exit closed to everyone shows no fee on the reader and asks for no
     money
  5  a closing reaches the lane on the FAST read, inside one stays cadence
  6  offline and across a restart, the lane keeps the last closing it was told
  11 one event per refused arrival, with the reason, and no plate in it
  13 a board message shows only inside its times, offline too
  14 a price on the board is what the lane would charge for that stay at that
     moment, tax and an event window included
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from fake_platform import FakePlatform
from lane_controller import DecisionCache
from lane_controller.board import BOARD_STAY_MINUTES, board_items, in_force, price_lines
from lane_controller.contract import VendAuthority, VendRefusal
from lane_controller.decision import Fallback, Outcome
from lane_controller.durable import DurableStore
from lane_controller.exit_pricing import price_exit
from lane_controller.interfaces import ClosingSequence, VehicleIdentity
from lane_controller.service import LaneService
from lane_controller.sync import SESSION_CLOSE, SESSION_OPEN, sync_rules, sync_stays
from test_durable_cache import cache_dir  # noqa: F401 -- the fixture, by name
from test_exit_decision import NEW_YORK, NOW, PLAN, gp_register, gp_row
from test_exit_tax import CITY, IN_FORCE
from test_production_loop import a_runner
from test_vend import a_lane, complete, settled

PLATE = "CLSD-123"
HOLDER = "PASS-777"
SEEN = VehicleIdentity(plate=PLATE, confidence=0.97, presence=True)
HOLDER_SEEN = VehicleIdentity(plate=HOLDER, confidence=0.97, presence=True)
UNCLEAR = VehicleIdentity(plate=PLATE, confidence=0.10, presence=True)
MESSAGE = "Garage is full. Monthly parkers only."
EVERYONE = {"state": "closed", "reason": "everyone", "message": "Closed for works.",
            "closed_at": "2026-06-10T12:00:00Z"}
FULL = {"state": "closed", "reason": "full", "message": MESSAGE,
        "closed_at": "2026-06-10T12:00:00Z"}
OPEN = {"state": "open", "reason": None, "message": None, "closed_at": None}


def closed_lane(closing, identities, *, direction="entry", crossings=None, register=None,
                stays=(), default_action="allow"):
    """A lane told `closing` on the fast read, with a register and stays as given,
    on the lane's clock at `NOW` (June 2026, the day the register covers)."""
    controller = a_lane(
        identities=identities, direction=direction, default_action=default_action,
        crossings=crossings if crossings is not None else [(ClosingSequence.FORWARD, 0.0)],
        clock=lambda: NOW,
    )
    cache = controller.cache
    cache.timezone = NEW_YORK
    cache.currency = "USD"
    cache.space_class = "standard"
    cache.plans = [PLAN]
    cache.tax_sets = [IN_FORCE]
    if register is not None:
        cache.entitlements["garage_pass"] = {"consulted": True, "register": register}
        cache._reindex_coverage()
    if stays:
        cache.replace_stays(
            [{"session_id": sid, "open": True, "plate": plate, "ticket_ref": None,
              "entry_at": entry} for sid, plate, entry in stays],
            "1",
        )
    # Told directly, not through the fast read: the fast read is guarantee 5's
    # own subject, and a helper that went through it would break with it.
    cache._take_lane({"lane": closing})
    return controller


def a_register():
    return gp_register(gp_row(HOLDER))


def watch_the_reader(controller) -> list:
    """Every record the lane hands its reader, in order, while the hand-over
    itself goes on as it would."""
    shown: list = []
    original = controller.show_reader

    def show(record):
        shown.append(record)
        original(record)

    controller.show_reader = show
    return shown


def kinds(controller):
    return [event.kind for event in controller.events._queue]


def details(controller, kind):
    return [event.detail for event in controller.events._queue if event.kind == kind]


# ---------------------------------------------------------------------------
# 1 -- closed to everyone: nothing opens it by itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("direction", ["entry", "exit"])
@pytest.mark.parametrize("who", ["covered", "allowed_by_default", "priced", "unclear"])
def test_closed_to_everyone_opens_for_no_car_by_itself(direction, who):
    identity = {"covered": HOLDER_SEEN, "allowed_by_default": SEEN, "priced": SEEN,
                "unclear": UNCLEAR}[who]
    controller = closed_lane(
        EVERYONE, [identity], direction=direction, register=a_register(),
        stays=[("s-1", PLATE, "2026-06-10T15:30:00+00:00")],
    )
    decision = controller.run_once()

    assert decision.outcome is Outcome.FALLBACK
    assert decision.fallback is Fallback.LANE_CLOSED
    assert controller.vend.vend_count == 0, "a lane closed to everyone opened by itself"
    assert SESSION_OPEN not in kinds(controller) and SESSION_CLOSE not in kinds(controller)


def test_the_control_the_same_lanes_open_when_the_lane_is_open():
    """The control on the guarantee above: the same cars, the lane open, and
    every one of them goes through -- so the refusal above is the closing's."""
    for identity, direction in ((HOLDER_SEEN, "entry"), (SEEN, "entry"), (SEEN, "exit")):
        controller = closed_lane(
            OPEN, [identity], direction=direction, register=a_register(),
            stays=[("s-1", PLATE, "2026-06-10T15:30:00+00:00")],
        )
        assert controller.run_once().outcome is Outcome.ALLOW
        assert controller.vend.vend_count == 1


def test_a_display_code_does_not_open_a_closed_lane_for_either_reason():
    for closing in (EVERYONE, FULL):
        controller = closed_lane(closing, [UNCLEAR])
        controller.run_once()
        status, answer = complete(
            LaneService(controller), controller, authority=VendAuthority.DISPLAY_CODE_CONFIRMED,
        )
        assert status == 409 and answer["code"] == VendRefusal.NOT_COMPLETABLE.value
        assert "closed" in answer["error"]
        assert controller.vend.vend_count == 0


def test_the_control_a_display_code_opens_the_same_lane_open():
    controller = closed_lane(OPEN, [UNCLEAR])
    controller.run_once()
    service = LaneService(controller)
    status, _ = complete(service, controller, authority=VendAuthority.DISPLAY_CODE_CONFIRMED)
    settled(service)
    assert status == 202 and controller.vend.vend_count == 1


# ---------------------------------------------------------------------------
# 2 -- full, at an entry
# ---------------------------------------------------------------------------


def test_full_lets_in_a_car_a_register_covers_today():
    controller = closed_lane(FULL, [HOLDER_SEEN], register=a_register())
    decision = controller.run_once()
    assert decision.outcome is Outcome.ALLOW
    assert controller.vend.vend_count == 1


def test_full_does_not_let_in_a_car_no_register_covers():
    controller = closed_lane(FULL, [SEEN], register=a_register())
    decision = controller.run_once()
    assert decision.outcome is Outcome.FALLBACK and decision.fallback is Fallback.LANE_CLOSED
    assert controller.vend.vend_count == 0


def test_full_does_not_let_in_a_car_whose_registration_has_ended():
    ended = gp_register(gp_row(HOLDER, end="2026-06-01"))
    controller = closed_lane(FULL, [HOLDER_SEEN], register=ended)
    assert controller.run_once().fallback is Fallback.LANE_CLOSED
    assert controller.vend.vend_count == 0


def test_full_sends_a_car_it_cannot_identify_to_a_person_as_on_any_day():
    controller = closed_lane(FULL, [UNCLEAR], register=a_register())
    decision = controller.run_once()
    assert decision.outcome is Outcome.FALLBACK
    assert decision.fallback is Fallback.LOW_CONFIDENCE
    assert controller.vend.vend_count == 0


def test_full_at_an_exit_is_acted_on_as_closed_to_everyone():
    controller = closed_lane(FULL, [HOLDER_SEEN], direction="exit", register=a_register())
    assert controller.run_once().fallback is Fallback.LANE_CLOSED
    assert controller.vend.vend_count == 0
    assert LaneService(controller).state().lane.reason == "everyone"


# ---------------------------------------------------------------------------
# 3 -- a person's word opens it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("closing", [EVERYONE, FULL], ids=["everyone", "full"])
@pytest.mark.parametrize(
    "authority", [VendAuthority.HUMAN_OPEN_NOW, VendAuthority.HUMAN_OPEN_AND_FLAG]
)
def test_a_persons_word_opens_a_closed_entry(closing, authority):
    controller = closed_lane(closing, [SEEN])
    assert controller.run_once().fallback is Fallback.LANE_CLOSED
    service = LaneService(controller)
    status, answer = complete(service, controller, authority=authority)
    settled(service)
    assert status == 202, answer
    assert controller.vend.vend_count == 1
    assert SESSION_OPEN in kinds(controller)


# ---------------------------------------------------------------------------
# 4 -- a closed exit asks for no money
# ---------------------------------------------------------------------------


def test_a_closed_exit_shows_no_fee_and_takes_no_money():
    controller = closed_lane(
        EVERYONE, [SEEN], direction="exit",
        stays=[("s-1", PLATE, "2026-06-10T15:30:00+00:00")],
    )
    shown = watch_the_reader(controller)
    controller.run_once()

    assert shown == [None], f"the reader was handed {shown}"
    assert LaneService(controller).state().exit_fee is None
    assert controller.vend.vend_count == 0
    assert not details(controller, SESSION_CLOSE), "a closed exit closed a stay by itself"
    for detail in details(controller, "decision"):
        assert "exit_pricing" not in detail


def test_the_control_the_same_exit_open_puts_the_fee_up():
    controller = closed_lane(
        OPEN, [SEEN], direction="exit", stays=[("s-1", PLATE, "2026-06-10T15:30:00+00:00")],
    )
    shown = watch_the_reader(controller)
    controller.run_once()
    assert shown and shown[0] is not None and shown[0]["status"] == "priced"
    assert shown[0]["fee_minor"] > 0


# ---------------------------------------------------------------------------
# 5 -- the fast read
# ---------------------------------------------------------------------------


def test_a_closing_reaches_the_lane_inside_one_fast_cadence():
    platform = FakePlatform()
    platform.lane = dict(OPEN)
    runner, clock, cache = a_runner(platform, rules_s=300.0, stays_s=5.0)
    runner.prime_schedule()
    assert runner.refresh_tick(clock.now) == "rules"
    assert cache.lane["state"] == "open"

    platform.lane = dict(EVERYONE)
    assert runner.refresh_tick(clock.now + 5.0) == "stays", "the premise: a fast read, not slow"
    assert cache.lane == EVERYONE
    assert platform.rules_reads == 1, "the closing waited for the slow read"

    platform.lane = dict(OPEN)
    assert runner.refresh_tick(clock.now + 10.0) == "stays"
    assert cache.lane["state"] == "open", "the reopening waited for the slow read"


def test_the_board_rides_the_fast_read_too():
    platform = FakePlatform()
    cache = DecisionCache()
    sync_rules(platform, cache)
    platform.board = {"prices": True, "messages": [{"id": "m-1", "text": "Event tonight.",
                                                    "starts_at": None, "ends_at": None}]}
    sync_stays(platform, cache)
    assert cache.board["prices"] is True
    assert [m["text"] for m in cache.board["messages"]] == ["Event tonight."]


def test_a_platform_that_says_nothing_about_the_lane_leaves_it_as_it_was():
    platform = FakePlatform()
    platform.lane = dict(EVERYONE)
    cache = DecisionCache()
    sync_rules(platform, cache)
    platform.lane = None
    sync_stays(platform, cache)
    assert cache.lane == EVERYONE, "silence was read as reopening"


# ---------------------------------------------------------------------------
# 6 -- offline and across a restart
# ---------------------------------------------------------------------------


def test_the_last_closing_holds_offline_and_across_a_restart(cache_dir):  # noqa: F811
    path = cache_dir / "cache.sqlite"
    platform = FakePlatform()
    platform.lane = dict(OPEN)
    first = DecisionCache(max_age_seconds=3600, store=DurableStore(path))
    sync_rules(platform, first)
    platform.lane = dict(FULL)
    platform.board = {"prices": True, "messages": []}
    sync_stays(platform, first)

    platform.online = False
    assert sync_stays(platform, first) is None, "the premise: the platform is unreachable"
    assert first.lane == FULL, "offline, the lane forgot its closing"

    second = DecisionCache(max_age_seconds=3600, store=DurableStore(path))
    assert sync_rules(platform, second) is None
    assert second.lane == FULL, "the restart forgot the closing"
    assert second.board["prices"] is True
    # CONTROL: without a store the same restart is an open lane.
    assert DecisionCache(max_age_seconds=3600).lane["state"] == "open"


def test_past_the_bound_the_closing_goes_with_the_rest_of_the_cache(cache_dir):  # noqa: F811
    path = cache_dir / "cache.sqlite"
    platform = FakePlatform()
    platform.lane = dict(EVERYONE)
    first = DecisionCache(max_age_seconds=60, store=DurableStore(path))
    sync_rules(platform, first)
    assert first.lane == EVERYONE, "the premise: the closing was held"
    assert first.expire_if_past_bound(now=first._refreshed_at + 61) is True
    assert first.lane["state"] == "open"
    assert DecisionCache(max_age_seconds=60, store=DurableStore(path)).lane["state"] == "open"


# ---------------------------------------------------------------------------
# 11 -- one event per refused arrival, and no plate in it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("closing,direction", [(EVERYONE, "entry"), (FULL, "entry"),
                                               (EVERYONE, "exit")])
def test_one_event_per_refused_arrival_with_the_reason_and_no_plate(closing, direction):
    controller = closed_lane(closing, [SEEN], direction=direction)
    controller.run_once()

    refused = details(controller, "fallback_needs_human")
    assert len(refused) == 1, f"{len(refused)} refusal events for one arrival"
    want = "full" if closing is FULL else "everyone"
    assert refused[0]["closed_reason"] == want
    assert refused[0]["fallback"] == Fallback.LANE_CLOSED.value
    for event in controller.events._queue:
        rendered = json.dumps(event.as_dict()["detail"], default=str)
        assert PLATE not in rendered, f"{event.kind} carries the plate: {rendered}"


def test_a_car_the_closing_lets_in_is_not_recorded_as_refused():
    controller = closed_lane(FULL, [HOLDER_SEEN], register=a_register())
    controller.run_once()
    assert details(controller, "fallback_needs_human") == []


# ---------------------------------------------------------------------------
# The read contract publishes the closing
# ---------------------------------------------------------------------------


def test_the_read_contract_publishes_the_closing_and_reopening():
    controller = closed_lane(FULL, [SEEN])
    published = LaneService(controller).state().to_dict()["lane"]
    assert published == {"state": "closed", "reason": "full", "message": MESSAGE}
    controller.cache._take_lane({"lane": OPEN})
    published = LaneService(controller).state().to_dict()["lane"]
    assert published == {"state": "open", "reason": None, "message": None}


# ---------------------------------------------------------------------------
# 13 -- a message shows only inside its times, offline too
# ---------------------------------------------------------------------------

EVENT_START = datetime(2026, 6, 10, 18, 0, tzinfo=UTC)
EVENT_END = EVENT_START + timedelta(hours=4)
TIMED = {"id": "m-1", "text": "Event parking tonight.",
         "starts_at": EVENT_START.isoformat(), "ends_at": EVENT_END.isoformat()}
ALWAYS = {"id": "m-2", "text": "Welcome.", "starts_at": None, "ends_at": None}


def board_texts(cache, at):
    return [item["text"] for item in board_items(cache, now=at.timestamp())
            if item["kind"] == "message"]


def test_a_message_shows_only_inside_its_times_offline_too():
    platform = FakePlatform()
    platform.board = {"prices": False, "messages": [TIMED, ALWAYS]}
    cache = DecisionCache(max_age_seconds=10**9)
    sync_rules(platform, cache)
    platform.online = False  # everything below is the lane's own clock

    assert board_texts(cache, EVENT_START - timedelta(seconds=1)) == ["Welcome."]
    assert board_texts(cache, EVENT_START) == ["Event parking tonight.", "Welcome."]
    assert board_texts(cache, EVENT_END - timedelta(seconds=1)) == [
        "Event parking tonight.", "Welcome."]
    assert board_texts(cache, EVENT_END) == ["Welcome."], "the message outlived its end"


def test_a_time_the_lane_cannot_read_keeps_the_message_down():
    assert in_force({"starts_at": "tonight", "ends_at": None}, EVENT_START.timestamp()) is False


# ---------------------------------------------------------------------------
# 14 -- the price on the board is the price the lane charges
# ---------------------------------------------------------------------------

#: A special-event plan that takes effect at 18:00 New York on the day, at ten
#: a period where the everyday plan charges three.
EVENT_PLAN = copy.deepcopy(PLAN)
EVENT_PLAN["plan_version"] = "event-2026-06-10"
EVENT_PLAN["effective_from"] = "2026-06-10T18:00:00-04:00"
EVENT_PLAN["rules"][0]["first_period_minor"] = 1000
EVENT_PLAN["rules"][0]["repeat_period_minor"] = 1000
#: 8.75%, the exit-tax suite's own set.
TAXED = {"effective_from": "2000-01-01T00:00:00.000000Z", "rules": [CITY]}
EVERYDAY = datetime(2026, 6, 10, 20, 0, tzinfo=UTC)  # 16:00 New York
EVENT = datetime(2026, 6, 10, 22, 30, tzinfo=UTC)  # 18:30 New York


def priced_cache(now):
    cache = DecisionCache(max_age_seconds=10**9)
    cache.load([], now=now)
    cache.timezone, cache.currency, cache.space_class = NEW_YORK, "USD", "standard"
    cache.plans = [PLAN, EVENT_PLAN]
    cache.tax_sets = [TAXED]
    cache.board = {"prices": True, "messages": []}
    return cache


@pytest.mark.parametrize("at", [
    datetime(2026, 6, 10, 20, 0, tzinfo=UTC),   # 16:00 New York: the everyday plan
    datetime(2026, 6, 10, 22, 30, tzinfo=UTC),  # 18:30 New York: the event plan
], ids=["everyday", "event_window"])
def test_every_price_on_the_board_is_what_the_exit_would_charge(at):
    cache = priced_cache(at.timestamp())
    lines = {line["minutes"]: line for line in price_lines(cache, at.timestamp())}
    assert sorted(lines) == sorted(BOARD_STAY_MINUTES)

    for minutes, line in lines.items():
        # THE EXIT, on a stay that entered when the board was read and leaves
        # `minutes` later: the call the reader is handed a fee from.
        cache.replace_stays([{"session_id": "s-b", "open": True, "plate": "BRD-1",
                              "ticket_ref": None, "entry_at": at.isoformat()}], "9")
        charged = price_exit("BRD-1", cache, now=(at + timedelta(minutes=minutes)).timestamp())
        assert charged.status == "priced"
        assert line["fee_minor"] == charged.fee_minor, (
            f"{minutes} min: the board says {line['fee_minor']}, the exit charges "
            f"{charged.fee_minor}"
        )
        assert line["currency"] == "USD" and line["minor_unit_digits"] == 2
        assert charged.fee_minor > charged.subtotal_minor, "the premise: tax is in it"


def test_the_event_window_changes_the_price_on_the_board():
    before = price_lines(priced_cache(EVERYDAY.timestamp()), EVERYDAY.timestamp())
    during = price_lines(priced_cache(EVENT.timestamp()), EVENT.timestamp())
    assert before[0]["fee_minor"] < during[0]["fee_minor"]


def test_a_cache_too_stale_to_price_shows_no_price():
    at = EVERYDAY.timestamp()
    cache = priced_cache(at - 61)
    cache._max_age = 60
    assert cache.is_stale(now=at)
    assert price_lines(cache, at) == []
    assert all(item["kind"] != "prices" for item in board_items(cache, now=at))


def test_no_tax_sets_is_no_price_never_an_untaxed_one():
    cache = priced_cache(EVERYDAY.timestamp())
    cache.tax_sets = []
    assert price_lines(cache, EVERYDAY.timestamp()) == []


def test_prices_are_published_only_where_the_owner_switched_them_on():
    at = EVERYDAY.timestamp()
    cache = priced_cache(at)
    assert [item["kind"] for item in board_items(cache, now=at)] == ["prices"]
    cache.board = {"prices": False, "messages": []}
    assert board_items(cache, now=at) == []


def test_the_read_contract_publishes_the_board():
    controller = closed_lane(OPEN, [SEEN])
    controller.cache.board = {"prices": True, "messages": [ALWAYS]}
    controller.cache._refreshed_at = NOW
    board = LaneService(controller).state().to_dict()["board"]
    assert [item["kind"] for item in board["items"]] == ["message", "prices"]
    assert board["items"][0] == {"kind": "message", "text": "Welcome."}
