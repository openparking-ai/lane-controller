"""Tax on what the driver pays, at the barrier (platform 0023).

The lane quotes the stay, then takes the garage's tax on the quote's total at
the exit -- `rate_engine.contract.run_tax`, the function behind the platform's
`POST /v1/tax`, in-process -- and the fee the barrier draws and the reader
totals is the taxed total. Its record says the subtotal, and two facts about
the tax sets its cache held: how many, and the newest instant among them. It
never names the set it used: which set is in force is the engine's choice
alone, and the platform judges a stale copy by those two facts.

At the reader, a held validation puts up the ledger in the one order -- base
lines, the validation, the platform's tax on the discounted subtotal -- and
the platform's taxed figure.

Each claim is paired with the case that falsifies it.
`scripts/exit_tax_fail_control.py` breaks each and requires this file to go red.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime

from rate_engine.contract import run_quote, run_tax

from lane_controller import DecisionCache, reader
from lane_controller import exit_pricing as pricing
from lane_controller.durable import DurableStore
from test_exit_decision import ENTRY, NOW, PLAN, a_cache, a_platform, gp_register, gp_row

#: 8.75%, rounded up: 78.75 on 9.00 is 0.79.
CITY = {"id": "city", "label": "City parking tax", "percent_bp": 875, "rounding": "up",
        "sequence": 1}
CITY_10 = {**CITY, "percent_bp": 1000}
IN_FORCE = {"effective_from": "2000-01-01T00:00:00.000000Z", "rules": [CITY]}
NONE = {"effective_from": "2000-01-01T00:00:00.000000Z", "rules": []}
EXIT_AT = datetime.fromtimestamp(NOW, UTC).isoformat()


def taxed_platform(*sets, stays=(("s-1", "TAXD-1", ENTRY),)):
    platform = a_platform(stays=stays)
    platform.tax_sets = list(sets)
    return platform


def priced(cache, identity="TAXD-1", now=NOW):
    answer = pricing.price_exit(identity, cache, now=now)
    assert answer.status == pricing.PRICED, answer
    return answer


def the_quote(entry=ENTRY, exit_at=EXIT_AT):
    status, body = run_quote({"plans": [PLAN], "currency": "USD", "space_class": "standard",
                              "entry_at": entry, "exit_at": exit_at})
    assert status == 200, body
    return body


# ---------------------------------------------------------------------------
# the fee the barrier draws is taxed, and the record says what it was taxed from
# ---------------------------------------------------------------------------


def test_a_priced_exit_is_taxed_last_on_the_quotes_total():
    answer = priced(a_cache(taxed_platform(IN_FORCE)))
    quote = the_quote()
    assert quote["fee_minor"] == 900, "2.5 h at 300 an hour, three periods"
    assert answer.subtotal_minor == 900
    tax = [line for line in answer.breakdown if line["code"] == "tax.applied"]
    assert [(t["rule_id"], t["delta_minor"]) for t in tax] == [("city", 79)]
    assert answer.breakdown[-1]["code"] == "tax.applied", "the tax is the last line"
    assert answer.breakdown[: -len(tax)] == quote["breakdown"], "the engine's lines, untouched"
    assert answer.fee_minor == 979
    assert sum(line["delta_minor"] for line in answer.breakdown) == answer.fee_minor
    # The lines ARE the engine's: run_tax itself, on the same subtotal at the same instant.
    status, direct = run_tax(
        {"tax_sets": [IN_FORCE], "subtotal_minor": 900, "currency": "USD", "at": EXIT_AT}
    )
    assert status == 200 and tax == direct["lines"]
    detail = answer.to_detail()
    assert (detail["subtotal_minor"], detail["fee_minor"]) == (900, 979)


def test_control_a_set_with_no_rules_taxes_nothing_and_the_fee_is_the_quote():
    answer = priced(a_cache(taxed_platform(NONE)))
    assert answer.fee_minor == answer.subtotal_minor == the_quote()["fee_minor"]
    assert not [line for line in answer.breakdown if line["code"] == "tax.applied"]


def test_the_set_is_chosen_by_the_exit_instant():
    """A law change at 18:00 UTC: one second before it, the old rate; at it, the new."""
    change = {"effective_from": "2026-06-10T18:00:00.000000Z", "rules": [CITY_10]}
    cache = a_cache(taxed_platform(IN_FORCE, change))
    before = priced(cache, now=NOW - 1)
    at = priced(cache, now=NOW)
    def tax(answer):
        return [line["delta_minor"] for line in answer.breakdown if line["code"] == "tax.applied"]

    assert tax(before) == [79] and tax(at) == [90]


def test_the_record_carries_how_many_sets_and_the_newest_INSTANT_never_the_set_used():
    # Text order and instant order disagree: 23:30 at -05:00 is 04:30Z, after 03:00Z.
    early_text_late_instant = {"effective_from": "2025-12-31T23:30:00-05:00", "rules": [CITY]}
    late_text_early_instant = {"effective_from": "2026-01-01T03:00:00Z", "rules": [CITY]}
    sets = [late_text_early_instant, IN_FORCE, early_text_late_instant]
    answer = priced(a_cache(taxed_platform(*sets)))
    assert answer.to_detail()["tax_sets_held"] == {
        "count": 3, "newest_effective_from": "2025-12-31T23:30:00-05:00",
    }
    # CONTROL: the newest by TEXT is a different set.
    assert max(s["effective_from"] for s in sets) == "2026-01-01T03:00:00Z"
    assert "tax_set" not in answer.to_detail() and "effective_from" not in answer.to_detail()


# ---------------------------------------------------------------------------
# no sets is a stale cache, never a stay taxed at zero
# ---------------------------------------------------------------------------


def test_a_cache_holding_no_tax_sets_does_not_price():
    platform = taxed_platform()
    answer = pricing.price_exit("TAXD-1", a_cache(platform), now=NOW)
    assert answer.status == pricing.STALE_FACTS
    assert answer.fee_minor is None


def test_control_a_pass_holder_is_still_covered_with_no_tax_sets():
    platform = a_platform(gp=gp_register(gp_row("PASS-1")), stays=[("s-2", "PASS-1", ENTRY)])
    platform.tax_sets = []
    assert pricing.price_exit("PASS-1", a_cache(platform), now=NOW).status == pricing.COVERED


def test_no_set_in_force_at_the_exit_is_the_engines_refusal_not_a_fee():
    future_only = {"effective_from": "2030-01-01T00:00:00Z", "rules": [CITY]}
    answer = pricing.price_exit("TAXD-1", a_cache(taxed_platform(future_only)), now=NOW)
    assert answer.status == pricing.ENGINE_REFUSED
    assert answer.refusal["findings"][0]["code"] == "GAP_NO_TAX_SET_IN_FORCE"
    assert answer.fee_minor is None


# ---------------------------------------------------------------------------
# the cache: replaced whole, kept on disk, cleared with the rest
# ---------------------------------------------------------------------------


def test_the_payloads_sets_replace_the_cached_ones_whole_never_merged():
    platform = taxed_platform(IN_FORCE, {"effective_from": "2027-01-01T00:00:00Z", "rules": []})
    cache = a_cache(platform)
    assert len(cache.tax_sets) == 2
    platform.tax_sets = [NONE]
    cache.load_payload(platform.get_rules())
    assert cache.tax_sets == [NONE]


def test_the_sets_are_kept_on_disk_restored_and_cleared(tmp_path):
    store = DurableStore(tmp_path / "cache.sqlite")
    cache = DecisionCache(max_age_seconds=3600, store=store)
    cache.load_payload(taxed_platform(IN_FORCE).get_rules())
    restored = DecisionCache(max_age_seconds=3600, store=DurableStore(tmp_path / "cache.sqlite"))
    assert restored.tax_sets == [IN_FORCE]
    restored.clear()
    assert restored.tax_sets == []
    again = DecisionCache(max_age_seconds=3600, store=DurableStore(tmp_path / "cache.sqlite"))
    assert again.tax_sets == []


# ---------------------------------------------------------------------------
# the reader, with a validation held
# ---------------------------------------------------------------------------


def held(record, *, discount=200, tax=27):
    line = {"code": "tax.applied", "rule_id": "city", "text": "City tax", "delta_minor": tax}
    lines = [line] if tax else []
    subtotal = record["subtotal_minor"] - discount
    return {
        "outcome": "held", "replay": False, "currency": record["currency"],
        "fee_before_minor": record["subtotal_minor"], "discount_minor": discount,
        "subtotal_minor": subtotal, "tax_lines": lines, "fee_minor": subtotal + tax,
        "line": {"code": "validation", "rule_id": None, "text": "Validation",
                 "delta_minor": -discount},
        "held_at": "2026-06-10T18:00:01.000Z",
    }


def test_a_held_validation_is_put_up_in_order_base_validation_tax_with_the_platforms_figure():
    record = priced(a_cache(taxed_platform(IN_FORCE))).to_detail()
    shown = reader.with_validation(record, held(record))
    codes = [line["code"] for line in shown["breakdown"]]
    assert codes[-2:] == ["validation", "tax.applied"], "validation, then tax"
    assert codes.count("tax.applied") == 1, "the lane's tax on the full fee is not kept"
    taxes = [line["delta_minor"] for line in shown["breakdown"] if line["code"] == "tax.applied"]
    assert taxes == [27]
    assert shown["fee_minor"] == 900 - 200 + 27
    assert sum(line["delta_minor"] for line in shown["breakdown"]) == shown["fee_minor"]
    cart = reader.cart_for(shown)
    assert cart["total"] == shown["fee_minor"]
    assert "tax" not in cart, "no cart[tax]: the tax lines are line items, inside the total"


def test_an_answer_made_on_the_taxed_fee_is_not_put_up():
    """The claim is on the SUBTOTAL; an answer that says it was made on the taxed
    fee is about another number, and the screen keeps the fee as priced."""
    record = priced(a_cache(taxed_platform(IN_FORCE))).to_detail()
    on_taxed = {**held(record), "fee_before_minor": record["fee_minor"]}
    assert record["fee_minor"] != record["subtotal_minor"]
    assert reader.with_validation(record, on_taxed) is None
    # CONTROL: the same answer on the subtotal is put up.
    assert reader.with_validation(record, held(record)) is not None


def test_a_record_is_never_mutated_by_putting_up_a_validation():
    record = priced(a_cache(taxed_platform(IN_FORCE))).to_detail()
    before = copy.deepcopy(record)
    reader.with_validation(record, held(record))
    assert record == before
