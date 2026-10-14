"""How full the garage was when the car came in, at the barrier (platform 0034).

The platform freezes onto each stay, when it opens, how full the garage was:
`occupancy_at_entry_bp`, every car inside against the garage's number of
spaces. Its copy of the open stays carries the figure, and the lane prices the
stay on it -- the same figure the platform's close hands the engine -- so a lane
that priced offline and the platform agree to the cent. The figure the lane
priced on travels in its record, and the platform takes the decision only when
it is the stay's own. The price board prices a car coming in now on how full
the garage is now: the cars this lane holds as inside, against `spaces`.

Each claim is paired with the case that falsifies it.
`scripts/fill_rule_fail_control.py` breaks each and requires this file to go red.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime

from rate_engine.contract import run_quote

from lane_controller import DecisionCache
from lane_controller import exit_pricing as pricing
from lane_controller.board import price_lines
from lane_controller.decision import spaces_from_payload
from lane_controller.durable import DurableStore
from test_exit_decision import ENTRY, NOW, PLAN, a_cache, a_platform

EXIT_AT = datetime.fromtimestamp(NOW, UTC).isoformat()

#: The flat plan (3.00 an hour) with a fill rule: at or above 80% full at entry,
#: the time-based charge is 20% more, rounded up.
FILL_PLAN = copy.deepcopy(PLAN)
FILL_PLAN["plan_version"] = "fill-lot-2026-01"
FILL_PLAN["rules"].append({
    "id": "busy", "type": "fill_level", "stage": "ADJUST", "space_classes": ["standard"],
    "threshold_bp": 8000, "percent_bp": 2000, "rounding": "up",
    "raises_specials": "yes", "raises_vip_extra": "no",
})


def fill_platform(*stays, plan=FILL_PLAN, spaces=None):
    """A platform serving `plan`, its open stays `(session_id, plate, entry_at, figure)`."""
    platform = a_platform()
    platform.rate_plans = [plan]
    platform.spaces = spaces
    for session_id, plate, entry_at, figure in stays:
        stay = platform.stay_opened(session_id, plate=plate)
        stay["entry_at"] = entry_at
        stay["occupancy_at_entry_bp"] = figure
    return platform


def as_the_platform_closes(figure, plan=FILL_PLAN):
    """The platform's close: the engine's quote on the stay's own figure (platform
    src/app.js `priceStay`), the same plans, currency, class and instants."""
    request = {"plans": [plan], "currency": "USD", "space_class": "standard",
               "entry_at": ENTRY, "exit_at": EXIT_AT}
    if figure is not None:
        request["occupancy_at_entry_bp"] = figure
    return run_quote(request)


# ---------------------------------------------------------------------------
# the exit prices on the stay's own figure, and agrees with the platform
# ---------------------------------------------------------------------------


def test_at_the_threshold_the_lane_charges_the_fill_rule_and_the_platform_the_same():
    cache = a_cache(fill_platform(("s-1", "FULL-1", ENTRY, 8000)))
    answer = pricing.price_exit("FULL-1", cache, now=NOW)
    assert answer.status == pricing.PRICED, answer
    status, platform = as_the_platform_closes(8000)
    assert status == 200
    assert answer.fee_minor == platform["fee_minor"] == 1080, "2.5 h at 3.00 is 9.00, +20%"
    assert answer.breakdown == platform["breakdown"], "line for line"
    applied = [line for line in answer.breakdown if line["code"] == "fill_level.applied"]
    assert [line["delta_minor"] for line in applied] == [180]


def test_one_basis_point_below_it_is_not_raised_and_still_agrees():
    cache = a_cache(fill_platform(("s-2", "FULL-2", ENTRY, 7999)))
    answer = pricing.price_exit("FULL-2", cache, now=NOW)
    status, platform = as_the_platform_closes(7999)
    assert status == 200
    assert answer.fee_minor == platform["fee_minor"] == 900
    assert any(line["code"] == "fill_level.not_applied" for line in answer.breakdown)


def test_a_stay_with_no_figure_is_refused_by_name_never_priced_as_empty_or_full():
    cache = a_cache(fill_platform(("s-3", "NONE-1", ENTRY, None)))
    answer = pricing.price_exit("NONE-1", cache, now=NOW)
    assert answer.status == pricing.ENGINE_REFUSED
    assert [f["code"] for f in answer.refusal["findings"]] == ["GAP_NO_OCCUPANCY_AT_ENTRY"]


def test_control_a_plan_without_a_fill_rule_prices_the_same_whatever_the_figure():
    fees = {
        figure: pricing.price_exit(
            "PLAIN-1", a_cache(fill_platform(("s-4", "PLAIN-1", ENTRY, figure), plan=PLAN)), now=NOW
        ).fee_minor
        for figure in (None, 0, 8000, 10000)
    }
    assert set(fees.values()) == {900}


def test_the_record_says_which_figure_the_stay_was_priced_on():
    cache = a_cache(fill_platform(("s-5", "REC-1", ENTRY, 8500)))
    answer = pricing.price_exit("REC-1", cache, now=NOW)
    detail = answer.to_detail()
    assert detail["occupancy_at_entry_bp"] == 8500
    assert detail["session_id"] == "s-5"
    cache = a_cache(fill_platform(("s-6", "REC-2", ENTRY, None)))
    unpriced = pricing.price_exit("REC-2", cache, now=NOW)
    assert unpriced.to_detail()["occupancy_at_entry_bp"] is None


# ---------------------------------------------------------------------------
# the board: a car coming in now, at how full the garage is now
# ---------------------------------------------------------------------------


def a_board_cache(open_cars, spaces, plan=FILL_PLAN):
    stays = [(f"b-{i}", f"BRD-{i}", ENTRY, 0) for i in range(open_cars)]
    cache = a_cache(fill_platform(*stays, plan=plan, spaces=spaces))
    cache.board = {"prices": True, "messages": []}
    return cache


def test_the_board_prices_a_car_coming_in_now_on_the_cars_inside_now():
    below = {line["minutes"]: line["fee_minor"] for line in price_lines(a_board_cache(3, 4), NOW)}
    full = {line["minutes"]: line["fee_minor"] for line in price_lines(a_board_cache(4, 4), NOW)}
    assert below[60] == 300, "3 of 4 inside: 75%, below 80%"
    assert full[60] == 360, "4 of 4 inside: full, +20%"


def test_the_figure_now_is_the_platforms_rounded_down_and_never_more_than_full():
    assert a_board_cache(1, 3).occupancy_now_bp() == 3333
    assert a_board_cache(5, 4).occupancy_now_bp() == 10000
    assert a_board_cache(2, None).occupancy_now_bp() is None


def test_with_no_number_of_spaces_a_fill_rule_puts_no_price_up_rather_than_a_guess():
    assert price_lines(a_board_cache(2, None), NOW) == []
    # CONTROL: the same garage without a fill rule puts its prices up.
    plain = price_lines(a_board_cache(2, None, plan=PLAN), NOW)
    assert [line["fee_minor"] for line in plain][:1] == [300]


def test_the_number_of_spaces_is_a_whole_number_above_zero_or_none():
    assert spaces_from_payload(4) == 4
    assert a_cache(fill_platform(spaces=4)).spaces == 4, "the control: a good one is taken"
    for bad in (0, -1, True, "4", 2.5, None, [4]):
        assert spaces_from_payload(bad) is None, bad
        # Through the payload, the way the cache takes it.
        assert a_cache(fill_platform(spaces=bad)).spaces is None, bad


def test_the_number_of_spaces_is_kept_on_disk_restored_and_cleared(tmp_path):
    cache = DecisionCache(max_age_seconds=3600, store=DurableStore(tmp_path / "cache.sqlite"))
    cache.load_payload(fill_platform(spaces=12).get_rules())
    restored = DecisionCache(max_age_seconds=3600, store=DurableStore(tmp_path / "cache.sqlite"))
    assert restored.spaces == 12
    restored.clear()
    assert restored.spaces is None
